"""Command bothy is the whole project in one binary: the registry, a host that
shares a GPU, a client that borrows one, and a mock engine for testing.

One binary because the roles overlap: a person who shares a GPU is usually
also the person who wants a client running, and shipping one artifact means
installing Bothy is copying a file or pulling an image.

Experimental commands are not in it. In Go they live behind a build tag and
register themselves through experimentalCommand, so a default build has no
trace of them -- see cmd/bothy/solve.go. Python has no build tags, so the
equivalent guarantee is structural: nothing opt-in is imported here, so
`bothy help` describes the whole binary and an unknown command is a usage
error, which is what the Go build tag was protecting.
"""

from __future__ import annotations

import datetime
import logging
import os
import signal
import sys
import threading
from typing import List, Optional, Tuple

from . import app, client, config, discovery, host, mockengine, version
from .errors import ConfigError

# configTemplate is what `bothy config init` writes.
#
# It is commented rather than filled in, because a file that sets everything
# hides which settings are actually doing something. Every line is a working
# example whose default is the value shown, so uncommenting one changes it and
# deleting the # changes nothing.
config_template = """# Bothy configuration.
#
# Any BOTHY_* setting from the documentation can go in this file, one per line,
# as NAME = value. Blank lines and lines starting with # are ignored.
#
# Precedence, highest first: a command-line flag, then the environment, then this
# file, then the built-in default. So this file is where a machine's standing
# policy lives, and a flag is for the thing you are trying once.

# ---- what this machine has -----------------------------------------------

BOTHY_ENGINE_URL = http://127.0.0.1:11434
# BOTHY_ENGINE_KIND = auto          # auto, ollama, openai, mock or static
# BOTHY_MODELS_DIR =                # Ollama models dir, for real weights digests
# BOTHY_WEIGHTS_PATH =              # a .gguf/.safetensors file to hash instead

# ---- where it listens ----------------------------------------------------

# BOTHY_HOST_LISTEN = :7777             # peers reach your engine here
# BOTHY_CLIENT_LISTEN = 127.0.0.1:11223 # your own tools borrow a model here
# BOTHY_LISTEN = :8080                  # discovery; on "run" it means the host's
# BOTHY_PUBLIC_ADDRESS =                # what peers should dial, if not your hostname

# ---- who may use it ------------------------------------------------------

# With no share key, anyone who can reach the port may use your GPU. That is a
# deliberate way to run a public host, and it is why the limits below matter.
# BOTHY_SHARE_KEY = change-me
# BOTHY_SHARE_KEYS = alice:key-a,bob:key-b
# BOTHY_ADMIN_KEY =                     # enables POST /bothy/sharing (pause/resume)
# BOTHY_PAUSED = false

# ---- what one caller may cost you ---------------------------------------
#
# These are the settings that protect the machine. With no accounts, limits are
# the only protection there is.

# BOTHY_MAX_CONCURRENT = 4          # requests in flight across the whole host
# BOTHY_OWNER_RESERVE = 1           # of those, slots kept out of peers' reach
# BOTHY_PEER_MAX_CONCURRENT = 1     # of those, slots one caller may hold at once
# BOTHY_PEER_QUOTA = 200/1h         # per-caller budget, then wait for the window
# BOTHY_MAX_REQUESTS_PER_MINUTE = 0 # per-caller rate (0 = no cap)
# BOTHY_MAX_REQUEST_TIME = 10m      # stop one request at a wall clock (0 = never)
# BOTHY_MAX_BODY = 33554432         # largest request body in bytes (32 MiB)

# ---- what the proxy will forward ----------------------------------------

# Inference routes only: the engine's own control routes (pull, delete, create)
# are refused with a 404 and never reach it.
# BOTHY_ALLOW_ROUTES = "POST /api/pull,GET /api/blobs/"
# BOTHY_ALLOW_ALL_ROUTES = false
# BOTHY_STREAM_USAGE = true         # ask the engine for token usage on streams

# ---- discovery -----------------------------------------------------------

# BOTHY_DISCOVERY_URL = http://localhost:8080
# BOTHY_REGISTRY_TOKEN =            # required to register, when the registry sets one
# BOTHY_REGISTRY_TTL = 1m
# BOTHY_HEARTBEAT = 20s

# ---- borrowing -----------------------------------------------------------

# BOTHY_HOST =                      # a host to use directly, skipping discovery
# BOTHY_MODEL =                     # the model to ask for
# BOTHY_EXPECTED_DIGEST =           # required weights digest; anything else refused
# BOTHY_LOCAL_API_KEY =             # key required on your own endpoint (optional)
# BOTHY_SERVE = true                # on "run": share a local engine when one is found
"""

# The help `bothy config` prints when it is given nothing to do.
config_usage = """bothy config — where settings live.

Usage:
  bothy config path     print the file in use, and whether it exists
  bothy config init     write a commented config file, if there is not one
  bothy config init -force   overwrite an existing one

Flags:
  -config <path>        use a different file (also BOTHY_CONFIG)

Settings come from a flag, then the environment, then this file, then the
built-in default.
"""


def main(argv: Optional[List[str]] = None) -> int:
    """Run one command and report what a shell would see as the exit code.

    Go's `main` is this function: install the signal context, configure the
    logger, read the config file before anything parses a flag, dispatch, and turn
    a failure into one logged line and a status. The statuses are part of the
    contract -- 2 for a usage error or an unusable config file, 1 for a command
    that fails at runtime, 0 on a clean stop -- because a supervisor, a
    healthcheck and a test all read them.

    `argv` defaults to `sys.argv`, and `argv[0]` is skipped, as Go's `os.Args[0]`
    is the program's own name.
    """
    argv = list(sys.argv if argv is None else argv)
    ctx = _signal_context()
    log = _configure_logging()

    if len(argv) < 2:
        usage()
        return 2
    cmd = argv[1]
    config_path, args = config_path_from(argv[2:])

    # The config file supplies the default for every setting, so it is read
    # before any command parses a flag. A file that exists but cannot be
    # understood is a hard error: running on with the built-in defaults would
    # leave a host's limits a mystery, which is the opposite of what the file is
    # for.
    try:
        file = config.load(config_path)
    except ConfigError as err:
        sys.stderr.write("bothy: %s\n" % err)
        return 2
    config.set_fallback(file)
    if file.len() > 0:
        log.info("config path=%s settings=%d", config_path, file.len())

    try:
        if cmd == "run":
            app.run(ctx, log, args)
        elif cmd == "config":
            config_command(args, config_path)
        elif cmd == "discovery":
            discovery.run(ctx, log, args)
        elif cmd == "share":
            host.run(ctx, log, args)
        elif cmd == "connect":
            client.run(ctx, log, args)
        elif cmd == "mock":
            mockengine.run(ctx, log, args)
        elif cmd in ("version", "-v", "--version"):
            print("bothy %s" % version)
            return 0
        elif cmd in ("help", "-h", "--help"):
            usage()
            return 0
        else:
            sys.stderr.write("bothy: unknown command %s\n\n" % _quote(cmd))
            usage()
            return 2
    except SystemExit as err:
        # A command's flag parser chose the status: argparse exits 0 for -h and 2
        # for a flag it cannot parse, which is what Go's flag.ExitOnError does.
        return _exit_status(err)
    except Exception as err:
        log.error("exiting command=%s err=%s", cmd, err)
        return 1
    return 0


def usage() -> None:
    """Print the help text, on stderr.

    It is the first thing anyone runs, so it lists every command, and it stays on
    the stream a pipe-then-page user expects: stdout is for a command's answer,
    not for its manual.
    """
    sys.stderr.write("""bothy — share a GPU, borrow a GPU.

One command runs the whole thing. Bothy looks for a local inference engine: if
one answers with a model, it shares it, and either way it opens the local
endpoint that borrows somebody else's. Sharing and borrowing are different
ports, so one machine can do both at once — serve your own model and use a
borrowed one in the same session. Nothing is impersonated: a local engine keeps
its own port. Bothy never moves model weights; it registers what an engine can
serve, and proxies to it.

Commands:
  run         one service: share a local engine if there is one, and borrow
  discovery   run a registry: hosts announce, clients look up
  share       run only the host half: proxy an engine and announce its models
  connect     run only the client half: expose a local OpenAI-compatible endpoint
  mock        run a fake engine, for testing the stack without a GPU
  config      where settings live: "bothy config path", "bothy config init"

Settings come from a flag, then the environment, then a config file, then the
built-in default. "bothy config init" writes a commented file to uncomment from;
"bothy config path" says which one is in use. -config <path> points elsewhere.

Every flag has a BOTHY_* environment default, which is how the containers are
configured. Run any command with -h to see its flags.

Examples:
  bothy run
  bothy discovery
  bothy share   -engine-url http://localhost:11434 -share-key secret
  bothy connect -discovery-url http://localhost:8080 -model llama3.1:8b

Then point anything OpenAI-compatible at http://127.0.0.1:11223/v1
""")


def config_command(args: List[str], path: str) -> None:
    """configCommand implements `bothy config`, the answer to "where are my
    settings and how do I write some down?"."""
    if not args:
        sys.stderr.write(config_usage)
        return

    name = args[0]
    if name == "path":
        file = config.load(path)
        state = "not found, so every setting is its default"
        if os.path.exists(path):
            state = "%d settings" % file.len()
        sys.stdout.write("%s (%s)\n" % (path, state))
        return

    if name == "init":
        force = len(args) > 1 and args[1] in ("-force", "--force")
        if os.path.exists(path) and not force:
            raise ConfigError("%s already exists; pass -force to overwrite it" % path)
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, mode=0o700, exist_ok=True)
        # 0600: a config file may hold a share key, and a key in a world-readable
        # file is a key someone else has. The mode is the one Go's WriteFile is
        # given, so it applies to the file this creates and not to one it was
        # asked to overwrite -- overwriting must not silently retighten a file its
        # owner set differently.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(config_template)
        sys.stdout.write(
            "wrote %s\n\nEvery setting is commented out, so nothing has changed yet. Uncomment the ones\nyou want and start again.\n"
            % path
        )
        return

    raise ConfigError(
        "unknown config command %s; try `bothy config path` or `bothy config init`" % _quote(name)
    )


def config_path_from(args: List[str]) -> Tuple[str, List[str]]:
    """configPathFrom pulls -config out of the arguments, because it names the file
    that every command's other flags take their defaults from: it has to be read
    before any command parses anything.

    It answers the path and the arguments that are left, and a stray -config with
    nothing after it leaves both the environment and the default in place.
    """
    path = config.text("BOTHY_CONFIG", config.default_path())
    rest: List[str] = []
    i = 0
    while i < len(args):
        arg = args[i]
        name, sep, value = arg.partition("=")
        if name not in ("-config", "--config"):
            rest.append(arg)
        elif sep:
            path = value
        elif i + 1 < len(args):
            path = args[i + 1]
            i += 1
        i += 1
    return path, rest


class _TextHandler(logging.Formatter):
    """Go's slog text handler, near enough to read side by side.

    One line per event: a local RFC3339 timestamp with milliseconds, the level,
    and the message. The messages this port logs already carry their fields as
    "key=value" (see any module's log calls), so what a reader sees is Go's shape,
    `time=2026-01-01T00:00:00.000+00:00 level=INFO msg="bothy is up ..."`, which is
    the only comparison that makes two implementations' logs worth reading
    together.
    """

    # Go spells the level WARN; Python's logging calls it WARNING.
    _LEVELS = {"WARNING": "WARN", "CRITICAL": "ERROR"}

    def format(self, record: logging.LogRecord) -> str:
        when = datetime.datetime.fromtimestamp(record.created).astimezone()
        return "time=%s level=%s msg=%s" % (
            when.isoformat(timespec="milliseconds"),
            self._LEVELS.get(record.levelname, record.levelname),
            _slog_value(record.getMessage()),
        )


def _configure_logging() -> logging.Logger:
    """Put one handler on stderr, at Info: Go's slog setup in main.

    Every module here logs through a logger named under "bothy.", so one handler
    on the root logger is what makes all of their lines reach stderr in one shape.
    """
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(_TextHandler())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.INFO)
    return logging.getLogger("bothy")


def _signal_context() -> threading.Event:
    """What `signal.NotifyContext(ctx, os.Interrupt, syscall.SIGTERM)` is here:
    an event that becomes set when the process is asked to stop.

    A service polls this, which is the convention every `run` in this package
    follows, so SIGINT and SIGTERM both shut the process down gracefully and it
    still exits 0 -- a service that ignored SIGTERM would hang a deployment's
    shutdown, which is where the fleet test and docker-compose both stop it.

    Signal handlers can only be installed from the main thread, so a caller that
    is not the main thread gets no handlers and an event nobody sets.
    """
    ctx = threading.Event()

    def stop(_signum, _frame):
        ctx.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, stop)
        except (ValueError, OSError, AttributeError):
            # Not the main thread, or no such signal on this platform.
            pass
    return ctx


def _exit_status(err: SystemExit) -> int:
    """The status a command's own exit chose, as a number.

    `None` is Python's "exited cleanly", and a string is what the interpreter
    prints before failing, which is the one case here that is not a status.
    """
    code = err.code
    if code is None:
        return 0
    if isinstance(code, int):
        return code
    sys.stderr.write("%s\n" % code)
    return 1


def _needs_quoting(value: str) -> bool:
    """Whether slog would quote a value: an empty one, or one holding a space, a
    quote, an equals sign, or anything unprintable. Those are the bytes that would
    otherwise be read as the end of one field and the start of another."""
    if value == "":
        return True
    return any(ch in ' "=' or ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value)


def _slog_value(value: str) -> str:
    """A value the way slog writes one: as it is when it needs no quoting, and
    quoted when it does."""
    return value if not _needs_quoting(value) else _quote(value)


def _quote(value: str) -> str:
    """Go's `%q`, near enough.

    It is both what `%q` writes into a message and what slog writes around a value
    that needs quoting, so one function serves both. The escapes it leaves out are
    the ones nobody puts in a command name or a log line.
    """
    out = ['"']
    for ch in value:
        if ch == '"':
            out.append('\\"')
        elif ch == "\\":
            out.append("\\\\")
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append("\\x%02x" % ord(ch))
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)
