"""The registry service: hosts announce what they serve, clients ask who has it.

It is deliberately dumb. It cannot check that an address is reachable, and it
cannot check that a digest is honest, so it is a bulletin board rather than an
authority. The one thing it does enforce is expiry, so hosts that stopped
heartbeating fall out of the list instead of being handed to clients forever.

Registration doubles as the heartbeat: there is no separate liveness call, and the
page's `age` column says how close an entry is to expiring rather than decorate
the table with when it arrived.

Go's `html/template` escapes every value it interpolates and Python's standard
library has no template engine that does, so the page is built here by hand and
every value goes through `html.escape(..., quote=True)`. That is not decoration
either: anyone who can reach an open registry can register a model, and an
unescaped registry page is a cross-site-scripting hole they can post into.

Go's `context.Context` has no Python counterpart. `run` takes the stop signal in
its place -- anything with an `is_set()` method, usually a `threading.Event` --
and nothing else here takes one, because nothing else waits on anyone for longer
than the call it was asked to make.
"""

from __future__ import annotations

import argparse
import html
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, List, Optional, Sequence

from . import config, httpx, model, registry
from .errors import BodyTooLarge, ConfigError

_log = logging.getLogger("bothy.discovery")

# How much of a register body the registry will hold. Without this, one POST is
# enough to make the registry allocate whatever the sender likes. (Go's
# `http.MaxBytesReader(w, r.Body, 1<<20)`.)
MAX_REGISTER_BODY = 1 << 20

# How often `run` looks at whether it has been asked to stop. Go selects on a
# context; a stop signal has to be polled, and this is the granularity of a
# shutdown nobody is waiting on with a stopwatch.
_POLL = 0.05


@dataclass
class Config:
    """Describes the registry service."""

    # listen is the address the service is served on. Go keeps it in the flag
    # rather than in `Config`; it is here so that `Config` is the whole of what a
    # registry is configured with, and so a caller that builds one (a test, the
    # command line) does not have to know which of the two holds the address.
    listen: str = ":8080"
    # ttl is Go's `time.Duration`; seconds, like every other duration in this
    # package.
    ttl: float = 60.0
    # token, when set, is required to register. Without it anyone reachable can
    # publish entries, which is a spam problem more than a security one.
    token: str = ""


class Server:
    """The registry's HTTP surface."""

    def __init__(self, cfg: Config, log: Optional[logging.Logger] = None) -> None:
        self.cfg = cfg
        self.store = registry.Store(cfg.ttl)
        self.token = cfg.token
        self.log = log if log is not None else _log

    def handler(self) -> httpx.Handler:
        """The registry routes.

        The human page is registered for exactly "/" -- a prefix pattern would
        answer every unknown path with it, which would make a typo look like a
        working registry.

        GET `/healthz` is unauthenticated on purpose: it is what a container
        healthcheck is, and it names a live count rather than anything a caller
        supplied. `/models` is too, because a registry nobody can look up is a
        registry nobody can use.
        """
        router = httpx.Router()
        router.handle("GET", "/", self._handle_index)
        router.handle("GET", "/healthz", self._handle_health)
        router.handle("GET", "/models", self._handle_list)
        router.handle("POST", "/register", httpx.require_token(self.token, self._handle_register))
        return httpx.log_requests(self.log, router)

    def _handle_health(self, req: httpx.Request, resp: httpx.Response) -> None:
        resp.json(
            200,
            {
                "ok": True,
                "live_entries": self.store.len(),
                "ttl": config.format_duration(self.store.ttl()),
            },
        )

    def _handle_register(self, req: httpx.Request, resp: httpx.Response) -> None:
        """Accept a batch of entries.

        A host serving several models sends them in one call, and repeats that
        call as its heartbeat. Every refusal of the body is the same sentence Go
        produces -- an oversized body included, which is why the limit is caught
        here rather than left to the 413 a body limit normally earns: Go's
        `MaxBytesReader` fails the decode, so the registry says "invalid JSON
        body" and nothing is stored on the way through.
        """
        try:
            body = req.body_bytes(MAX_REGISTER_BODY)
        except BodyTooLarge as err:
            _close_connection(resp)
            resp.error(400, "invalid JSON body: %s" % err)
            return
        if not body:
            # Go's decoder answers an empty body with io.EOF, and the message says
            # so rather than reading as "no entries to register".
            resp.error(400, "invalid JSON body: EOF")
            return
        try:
            payload = json.loads(body)
        except ValueError as err:
            resp.error(400, "invalid JSON body: %s" % err)
            return
        if not isinstance(payload, dict):
            resp.error(400, "invalid JSON body: want a JSON object")
            return
        raw = payload.get("entries")
        if raw is not None and not isinstance(raw, list):
            resp.error(400, "invalid JSON body: entries is not a list")
            return
        entries: List[registry.Entry] = []
        for item in raw or ():
            try:
                entries.append(registry.Entry.from_json(item))
            except (AttributeError, TypeError, ValueError) as err:
                # A field of the wrong shape fails the whole decode in Go, so a
                # batch with one bad `free` is refused rather than partly stored.
                resp.error(400, "invalid JSON body: %s" % err)
                return
        if not entries:
            resp.error(400, "no entries to register")
            return
        n = self.store.register(entries)
        self.log.info("registered accepted=%d offered=%d live=%d", n, len(entries), self.store.len())
        resp.json(
            200,
            {
                "registered": n,
                "live": self.store.len(),
                "ttl": config.format_duration(self.store.ttl()),
            },
        )

    def _handle_list(self, req: httpx.Request, resp: httpx.Response) -> None:
        """Answer "who has this model?"."""
        entries = self.store.list(req.param("model"))
        resp.json(200, {"entries": [e.to_json() for e in entries], "count": len(entries)})

    def _handle_index(self, req: httpx.Request, resp: httpx.Response) -> None:
        """Render the live registry for a person looking at it."""
        now = time.time()
        entries = self.store.list("")
        data = IndexData(
            entries=[
                IndexEntry(
                    model=e.model,
                    host=e.host,
                    address=e.address,
                    free=_free_cell(e),
                    digest=_short_digest(e.digest),
                    age=_age(now - e.last_seen),
                )
                for e in entries
            ],
            count=len(entries),
            ttl=config.format_duration(self.store.ttl()),
            # Already discoverable by anyone who can reach the port -- POST and
            # read the answer -- so stating it costs nothing and saves an operator
            # guessing.
            open=self.token == "",
        )
        # Go streams the template straight to the response and logs a render
        # failure, because the status line is already sent by then. There is
        # nothing here that can fail: the page is one escaped string.
        resp.send_bytes(200, render_index(data), content_type="text/html; charset=utf-8")


def new_server(cfg: Config, log: Optional[logging.Logger] = None) -> Server:
    """Return a registry whose entries live for cfg.ttl without a heartbeat.

    (Go's `NewServer`. The class builds exactly the same thing; this name is kept
    because it is the one the Go side exported and the one `run` and the command
    line call.)
    """
    return Server(cfg, log)


def run(ctx: Any = None, log: Optional[logging.Logger] = None, args: Optional[Sequence[str]] = None) -> None:
    """Parse flags for the "discovery" command and serve until stopped.

    Go returns an error here; the same failures are raised, so a caller that wants
    to report one gets it as a sentence rather than as a silent non-serving
    process. A bad flag is the exception: Go's flag set is built with
    ExitOnError, and argparse reproduces that by raising `SystemExit(2)` -- the
    one failure that is a usage message rather than something to catch.

    `ctx` stands in for the Go context: anything with an `is_set()` method, so a
    `threading.Event` is the usual one, and calling `set()` on it is what stops the
    server. None means nobody will ask, and it serves until interrupted -- which
    is also what `run(args)` means, so the flags alone may be passed first.
    """
    if args is None and isinstance(ctx, (list, tuple)):
        # `run(args)`: the flags on their own are the common call, and nothing but
        # arguments ever arrives in that position.
        ctx, args = None, list(ctx)
    log = log if log is not None else _log

    # Every flag is also its long form, because a service configured in a compose
    # file is written with `--listen` and one configured on a command line by a
    # person who read the Go documentation writes `-listen`.
    parser = argparse.ArgumentParser(prog="bothy discovery", description="run a registry: hosts announce, clients look up")
    parser.add_argument("-listen", "--listen", default=config.listen_default(":8080"), help="address to listen on")
    parser.add_argument(
        "-ttl",
        "--ttl",
        type=config.parse_duration,
        default=config.dur("BOTHY_REGISTRY_TTL", 60.0),
        help="how long a registration stays live without a heartbeat",
    )
    # Go spells this flag `-register-token`, and the environment variable has
    # always been BOTHY_REGISTRY_TOKEN; both names are accepted so that a
    # deployment script written against either keeps working.
    parser.add_argument(
        "-token",
        "--token",
        "-register-token",
        "--register-token",
        default=config.text("BOTHY_REGISTRY_TOKEN", ""),
        help="token required to register (optional)",
    )
    opts = parser.parse_args(_flag_values(list(args or ())))

    # A TTL of zero or less expires every registration the moment it arrives, so
    # the registry would answer every lookup with nothing while looking healthy.
    # That is a misconfiguration, and it says so here instead.
    if opts.ttl <= 0:
        raise ConfigError(
            "ttl %s must be positive: an entry that expires on arrival leaves the registry serving nobody"
            % config.format_duration(opts.ttl)
        )

    s = new_server(Config(listen=opts.listen, ttl=opts.ttl, token=opts.token), log)
    if opts.token == "":
        log.warning("registration is open: anyone who can reach this port can publish entries")
    log.info("registry ready ttl=%s", config.format_duration(opts.ttl))
    _serve(opts.listen, s.handler(), ctx, log)


def _serve(addr: str, handler: httpx.Handler, ctx: Any, log: logging.Logger) -> None:
    """Bind, serve, and stop when ctx asks.

    `httpx.serve` is the shared lifecycle, but it serves until interrupted and the
    registry has to be stoppable from another thread, so the three steps it is
    made of are spelled out here around a wait that can also be a stop request.
    Binding happens in the constructor, which is why an address that cannot be
    bound fails the process rather than leaving a registry that quietly is not
    there.
    """
    server = httpx.Server(addr, handler, log)
    log.info("listening addr=%s", server.addr)
    server.start()
    try:
        if ctx is None:
            server.wait()
        else:
            while not ctx.is_set():
                time.sleep(_POLL)
    except KeyboardInterrupt:
        # A person asking to stop is not a failure.
        pass
    finally:
        server.shutdown()


# The flags that take a value, so that a value which looks like a flag is still
# read as one.
_VALUE_FLAGS = frozenset(
    ("-listen", "--listen", "-ttl", "--ttl", "-token", "--token", "-register-token", "--register-token")
)


def _flag_values(args: List[str]) -> List[str]:
    """Join `-flag value` into `-flag=value`.

    Go's flag package takes the token after a flag as that flag's value whatever
    it looks like, so `-ttl -1m` is a negative TTL rather than a missing one.
    argparse reads the `-1m` as an option of its own and refuses the pair, which
    would turn a measurable misconfiguration into a usage error, so the pair is
    joined here exactly as `-ttl=-1m` would have been written by hand.
    """
    out: List[str] = []
    i = 0
    while i < len(args):
        tok = args[i]
        if tok in _VALUE_FLAGS and i + 1 < len(args):
            out.append(tok + "=" + args[i + 1])
            i += 2
            continue
        out.append(tok)
        i += 1
    return out


def _close_connection(resp: httpx.Response) -> None:
    """Mark the connection for closing, since the body was left unread.

    Go's server does this itself once a handler walks away from a body past the
    few hundred kilobytes it is willing to drain. `Response` has no way to say it,
    so it is said to the stdlib handler underneath -- without it, the next request
    on a keep-alive connection would be parsed out of the body just refused.
    """
    conn = getattr(resp, "_h", None)
    if conn is not None:
        conn.close_connection = True


# ---------------------------------------------------------------------------
# The page.
#
# It renders exactly the live entries the JSON API already hands to anyone who
# asks, and nothing else. A registry that showed more than its own contract would
# be a directory with extra reach, and one that recorded who looked would be the
# largest metadata leak in the design -- so this page keeps no state, has no query
# to express a lookup with, and is read-only by construction: it is registered for
# GET on one path, and registration is still the only write.
#
# Go holds the template in `indexPage` and lets html/template escape as it
# interpolates. There is no such engine in the Python standard library, so the
# markup is a constant here and `render_index` is the only thing that writes a
# value into it -- through `html.escape(..., quote=True)`, with no exception,
# because the values are whatever someone registered.
# ---------------------------------------------------------------------------

_PAGE_HEAD = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>bothy registry</title>
<style>
  body { font: 14px/1.5 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
         margin: 2rem; color: #111; background: #fff; }
  h1 { font-size: 1.1rem; margin: 0 0 0.25rem; }
  p { margin: 0.25rem 0; }
  table { border-collapse: collapse; margin-top: 1rem; }
  th, td { text-align: left; padding: 0.25rem 1.25rem 0.25rem 0; }
  th { border-bottom: 1px solid #bbb; font-weight: 600; }
  td.num, th.num { text-align: right; padding-right: 2rem; }
  .quiet { color: #666; margin-top: 1rem; }
  footer { color: #666; margin-top: 2rem; font-size: 0.9rem; max-width: 60rem; }
</style>
</head>
<body>
<h1>bothy registry</h1>
"""

_PAGE_ROWS = """<table>
<tr><th>model</th><th>host</th><th>address</th><th class="num">free</th><th>digest</th><th>last seen</th></tr>
"""

_PAGE_FOOT = """
<footer>
A read-only view of <code>/models</code>, which is the contract. Nothing here is
checked: the registry cannot tell whether an address is reachable, and it cannot
tell whether a digest is honest. <code>free</code> is free <em>peer</em> slots,
as the host last reported them; <code>unknown</code> means it reported nothing,
which is not the same as being full.
</footer>
</body>
</html>
"""


@dataclass
class IndexEntry:
    """One row. It holds what the page shows rather than the Entry itself,
    because a view that could reach the stored record is a view that can grow a
    field nobody decided to publish.
    """

    model: str = ""
    host: str = ""
    address: str = ""
    # free is a string because the page has to say "unknown" as readily as a
    # number, and a host that reported nothing must not be shown as full.
    free: str = ""
    digest: str = ""
    age: str = ""


@dataclass
class IndexData:
    """Everything the page is handed, and therefore everything it can show."""

    entries: List[IndexEntry] = field(default_factory=list)
    count: int = 0
    ttl: str = ""
    # open says whether registration is unauthenticated.
    open: bool = False


def render_index(data: IndexData) -> str:
    """Render the page, escaping every value that goes into it."""
    out = [_PAGE_HEAD]
    out.append(
        "<p>%s live %s \u00b7 ttl %s%s</p>\n"
        % (
            _escape(data.count),
            "entry" if data.count == 1 else "entries",
            _escape(data.ttl),
            " \u00b7 registration is open" if data.open else "",
        )
    )
    if data.entries:
        out.append(_PAGE_ROWS)
        for e in data.entries:
            out.append(
                '<tr><td>%s</td><td>%s</td><td>%s</td><td class="num">%s</td><td>%s</td><td>%s</td></tr>\n'
                % (
                    _escape(e.model),
                    _escape(e.host),
                    _escape(e.address),
                    _escape(e.free),
                    _escape(e.digest),
                    _escape(e.age),
                )
            )
        out.append("</table>")
    else:
        out.append('<p class="quiet">Nobody is serving anything right now.</p>')
    out.append(_PAGE_FOOT)
    return "".join(out)


def _escape(value: Any) -> str:
    """One interpolated value, escaped.

    `quote=True` because the values land in attribute positions as readily as in
    text, and a quote that is not escaped is the shortest way out of a table cell.
    """
    return html.escape(str(value), quote=True)


def _free_cell(e: registry.Entry) -> str:
    """Say how many peer slots a host offered, or that it did not say. The page
    never invents a number: a host that reported nothing is not shown as busy,
    because being busy is a claim and saying nothing is not one.
    """
    slots, known = e.free_slots()
    if not known:
        return "unknown"
    return str(slots)


def _short_digest(digest: str) -> str:
    """Abbreviate a digest for a table cell. An empty digest reads as "unknown"
    rather than as blank, because blank suggests nothing to say and unknown is a
    thing the rest of Bothy treats as a fact: it never satisfies a pinned
    expectation.
    """
    d = model.normalize_digest(digest)
    if d == "":
        return "unknown"
    keep = 12
    if len(d) > len("sha256:") + keep:
        return d[: len("sha256:") + keep] + "\u2026"
    return d


def _age(seconds: float) -> str:
    """Render how long ago the last heartbeat was, which is the number worth
    showing: an entry is live or it is gone, and the question underneath is how
    close it is to expiring.
    """
    if seconds < 2:
        return "just now"
    if seconds < 60:
        return "%ds ago" % int(seconds)
    if seconds < 3600:
        return "%dm%ds ago" % (int(seconds // 60), int(seconds) % 60)
    return "%dh%dm ago" % (int(seconds // 3600), int(seconds // 60) % 60)
