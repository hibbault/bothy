"""The shared vocabulary: a servable model, and the identity of its weights.

The same tag means different weights on different machines -- a different
quantization, a fine-tune, a stale pull. The digest is what makes "the same model"
mean something, so it travels with every model record.

Nothing here talks to anything. That is deliberate: every other module describes
what it has or what it wants in these terms, so a disagreement about what a model
*is* has exactly one place to be settled.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .errors import ConfigError


@dataclass(frozen=True)
class Model:
    """A servable model. `digest` is the SHA-256 of the weights file."""

    name: str
    digest: str = ""

    def to_json(self) -> Dict[str, str]:
        """The wire form.

        The digest is sent even when it is empty, because an empty digest is a
        fact -- this host cannot say which weights it has -- and omitting it would
        leave a client unable to tell that from a field it failed to read.
        """
        return {"name": self.name, "digest": self.digest}

    @classmethod
    def from_json(cls, payload: Dict[str, Any]) -> "Model":
        """Read one model, tolerating a missing digest.

        An engine or registry that omits the field is saying the same thing as one
        that sends an empty string, and both mean "unknown".
        """
        return cls(name=payload.get("name") or "", digest=payload.get("digest") or "")


def models_to_json(models: Optional[Iterable[Model]]) -> List[Dict[str, str]]:
    return [m.to_json() for m in models or ()]


def models_from_json(payload: Any) -> List[Model]:
    return [Model.from_json(m) for m in payload or ()]


def normalize_digest(s: str) -> str:
    """Make digests comparable: lowercase, with a sha256: prefix.

    An empty digest stays empty rather than becoming "sha256:", because a
    prefix on its own would compare equal to another prefix on its own and
    "unknown" would start looking like agreement.
    """
    s = (s or "").strip().lower()
    if s == "":
        return ""
    if s.startswith("sha256:"):
        return s
    return "sha256:" + s


def equal_digest(a: str, b: str) -> bool:
    """Whether two digests identify the same weights.

    Two empty digests are not equal -- "unknown" must never read as "verified".
    """
    na, nb = normalize_digest(a), normalize_digest(b)
    return na != "" and na == nb


def same_name(a: str, b: str) -> bool:
    """Whether two names denote the same exact reference.

    A missing tag means :latest, the way Ollama reads it. Use this to compare two
    names that are supposed to be the same model; use `matches` to decide whether
    a model on offer satisfies a request.
    """
    abase, atag, _ = split_tag(a)
    bbase, btag, _ = split_tag(b)
    if atag == "":
        atag = "latest"
    if btag == "":
        btag = "latest"
    return abase == bbase and atag == btag


def matches(want: str, have: str) -> bool:
    """Whether a model named `have` satisfies a request for `want`.

    A request with no tag matches any tag, because "who has llama3.1?" must not
    miss a host offering llama3.1:8b. A request with a tag is exact, so asking for
    :8b never silently returns a different size. An empty request matches
    everything, which is what makes a one-model host usable with no configuration.
    """
    if (want or "").strip() == "":
        return True
    if split_tag(want)[2]:
        return same_name(want, have)
    wbase = split_tag(want)[0]
    hbase = split_tag(have)[0]
    return wbase == hbase


def split_tag(name: str) -> Tuple[str, str, bool]:
    """Split "name:tag".

    A name with no tag reports tagged=False, which keeps "no tag" distinguishable
    from an explicit request for :latest -- the first matches anything, the second
    matches only :latest. A colon at position zero is not a separator, so ":8b" is
    a (strange) name rather than a tag with no name.
    """
    name = (name or "").strip()
    i = name.rfind(":")
    if i > 0:
        return name[:i], name[i + 1:], True
    return name, "", False


def parse_list(s: str) -> List[Model]:
    """Parse "name=digest,name2=digest2".

    The digest is optional, for engines that cannot report one (llama.cpp, vLLM).
    A malformed item is an error rather than a dropped row: this string is typed
    by a person into a flag or a config file, and silently serving nothing is the
    worst way to find out about a typo.
    """
    out: List[Model] = []
    for item in (s or "").split(","):
        item = item.strip()
        if item == "":
            continue
        name, eq, dig = item.partition("=")
        name = name.strip()
        if name == "":
            raise ConfigError('bad model list item "%s": missing name' % item)
        m = Model(name=name)
        if eq:
            m = Model(name=name, digest=normalize_digest(dig))
        out.append(m)
    return out


def format_list(models: Optional[Iterable[Model]]) -> str:
    """Render models back into the name=digest form, for logging.

    A model with no digest prints as a bare name: "name=" would look like a
    missing value rather than a value that does not exist.
    """
    parts = []
    for m in models or ():
        if m.digest == "":
            parts.append(m.name)
            continue
        parts.append(m.name + "=" + m.digest)
    return ",".join(parts)
