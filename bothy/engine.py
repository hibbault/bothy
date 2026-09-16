"""Reports which models an inference engine can serve, with a digest for each.

Bothy does not implement inference. It discovers what is already running and
proxies to it, so "adding an engine" means teaching this package to describe
one — never touching the data path.

The kind strings — "auto", "ollama", "openai", "mock" and "static" — are
user-facing configuration values: one of them goes into `BOTHY_ENGINE`, and a
host reports its own back in its health output, so they are part of what an
operator sees rather than internal names. "auto" is the default so one host
config works against Ollama, the mock engine, and anything else that looks like
either.

Go's `context.Context` has no counterpart here, so nothing in this module takes
one. The only thing the context carried was the per-call deadline, and that is
`Options.timeout`, handed to `urllib` on every request — see the note there for
why a timeout belongs to the lister rather than to the call.
"""

from __future__ import annotations

import http.client
import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field, replace
from typing import Any, List, Optional, Tuple

from .digest import Hasher
from .errors import BothyError, ConfigError
from .model import Model, normalize_digest, same_name

# The manifest layer that holds the weights. Its digest is the SHA-256 of the
# weights blob — not the manifest digest an engine's API reports, which changes
# whenever any layer does.
_MODEL_LAYER_MEDIA_TYPE = "application/vnd.ollama.image.model"


class EngineError(BothyError):
    """An engine could not be listed.

    Go's listers return a plain `error`, which a caller can only print. The
    sentences share a type here because one caller has to *act* on them: `auto`
    falls back to Ollama exactly when the mock probe failed, and "the engine is
    unusable" is the whole of that condition. A bug in this code is not an
    `EngineError`, which is what keeps a typo here from being answered with a
    silent switch to a different engine.
    """


class Lister:
    """Reports the models an engine can serve.

    Go's `Lister` interface, as a base class. Both methods are what a host needs
    from an engine and all it needs: `kind` is the name that shows up in logs and
    health output, and `list_models` answers the one question this package exists
    to answer.
    """

    def list_models(self) -> List[Model]:
        """The models this engine can serve right now.

        No context parameter, and no per-call deadline: the request timeout is a
        property of the engine being asked — how long that engine is allowed to
        take — so it lives on `Options.timeout` and is applied per request by the
        lister that makes it.
        """
        raise NotImplementedError("list_models")

    def kind(self) -> str:
        """The engine kind, spelled the way the configuration spells it."""
        raise NotImplementedError("kind")


@dataclass
class Options:
    """Tune how digests are resolved."""

    # The Ollama models directory, usually ~/.ollama/models. When set, digests
    # come from the model manifest rather than the API, which is the difference
    # between a real weights hash and a manifest hash.
    models_dir: str = ""
    # A single weights file (.gguf, .safetensors) to hash, for engines that
    # cannot report a digest at all.
    weights_path: str = ""
    # The model name weights_path belongs to.
    weights_model: str = ""
    # The explicit model list, used by the "static" kind and to supply digests
    # for engines that list models without them. It is the operator's list as
    # written — "name=digest,name2=digest2" — read with `model.parse_list`, and
    # it belongs to the caller: the listers copy it before changing anything.
    static: List[Model] = field(default_factory=list)
    # How long one request to an engine may take, in seconds. Go puts this on the
    # `http.Client` it builds inside `New`, shared by every kind; it is an option
    # here for the same reason it is a constant there — every engine this process
    # talks to is asked over the same network — and so a test can shorten it.
    timeout: float = 20.0
    # The digest cache. Go builds a `digest.NewHasher()` per `New` call; passing
    # one in is how a host that starts several listers keeps a single cache, so a
    # 40GB weights file is still hashed once. None means a fresh cache.
    hasher: Optional[Hasher] = None


def new(kind: str, base_url: str, opts: Optional[Options] = None) -> Lister:
    """Return a `Lister` for kind.

    "auto" probes the engine, and is the default so one host config works against
    Ollama, the mock engine, and anything else that looks like either.

    Go returns `(Lister, error)`; the two refusals here are raised instead, both
    as `ConfigError`, because both are settings that cannot work and both are
    found before anything listens. Even the kinds that never make a request —
    "static" hashes a file and asks nobody — refuse an empty URL, because a host
    that is configured with no engine address has a mistake in it and finding
    that out at startup is the point.
    """
    opts = Options() if opts is None else opts
    base = (base_url or "").strip().rstrip("/")
    if base == "":
        raise ConfigError("engine URL is required")
    ollama_lister = _Ollama(base, opts, _hasher(opts))
    mock_lister = _Mock(base, opts)
    if kind in ("", "auto"):
        return _Auto(ollama_lister, mock_lister)
    if kind == "ollama":
        return ollama_lister
    if kind == "openai":
        return _OpenAI(base, opts)
    if kind == "mock":
        return mock_lister
    if kind == "static":
        return _Static(opts, _hasher(opts))
    raise ConfigError('unknown engine kind "%s" (want auto, ollama, openai, mock or static)' % kind)


class _Auto:
    """Probes for a mock engine's digest endpoint first, then falls back to
    Ollama's API. It re-probes on every call rather than caching, so a container
    race at startup heals itself without a restart.
    """

    def __init__(self, ollama: "_Ollama", mock: "_Mock") -> None:
        self.ollama = ollama
        self.mock = mock

    def kind(self) -> str:
        return "auto"

    def list_models(self) -> List[Model]:
        # The probe is a request, and it is made again on every call. The
        # alternative — remembering that the mock endpoint was absent — would
        # decide the engine's kind once, from the state of a container that may
        # not have finished starting.
        try:
            return self.mock.list_models()
        except EngineError:
            return self.ollama.list_models()


class _Mock:
    """Reads the digest endpoint a mock engine exposes."""

    def __init__(self, base: str, opts: Options) -> None:
        self.base = base
        self.opts = opts

    def kind(self) -> str:
        return "mock"

    def list_models(self) -> List[Model]:
        # The mock engine is the one engine Bothy also implements, so this route
        # is the one place a digest is known to be about the weights rather than
        # something the engine happened to report.
        endpoint = self.base + "/internal/models"
        payload = _get_json(endpoint, self.opts.timeout, "", endpoint)
        where = "decode %s" % endpoint
        out: List[Model] = []
        for row in _rows(payload, "models", where):
            name = _text(row, "name", where)
            if name == "":
                continue
            out.append(Model(name=name, digest=normalize_digest(_text(row, "digest", where))))
        return out


class _Ollama:
    """Lists models from Ollama's native API."""

    def __init__(self, base: str, opts: Options, hasher: Hasher) -> None:
        self.base = base
        self.opts = opts
        # Go builds a hasher for the ollama kind too and never calls it — its
        # digests come from the manifest, not from reading a file. The field is
        # kept because `New` builds the kinds the same way there, and a reader
        # coming from that side should not have to wonder which one changed.
        self.hasher = hasher

    def kind(self) -> str:
        return "ollama"

    def list_models(self) -> List[Model]:
        prefix = "engine %s: " % self.base
        payload = _get_json(self.base + "/api/tags", self.opts.timeout, prefix, "/api/tags")
        where = "engine %s: decode /api/tags" % self.base
        out: List[Model] = []
        for row in _rows(payload, "models", where):
            # Which field holds the name is inconsistent between Ollama versions,
            # and a row with neither is unusable rather than an empty-named model.
            name = _text(row, "name", where)
            if name == "":
                name = _text(row, "model", where)
            if name == "":
                continue
            dig = normalize_digest(_text(row, "digest", where))
            weights, ok = self.manifest_digest(name)
            if ok:
                dig = weights
            out.append(Model(name=name, digest=dig))
        return out

    def manifest_digest(self, name: str) -> Tuple[str, bool]:
        """Return the digest of the weights layer in the manifest for name, when
        the models directory is readable.

        A pair rather than an empty string plus a flag, because "this manifest has
        no weights layer" and "the weights layer has an empty digest" are
        different answers: the first must leave the API's digest alone, and an
        empty digest from a manifest is not a digest either. Both come back as
        `("", False)` here — Go's `(string, bool)` — and the caller falls back to
        what the engine reported.
        """
        if self.opts.models_dir == "":
            return "", False
        path = manifest_path(self.opts.models_dir, name)
        if path == "":
            return "", False
        try:
            with open(path, "rb") as f:
                raw = f.read()
        except OSError:
            return "", False
        try:
            manifest = json.loads(raw)
        except ValueError:
            return "", False
        if manifest is None:
            return "", False
        if not isinstance(manifest, dict):
            return "", False
        layers = _obj_field(manifest, "layers")
        if layers is None:
            layers = []
        if not isinstance(layers, list):
            return "", False
        for layer in layers:
            if not isinstance(layer, dict):
                return "", False
            media_type = _obj_field(layer, "mediaType")
            digest = _obj_field(layer, "digest")
            if media_type is not None and not isinstance(media_type, str):
                return "", False
            if digest is not None and not isinstance(digest, str):
                return "", False
            if media_type == _MODEL_LAYER_MEDIA_TYPE:
                return normalize_digest(digest or ""), True
        # No weights layer at all: nothing here says which weights these are, and
        # guessing from the manifest digest would pin a hash that changes
        # whenever any other layer does.
        return "", False


def manifest_path(models_dir: str, name: str) -> str:
    """Find the manifest file for name:tag under a models directory.

    It tries the default library namespace directly, then walks, so models pulled
    from another namespace still resolve.

    The walk is what makes a registry other than Ollama's own work: a manifest
    under `example.com/someone/mistral/7b` is the same model as `mistral:7b` to
    whoever pulled it, and the direct path only ever covers
    `registry.ollama.ai/library`. The walk is bounded to where manifests live,
    and it stops at the first match, so the cost is one directory subtree and not
    the models themselves.
    """
    if models_dir == "" or name == "":
        return ""
    base, tag = name, "latest"
    i = name.rfind(":")
    if i > 0:
        base, tag = name[:i], name[i + 1:]
    i = base.rfind("/")
    if i >= 0:
        base = base[i + 1:]
    if base == "" or tag == "":
        return ""
    direct = os.path.join(models_dir, "manifests", "registry.ollama.ai", "library", base, tag)
    if os.path.exists(direct):
        return direct
    # Go's filepath.WalkDir visits each directory's entries in lexical order and
    # stops at `fs.SkipAll`; os.walk does not promise an order at all, so the
    # names are sorted here to make the first match the same one Go would have
    # found.
    for dirpath, dirnames, filenames in os.walk(os.path.join(models_dir, "manifests")):
        dirnames.sort()
        filenames.sort()
        if os.path.basename(dirpath) != base:
            continue
        for filename in filenames:
            if filename == tag:
                return os.path.join(dirpath, filename)
    return ""


class _OpenAI:
    """Lists models from an OpenAI-compatible /v1/models endpoint, which is what
    vLLM and llama.cpp server expose. Those endpoints report no digest, so any
    digest configured by name is merged in; without one, the host registers an
    empty digest and clients cannot verify it.

    That is a real limitation rather than an oversight: the weights behind
    "llama3.1:8b" on a vLLM box are whatever that operator loaded, and nothing on
    the wire says which. A digest written down by hand is the only way such a host
    offers anything verifiable, and an empty digest — which a client reads as
    "unknown" rather than as agreement — is the honest alternative.
    """

    def __init__(self, base: str, opts: Options) -> None:
        self.base = base
        self.opts = opts

    def kind(self) -> str:
        return "openai"

    def list_models(self) -> List[Model]:
        prefix = "engine %s: " % self.base
        payload = _get_json(self.base + "/v1/models", self.opts.timeout, prefix, "/v1/models")
        where = "engine %s: decode /v1/models" % self.base
        out: List[Model] = []
        for row in _rows(payload, "data", where):
            model_id = _text(row, "id", where)
            if model_id == "":
                continue
            out.append(Model(name=model_id, digest=_digest_for(self.opts.static, model_id)))
        return out


def _digest_for(configured: List[Model], name: str) -> str:
    """Return the configured digest for name, if any.

    The comparison is `model.same_name`, so a digest written for `llama3.1`
    (no tag, meaning :latest) does satisfy a served `llama3.1`, and one written
    for `llama3.1:8b` does not satisfy `llama3.1:70b`. Handing a digest to a
    different size of the same model is exactly the mistake a digest exists to
    prevent, so the identity has to be exact — including case, which is part of
    the name the way it is part of the digest.
    """
    for m in configured or ():
        if same_name(m.name, name):
            return normalize_digest(m.digest)
    return ""


class _Static:
    """Serves a model list the operator wrote down. It is the escape hatch for
    engines Bothy cannot introspect, and the only kind that hashes a weights file.
    """

    def __init__(self, opts: Options, hasher: Hasher) -> None:
        self.opts = opts
        self.hasher = hasher

    def kind(self) -> str:
        return "static"

    def list_models(self) -> List[Model]:
        # A copy, because the list belongs to the caller: handing back the same
        # list after writing a digest into it would mutate the operator's config,
        # and a host that lists twice would then be hashing into a list that had
        # already been changed.
        out = list(self.opts.static)
        if self.opts.weights_path == "":
            if not out:
                raise EngineError("engine kind static needs a model list (BOTHY_MODELS)")
            return out
        try:
            dig = self.hasher.file(self.opts.weights_path)
        except OSError as err:
            raise EngineError("hash %s: %s" % (self.opts.weights_path, err)) from None
        if not out:
            out = [Model(name=self.opts.weights_model, digest=dig)]
        elif len(out) == 1:
            # One model and one weights file: the file is that model's. The name
            # does not have to match, because the operator configuring one model
            # and one file has already said which is which.
            out[0] = replace(out[0], digest=dig)
        else:
            for i, m in enumerate(out):
                if same_name(m.name, self.opts.weights_model):
                    out[i] = replace(m, digest=dig)
        return out


# ---------------------------------------------------------------------------
# Reading an engine's answer.
#
# Go gets all of this from net/http and encoding/json; the pieces here exist
# because urllib raises where Go returns, and because Go's decoder refuses a body
# of the wrong shape where Python's json module would happily hand back a string
# or a number for a field the caller expects to be a list.
# ---------------------------------------------------------------------------


def _hasher(opts: Options) -> Hasher:
    """The cache to use, or a new one, the way Go's `digest.NewHasher()` does."""
    return opts.hasher if opts.hasher is not None else Hasher()


def _get_json(url: str, timeout: float, prefix: str, endpoint: str) -> Any:
    """GET one endpoint and parse its body as JSON.

    Three failures stay three failures, none of which means "no models": a
    connection that was refused, a response that was not 200, and a body that is
    not the documented shape. A host that read any of them as an empty list would
    announce that it has nothing to serve while an engine sits there answering
    something else entirely.

    `prefix` is how the engine is named in the message ("engine http://host: ")
    and `endpoint` is the route that was asked for, so an error says which engine
    and which route failed. The remote service's own words are kept in the
    sentence rather than replaced by a status code alone, because "401" and
    "404" are hints at different mistakes.
    """
    request = urllib.request.Request(url, method="GET", headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            body = resp.read()
    except urllib.error.HTTPError as err:
        raise EngineError("%s%s: %s" % (prefix, endpoint, _status_text(err))) from None
    except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError) as err:
        # Go wraps the transport error (which carries the URL it tried) for
        # ollama and returns it bare for mock. urllib's URLError does not name the
        # URL, so the route is named here for every kind instead. The other two
        # are what urllib raises where Go's `Do` returns: a response so malformed
        # it cannot be parsed is an `HTTPException`, and a URL with no scheme in
        # it is a `ValueError`.
        raise EngineError("%s%s: %s" % (prefix, endpoint, err)) from None
    try:
        return json.loads(body)
    except ValueError as err:
        raise EngineError("%sdecode %s: %s" % (prefix, endpoint, err)) from None


def _status_text(err: urllib.error.HTTPError) -> str:
    """The status the way Go's `resp.Status` spells it: "404 Not Found".

    The reason phrase is the server's, and the code alone is what is left when it
    sent none.
    """
    reason = err.reason if isinstance(err.reason, str) else ""
    if reason == "":
        reason = http.client.responses.get(err.code, "")
    if reason == "":
        return str(err.code)
    return "%d %s" % (err.code, reason)


def _rows(payload: Any, key: str, where: str) -> List[Any]:
    """The list of rows under key, the way Go's decoder would have read it.

    A body of the wrong shape is an error rather than an empty list. Null is the
    one shape that is not: Go's decoder leaves the field as it was for a JSON
    null, and an absent list is the same fact as an empty one.
    """
    if payload is None:
        return []
    if not isinstance(payload, dict):
        raise EngineError("%s: body is not an object" % where)
    rows = _obj_field(payload, key)
    if rows is None:
        return []
    if not isinstance(rows, list):
        raise EngineError("%s: %s is not a list" % (where, key))
    return rows


def _text(row: Any, key: str, where: str) -> str:
    """One string field of a row, empty when the row does not carry it.

    A field of the wrong type is an error, because Go's decoder would have
    refused it: a number where a name belongs means the body is not the shape
    this code knows how to read, and reading it anyway would invent a model.
    """
    if not isinstance(row, dict):
        raise EngineError("%s: a row is not an object" % where)
    value = _obj_field(row, key)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise EngineError("%s: %s is not a string" % (where, key))
    return value


def _obj_field(obj: Any, key: str) -> Any:
    """One field of a JSON object, matched the way Go's decoder matches one.

    encoding/json matches a declared field name exactly or, failing that, without
    regard to case, so a server that writes "Name" still fills the field Go
    declared as "name". Bothy's two implementations read the same bodies from the
    same engines, so the leniency is kept rather than tightened.
    """
    if not isinstance(obj, dict):
        return None
    if key in obj:
        return obj[key]
    wanted = key.lower()
    for k, v in obj.items():
        if isinstance(k, str) and k.lower() == wanted:
            return v
    return None
