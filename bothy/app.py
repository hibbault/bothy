"""Package app is Bothy as one service.

A person does not pick a role. They run one command and the machine becomes
whatever it can be: if a local inference engine answers with at least one
model, Bothy shares it — and it also opens the borrowing endpoint, because
serving and borrowing are different ports and wanting both at once is
ordinary. You serve your own model and use somebody else's in the same
session; the two never collide, so there is no reason to choose. No engine
found means this machine borrows and nothing else.

The decision is made once, at startup, from a probe. Startup rather than
per-request because half a working service is worse than a clear log line: a
host that is up but cannot reach its engine would advertise itself to peers
and then refuse them.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import sys
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, List, Optional, Tuple

from . import client, engine, host, model
from .config import bool_ as _env_bool
from .config import dur as _env_dur
from .config import format_duration as _format_duration
from .config import int_ as _env_int
from .config import int64 as _env_int64
from .config import listen_default as _listen
from .config import parse_duration as _parse_duration
from .config import text as _env_text
from .errors import BothyError, ConfigError


@dataclass
class Config:
    """Config is what one Bothy needs: the host half and the client half together.

    Both live in one Config rather than two because one process runs both, and
    because they share the one setting that matters — the share key, which is the
    group secret: peers present it, and this host requires it.

    A bare `Config()` is Go's zero value, defaults and all: the built-in defaults
    live on the flags, exactly as `config.Str("BOTHY_ENGINE_URL", …)` puts them
    there, so nothing here has to be kept in step with a flag's default.
    """

    # engine_url is where a local inference engine might be. It is a probe
    # target, not a requirement: nothing there means this machine borrows.
    engine_url: str = ""
    engine_kind: str = ""
    # host_listen is where peers reach this machine's engine.
    host_listen: str = ""
    # client_listen is the local endpoint a tool points at to borrow a model.
    client_listen: str = ""
    # serve is whether to share the local engine when one is found. Off means
    # this machine only ever borrows.
    serve: bool = False
    # probe_timeout bounds the startup probe, in seconds. A local engine answers
    # in milliseconds or is not there; waiting longer only delays a decision.
    probe_timeout: float = 0.0

    # share_key is the group secret. It is deliberately one key for both halves:
    # this host requires it of its peers, and this client presents it to the
    # hosts it borrows from. Share any other key here and the agreement is a
    # shape nobody wrote down.
    share_key: str = ""
    # share_keys replaces share_key with per-peer keys on the host half,
    # "alice:key,bob:key", so usage is attributed to a person.
    share_keys: str = ""

    public_address: str = ""
    discovery_url: str = ""
    register_token: str = ""
    heartbeat: float = 0.0
    max_concurrent: int = 0
    owner_reserve: int = 0
    # peer_max_concurrent and max_request_time bound what one peer, and one
    # request, can cost the machine.
    peer_max_concurrent: int = 0
    max_request_time: float = 0.0
    peer_quota: str = ""
    requests_per_minute: int = 0
    admin_key: str = ""
    paused: bool = False
    stream_usage: bool = False
    # allow_all_routes and allow_routes widen what the host proxies to the
    # engine. The default is the inference allowlist; see host/routes.go for why.
    allow_all_routes: bool = False
    allow_routes: List[host.RouteRule] = field(default_factory=list)
    # max_body caps a proxied request body in bytes.
    max_body: int = 0

    models_dir: str = ""
    weights_path: str = ""
    weights_model: str = ""
    static: List[model.Model] = field(default_factory=list)

    # model is what to ask for when borrowing. expected_digest pins the weights.
    model: str = ""
    expected_digest: str = ""
    # host_address skips discovery and dials one host directly.
    host_address: str = ""
    # local_api_key guards this machine's borrowing endpoint. Empty leaves it
    # open, which is usual because it listens on loopback only.
    local_api_key: str = ""


def run(ctx, log: logging.Logger, args: List[str]) -> None:
    """Parse flags for the "run" command and serves until ctx is cancelled.

    Go returns an error from here; the failures are raised instead, so the
    command line reports one sentence rather than a service that quietly does
    nothing.
    """
    ns = _parser().parse_args(args)

    static = model.parse_list(ns.models)
    allow_routes = host.parse_routes(ns.allow_routes)
    cfg = Config(
        engine_url=ns.engine_url,
        engine_kind=ns.engine_kind,
        host_listen=ns.host_listen,
        client_listen=ns.client_listen,
        serve=ns.serve,
        probe_timeout=ns.probe_timeout,
        share_key=ns.share_key,
        share_keys=ns.share_keys,
        public_address=ns.address,
        discovery_url=ns.discovery_url,
        register_token=ns.register_token,
        heartbeat=ns.heartbeat,
        max_concurrent=ns.max_concurrent,
        owner_reserve=ns.owner_reserve,
        peer_max_concurrent=ns.peer_max_concurrent,
        max_request_time=ns.max_request_time,
        peer_quota=ns.peer_quota,
        requests_per_minute=ns.max_requests_per_minute,
        admin_key=ns.admin_key,
        paused=ns.paused,
        stream_usage=ns.stream_usage,
        allow_all_routes=ns.allow_all_routes,
        allow_routes=allow_routes,
        max_body=ns.max_body,
        models_dir=ns.models_dir,
        weights_path=ns.weights,
        weights_model=ns.weights_model,
        static=static,
        model=ns.model,
        expected_digest=ns.expected_digest,
        host_address=ns.host,
        local_api_key=ns.local_api_key,
    )

    opts = engine.Options(
        models_dir=cfg.models_dir,
        weights_path=cfg.weights_path,
        weights_model=cfg.weights_model,
        static=cfg.static,
    )
    # Go bounds the startup probe with a context deadline. `Lister.list_models`
    # takes no context here, so the probe carries its deadline as the lister's own
    # request timeout instead; the host builds its own lister from `opts`, so
    # nothing but the probe is hurried. A probe timeout of 0 is Go's "no limit",
    # and the engine's own timeout is what is left of that.
    probe_opts = dataclasses.replace(opts, timeout=cfg.probe_timeout or opts.timeout)
    lister = engine.new(cfg.engine_kind, cfg.engine_url, probe_opts)

    serve = should_serve(ctx, cfg, lister, log)

    halves: List[half] = []
    if serve:
        h = host.new(host.Config(
            listen=cfg.host_listen,
            engine_url=cfg.engine_url,
            engine_kind=cfg.engine_kind,
            discovery_url=cfg.discovery_url,
            register_token=cfg.register_token,
            share_key=cfg.share_key,
            share_keys=cfg.share_keys,
            public_address=public_address(cfg),
            heartbeat=cfg.heartbeat,
            max_concurrent=cfg.max_concurrent,
            owner_reserve=cfg.owner_reserve,
            peer_max_concurrent=cfg.peer_max_concurrent,
            max_request_time=cfg.max_request_time,
            peer_quota=cfg.peer_quota,
            requests_per_minute=cfg.requests_per_minute,
            admin_key=cfg.admin_key,
            paused=cfg.paused,
            stream_usage=cfg.stream_usage,
            allow_all_routes=cfg.allow_all_routes,
            allow_routes=cfg.allow_routes,
            max_body=cfg.max_body,
            engine=opts,
        ), log)
        # A configuration a host refuses is a configuration this process cannot
        # be: it is raised rather than downgraded to borrowing, so a typo in a
        # limit does not quietly turn sharing off.
        halves.append(half(name="share", serve=h.serve))

    c = client.new(client.Config(
        listen=cfg.client_listen,
        host_address=cfg.host_address,
        discovery_url=cfg.discovery_url,
        model=cfg.model,
        share_key=cfg.share_key,
        expected_digest=cfg.expected_digest,
        local_api_key=cfg.local_api_key,
    ), log)
    halves.append(half(name="connect", serve=c.serve))

    if not serve and cfg.host_address == "" and cfg.discovery_url == "":
        log.warning(
            "nothing to borrow from: set -discovery-url or -host"
            " hint=or point -engine-url at a local engine and set -share-key, so this machine has something to serve"
        )
    log.info(
        "bothy is up borrowing_on=%s sharing=%s serving_on=%s",
        cfg.client_listen,
        "true" if serve else "false",
        cfg.host_listen,
    )
    run_all(ctx, log, halves)


def should_serve(ctx, cfg: Config, lister: engine.Lister, log: logging.Logger) -> bool:
    """shouldServe decides whether this machine has something to share.

    There are three ways to end up borrowing only, and each says so out loud
    rather than leaving a host that is up but useless: no engine answered, an
    engine with no models, and a host with no key. The last one is a refusal
    rather than a warning because this mode is automatic — a host with no key
    lets anyone who can reach the port spend this machine's GPU, and nothing
    asked for that in advance, so it will not happen by accident.

    `ctx` is kept because Go's signature has it — there it bounds the probe. The
    probe cannot be interrupted from here: `Lister.list_models` takes no context,
    so its deadline is the lister's own timeout, which `run` sets from
    probe_timeout.
    """
    if not cfg.serve:
        log.info(
            "not sharing: -serve is off, so this machine only borrows borrowing_on=%s",
            cfg.client_listen,
        )
        return False
    try:
        models = lister.list_models()
    except Exception as err:
        log.info("no local engine answered; borrowing only engine=%s err=%s", cfg.engine_url, err)
        return False
    if len(models) == 0:
        log.info("the local engine offers no models; borrowing only engine=%s", cfg.engine_url)
        return False
    if cfg.share_key == "" and cfg.share_keys == "":
        log.warning(
            "not sharing: no share key set, and an open host lets anyone who can reach the port use your GPU"
            " engine=%s models=%s hint=set a share key (-share-key, or -share-keys name:key,...) and start again to serve",
            cfg.engine_url,
            model.format_list(models),
        )
        return False
    log.info(
        "sharing the local engine engine=%s listen=%s models=%s",
        cfg.engine_url,
        cfg.host_listen,
        model.format_list(models),
    )
    return True


def public_address(cfg: Config) -> str:
    """publicAddress is the address to advertise when none was given: what a peer
    should dial, which is the host-listen port on this machine's name."""
    if cfg.public_address != "":
        return cfg.public_address
    return host.default_address(cfg.host_listen)


@dataclass
class half:
    """half is one of the services this process runs."""

    name: str
    serve: Callable[[object], None]


def run_all(ctx, log: logging.Logger, halves: List[half]) -> None:
    """runAll runs every half until they all stop.

    A failure takes the whole process down rather than leaving half a service:
    both ports are one thing to a user, and a Bothy that is borrowing but not
    serving — or the reverse — is a state nobody asked for. The raised error names
    which half failed, because "address already in use" means something different
    on each, and the failure it comes from is chained as the cause, which is what
    Go's %w carries.
    """
    child = _ChildContext(ctx)
    lock = threading.Lock()
    failed: List[Tuple[str, BaseException]] = []

    def run_one(h: half) -> None:
        try:
            h.serve(child)
        except Exception as err:
            log.error("service stopped service=%s err=%s", h.name, err)
            with lock:
                if not failed:
                    failed.append((h.name, err))
            child.set()

    threads = [
        threading.Thread(target=run_one, args=(h,), name=h.name) for h in halves
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    if failed:
        name, err = failed[0]
        raise BothyError("%s: %s" % (name, err)) from err


class _ChildContext:
    """A cancellable child of a context, which is what `context.WithCancel` makes
    in runAll.

    The parent is anything with `is_set()`, the convention every `run` in this
    package follows, so this is the second event: the one a failed half sets to
    tell its partner to stop.
    """

    def __init__(self, parent) -> None:
        self._parent = parent
        self._event = threading.Event()

    def is_set(self) -> bool:
        return self._event.is_set() or (self._parent is not None and self._parent.is_set())

    def set(self) -> None:
        self._event.set()


class _GoHelp(argparse.HelpFormatter):
    """Help that prints a default the way Go's flag package does: "(default 4)",
    "(default "127.0.0.1:11223")", and nothing at all when the default is the zero
    value.

    The defaults are already resolved from the environment and any config file by
    the time the parser is built, which is what makes `bothy run -h` the place to
    see which settings are actually in force.
    """

    def _get_help_string(self, action: argparse.Action) -> str:
        text = action.help or ""
        if "%(default)" in text or action.default is None:
            return text
        rendered = _default_text(action.default)
        return text if rendered == "" else text + " (default %s)" % rendered


class _FlagParser(argparse.ArgumentParser):
    """Go's `flag.ExitOnError`, as an ArgumentParser.

    Go prints a flag set's usage to stderr, and exits 0 for -h and 2 for a flag it
    cannot understand; argparse does the first half of that on stdout, because
    `print_help` and `print_usage` name stdout before they reach
    `_print_message`. Both are overridden, so this command's manual lands on stderr
    with everyone else's -- stdout is for what a command answered.
    """

    def _print_message(self, message: Optional[str], file=None) -> None:  # type: ignore[override]
        if message:
            (file or sys.stderr).write(message)

    def print_usage(self, file=None) -> None:  # type: ignore[override]
        self._print_message(self.format_usage(), file or sys.stderr)

    def print_help(self, file=None) -> None:  # type: ignore[override]
        self._print_message(self.format_help(), file or sys.stderr)

    def error(self, message: str) -> None:
        self.print_usage(sys.stderr)
        self.exit(2, "%s: error: %s\n" % (self.prog, message))


def _parser() -> argparse.ArgumentParser:
    """The flags of `bothy run`, ported one for one.

    Every name, help sentence and BOTHY_* default here is the Go flag set's, in
    the order it declares them.
    """
    parser = _FlagParser(
        prog="bothy run",
        add_help=False,
        allow_abbrev=False,
        formatter_class=_GoHelp,
        description="one service: share a local engine if there is one, and borrow",
    )
    parser.add_argument("-h", "-help", "--help", action="help", help="show this help and exit")
    parser.add_argument(
        "-engine-url", "--engine-url",
        default=_env_text("BOTHY_ENGINE_URL", "http://127.0.0.1:11434"),
        help="your inference engine's base URL, if you have one",
    )
    parser.add_argument(
        "-engine-kind", "--engine-kind",
        default=_env_text("BOTHY_ENGINE_KIND", "auto"),
        help="auto, ollama, openai, mock or static",
    )
    parser.add_argument(
        "-host-listen", "--host-listen",
        default=_listen(":7777", "BOTHY_HOST_LISTEN", "BOTHY_LISTEN"),
        help="address to share on, for peers",
    )
    parser.add_argument(
        "-client-listen", "--client-listen",
        default=_env_text("BOTHY_CLIENT_LISTEN", "127.0.0.1:11223"),
        help="local address for the OpenAI-compatible endpoint",
    )
    parser.add_argument(
        "-serve", "--serve",
        nargs="?",
        const="true",
        type=_flag_bool,
        default=_env_bool("BOTHY_SERVE", True),
        help="share your engine when one is found; false borrows and nothing else",
    )
    parser.add_argument(
        "-probe-timeout", "--probe-timeout",
        type=_env_duration,
        default=_env_dur("BOTHY_PROBE_TIMEOUT", 3.0),
        help="how long to wait for a local engine before deciding to borrow only",
    )
    parser.add_argument(
        "-share-key", "--share-key",
        default=_env_text("BOTHY_SHARE_KEY", ""),
        help="group secret: your host requires it, your client presents it",
    )
    parser.add_argument(
        "-share-keys", "--share-keys",
        default=_env_text("BOTHY_SHARE_KEYS", ""),
        help="per-peer keys as name:key,name:key; wins over -share-key",
    )
    parser.add_argument(
        "-address", "--address",
        default=_env_text("BOTHY_PUBLIC_ADDRESS", ""),
        help="address to advertise to peers (default: hostname plus host-listen port)",
    )
    parser.add_argument(
        "-discovery-url", "--discovery-url",
        default=_env_text("BOTHY_DISCOVERY_URL", ""),
        help="registry to announce to and look up in",
    )
    parser.add_argument(
        "-register-token", "--register-token",
        default=_env_text("BOTHY_REGISTRY_TOKEN", ""),
        help="token for registering with the registry",
    )
    parser.add_argument(
        "-heartbeat", "--heartbeat",
        type=_env_duration,
        default=_env_dur("BOTHY_HEARTBEAT", 20.0),
        help="how often to re-announce",
    )
    parser.add_argument(
        "-max-concurrent", "--max-concurrent",
        type=int,
        default=_env_int("BOTHY_MAX_CONCURRENT", 4),
        help="requests to serve at once across all peers (0 = no cap)",
    )
    parser.add_argument(
        "-owner-reserve", "--owner-reserve",
        type=int,
        default=_env_int("BOTHY_OWNER_RESERVE", 1),
        help="of max-concurrent, how many slots peers may not use",
    )
    parser.add_argument(
        "-peer-max-concurrent", "--peer-max-concurrent",
        type=int,
        default=_env_int("BOTHY_PEER_MAX_CONCURRENT", 0),
        help="how many slots one peer may hold at once (0 = no separate cap)",
    )
    parser.add_argument(
        "-max-request-time", "--max-request-time",
        type=_env_duration,
        default=_env_dur("BOTHY_MAX_REQUEST_TIME", 0.0),
        help="wall-clock limit for one request, e.g. 10m (0 = no limit)",
    )
    parser.add_argument(
        "-peer-quota", "--peer-quota",
        default=_env_text("BOTHY_PEER_QUOTA", ""),
        help="per-peer request budget as count/period, e.g. 200/1h",
    )
    parser.add_argument(
        "-max-requests-per-minute", "--max-requests-per-minute",
        type=int,
        default=_env_int("BOTHY_MAX_REQUESTS_PER_MINUTE", 0),
        help="request rate allowed per peer (0 = no cap)",
    )
    parser.add_argument(
        "-admin-key", "--admin-key",
        default=_env_text("BOTHY_ADMIN_KEY", ""),
        help="key for POST /bothy/sharing, which pauses and resumes sharing",
    )
    parser.add_argument(
        "-paused", "--paused",
        nargs="?",
        const="true",
        type=_flag_bool,
        default=_env_bool("BOTHY_PAUSED", False),
        help="start paused: refuse peers until resumed",
    )
    parser.add_argument(
        "-stream-usage", "--stream-usage",
        nargs="?",
        const="true",
        type=_flag_bool,
        default=_env_bool("BOTHY_STREAM_USAGE", True),
        help="ask the engine for token usage on streamed replies, so they can be metered",
    )
    parser.add_argument(
        "-allow-all-routes", "--allow-all-routes",
        nargs="?",
        const="true",
        type=_flag_bool,
        default=_env_bool("BOTHY_ALLOW_ALL_ROUTES", False),
        help="proxy every engine path, including its control routes (for a network you control)",
    )
    parser.add_argument(
        "-allow-routes", "--allow-routes",
        default=_env_text("BOTHY_ALLOW_ROUTES", ""),
        help="extra engine paths to proxy, as path or 'METHOD path', comma separated",
    )
    parser.add_argument(
        "-max-body", "--max-body",
        type=int,
        default=_env_int64("BOTHY_MAX_BODY", host.DEFAULT_MAX_BODY),
        help="largest request body to proxy, in bytes (0 = no cap)",
    )
    parser.add_argument(
        "-models-dir", "--models-dir",
        default=_env_text("BOTHY_MODELS_DIR", ""),
        help="Ollama models directory, for real weights digests",
    )
    parser.add_argument(
        "-weights", "--weights",
        default=_env_text("BOTHY_WEIGHTS_PATH", ""),
        help="weights file to hash (.gguf/.safetensors)",
    )
    parser.add_argument(
        "-weights-model", "--weights-model",
        default=_env_text("BOTHY_WEIGHTS_MODEL", ""),
        help="model name the weights file belongs to",
    )
    parser.add_argument(
        "-models", "--models",
        default=_env_text("BOTHY_MODELS", ""),
        help="static model list as name=digest,name=digest",
    )
    parser.add_argument(
        "-model", "--model",
        default=_env_text("BOTHY_MODEL", ""),
        help="model to ask a host for when borrowing",
    )
    parser.add_argument(
        "-expected-digest", "--expected-digest",
        default=_env_text("BOTHY_EXPECTED_DIGEST", ""),
        help="required weights digest; anything else is refused",
    )
    parser.add_argument(
        "-host", "--host",
        default=_env_text("BOTHY_HOST", ""),
        help="borrow from this host directly, skipping discovery",
    )
    parser.add_argument(
        "-local-api-key", "--local-api-key",
        default=_env_text("BOTHY_LOCAL_API_KEY", ""),
        help="key required on the local endpoint (optional)",
    )
    return parser


def _flag_bool(value: str) -> bool:
    """Read a boolean flag, spelled the way `config.bool_` reads one from the
    environment: Go's strconv.ParseBool, plus yes/no and on/off.

    A word it cannot read is an error rather than a silent False, because these
    flags decide whether this machine hands out its GPU. Go's boolean flags do not
    take a separate word -- `-serve` is true and the value is written
    `-serve=false` -- but argparse's optional value accepts both spellings here.
    """
    lowered = value.strip().lower()
    if lowered in ("1", "t", "true", "yes", "y", "on"):
        return True
    if lowered in ("0", "f", "false", "no", "n", "off"):
        return False
    raise argparse.ArgumentTypeError("invalid boolean %r" % value)


def _env_duration(value: str) -> float:
    """Read a duration flag the way Go's `flag.Duration` does: "10m", "1h30m"
    or "500ms".

    An unreadable one is a flag error rather than a zero, which is what Go's flag
    package reports when it cannot parse one: the status is 2 and the reason names
    the flag, not a service that quietly lost its timeout.
    """
    try:
        return _parse_duration(value)
    except ConfigError as err:
        raise argparse.ArgumentTypeError(str(err)) from None


def _default_text(value: Any) -> str:
    """A default as Go's PrintDefaults writes one, or "" to leave it out.

    Go omits the default when it is the zero value, writes a duration the way it
    was typed ("3s", not "3.0"), and quotes a string, because an address with a
    space in it would otherwise be unreadable.
    """
    if isinstance(value, bool):
        return "true" if value else ""
    if isinstance(value, int):
        return "" if value == 0 else str(value)
    if isinstance(value, float):
        return "" if value == 0 else _format_duration(value)
    text = str(value)
    if text == "":
        return ""
    return json.dumps(text, ensure_ascii=False)
