"""The "share" side: it announces what a GPU can serve, meters who uses it, and
proxies requests to the engine that owns it.

It is a reverse proxy rather than a reimplementation. Everything Bothy does not
handle itself goes straight through to the engine, which is what keeps streaming,
token accounting and every OpenAI endpoint working without writing any protocol
code at all.

Go reaches for `httputil.ReverseProxy` here; this port speaks to the engine over
`http.client` directly, because the two promises that matter -- a body streamed
in both directions without being collected, and a key that never leaves for the
engine -- are exactly the parts a proxy library hides. The behaviour is the
Go's: the same allowlist, the same order of refusals, the same statuses, and the
same metering around the whole response.

**Go's `context.Context` has no counterpart here.** Cancellation is a
`threading.Event` handed to `Host.serve` (anything with `is_set()` will do, and
`Host.stop` asks for the same thing without one), and the per-request time limit
is a deadline the proxy applies to the socket it is reading from. There is no
per-call deadline on the engine lister; the engine's own client carries a
timeout, which is the whole of what the context did for it.
"""

from __future__ import annotations

import argparse
import hmac
import http.client
import io
import json
import logging
import socket
import sys
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

from . import engine, httpx, meter, model, registry
from .config import bool_ as _env_bool
from .config import dur as _env_dur
from .config import format_duration
from .config import int_ as _env_int
from .config import int64 as _env_int64
from .config import listen_default as _listen
from .config import parse_duration
from .config import text as _env_text
from .errors import BodyTooLarge, BothyError, ConfigError

_log = logging.getLogger("bothy.host")

# How often a wait that is waiting for something else looks at whether it should
# stop. Cancellation has no callback here, so a stop request is noticed within
# this rather than within a whole heartbeat.
_POLL = 0.05

# How long a connection to the engine may take to be established, and -- when the
# host has no time limit of its own -- how long a read may take. Go's
# DefaultTransport gives a dial 30 seconds and, once connected, no timeout at
# all: a generation legitimately takes minutes, and the only bound on one is
# `max_request_time`. Both halves are kept.
_DIAL_TIMEOUT = 30.0

# How much of a `/bothy/sharing` body is read. The endpoint is meant to be
# reachable from wherever the owner happens to be, so it is not a place to accept
# an unbounded body.
_SHARING_BODY_LIMIT = 4 << 10


# ---------------------------------------------------------------------------
# routes.go -- what the proxy will forward
# ---------------------------------------------------------------------------

# DefaultMaxBody is how large a proxied request body may be. It is generous on
# purpose: a prompt carrying images is legitimately megabytes, and the cap is
# there to bound a host's exposure, not to police prompts. Past it the host
# answers 413 rather than reading the rest.
DEFAULT_MAX_BODY = 32 << 20

# Bothy proxies a request it does not handle itself to the engine. It used to
# forward *every* path, which was a feature on a private network -- "every OpenAI
# and Ollama endpoint works the day it ships upstream" -- and is a liability on
# the open internet, where a host may run with no share key at all.
#
# The engine's own control routes are not inference. `DELETE /api/delete` removes
# the host's models, `POST /api/pull` fills its disk, `POST /api/create` writes
# new ones, and reaching them requires nothing but a port number when the host is
# open. That was demonstrated against a real Ollama: with a valid key the engine
# answered its own `404 model not found`, which means the request got there.
#
# So the proxy denies by default and allows the routes that are inference plus
# the read-only metadata a client needs to learn what a host serves.
#
# INFERENCE_ROUTES maps a path to the methods that may use it. A trailing slash
# means the whole subtree.
INFERENCE_ROUTES: Dict[str, Tuple[str, ...]] = {
    # OpenAI-shaped engines. Embeddings are inference too: they cost GPU time and
    # do not change the engine.
    "/v1/chat/completions": ("POST",),
    "/v1/completions": ("POST",),
    "/v1/embeddings": ("POST",),
    "/v1/models": ("GET",),
    "/v1/models/": ("GET",),
    # Ollama's native API: inference, plus the read-only routes a client uses to
    # see what is served and what is loaded. Deliberately absent: pull, push,
    # create, delete, copy, blobs, and anything else that writes to the host.
    "/api/chat": ("POST",),
    "/api/generate": ("POST",),
    "/api/embed": ("POST",),
    "/api/embeddings": ("POST",),
    "/api/tags": ("GET",),
    "/api/ps": ("GET",),
    "/api/show": ("POST",),
    "/api/version": ("GET",),
}


@dataclass(frozen=True)
class RouteRule:
    """One engine path a host chooses to proxy on top of the inference routes:
    "POST /api/pull", or "GET /api/blobs/" for a subtree."""

    method: str = ""
    path: str = ""


def parse_routes(spec: str) -> List[RouteRule]:
    """Read "path,METHOD path" into rules. A rule without a method applies to
    every method, which is what someone writing a bare path means.

    A typo is refused at startup rather than becoming a rule that matches nothing:
    an operator who wrote `POST api/pull` has a host that silently does not proxy
    what they opened, which is the failure this check exists to prevent.
    """
    out: List[RouteRule] = []
    for item in (spec or "").split(","):
        item = item.strip()
        if item == "":
            continue
        rule = RouteRule(path=item)
        method, sep, path = item.partition(" ")
        if sep:
            rule = RouteRule(method=method.strip().upper(), path=path.strip())
        if rule.path == "" or not rule.path.startswith("/"):
            raise ConfigError(
                'bad route "%s": want a path like /v1/chat/completions, optionally after a method' % item
            )
        out.append(rule)
    return out


def matches_routes(table: Dict[str, Sequence[str]], method: str, path: str) -> bool:
    """Whether method and path appear in an allowlist table."""
    for pattern, methods in table.items():
        if not matches_path(pattern, path):
            continue
        if method in methods:
            return True
    return False


def matches_path(pattern: str, path: str) -> bool:
    """Whether path is the pattern, or lives under it when it ends in a slash."""
    if pattern.endswith("/"):
        return path.startswith(pattern)
    return pattern == path


@dataclass
class Config:
    """Config describes one host."""

    listen: str = ""
    engine_url: str = ""
    engine_kind: str = ""
    discovery_url: str = ""
    register_token: str = ""
    # share_key is a single key, attributed to the peer named "default".
    share_key: str = ""
    # share_keys replaces share_key with per-peer keys, "alice:key,bob:key", so
    # that usage is attributed to a person instead of to everyone who shares a
    # key.
    share_keys: str = ""
    # public_address is what gets registered: the address a client should dial.
    # It has to be the address peers can actually reach, which is usually not the
    # address this process listens on.
    public_address: str = ""
    heartbeat: float = 0.0
    # max_concurrent caps requests in flight across the whole host. A GPU
    # serialises work anyway, so this is the limit that really protects it.
    # Zero means no cap.
    max_concurrent: int = 0
    # owner_reserve is how many of max_concurrent are kept for you, out of reach
    # of peers. One by default: sharing your machine should not mean losing it.
    owner_reserve: int = 0
    # peer_max_concurrent caps how many of the host's slots one peer may hold at
    # once. Without it the cap is first-come-first-served, so one client with
    # parallel requests takes the whole GPU while everybody else is told the host
    # is full. Zero means no separate cap.
    peer_max_concurrent: int = 0
    # max_request_time stops a single request at a wall-clock limit, which is the
    # only lever that bounds how long one generation can occupy the GPU. Zero
    # means no limit. A reply already streaming is cut off when the limit is
    # reached -- the caller sees a truncated response rather than a clean error,
    # because the headers are long gone by then.
    max_request_time: float = 0.0
    # peer_quota is a per-peer request budget, as "count/period" -- 200/1h. Empty
    # means no budget, and a peer may use the GPU all day a request at a time.
    peer_quota: str = ""
    # requests_per_minute caps one peer's request rate. Zero means no cap.
    requests_per_minute: int = 0
    # admin_key guards the /bothy/sharing control endpoint. Without it there is
    # no remote control at all. It is deliberately not a share key: peers hold
    # those, and a peer who can pause your host is worse than no control.
    admin_key: str = ""
    # paused starts the host refusing peers. It is what makes a schedule
    # possible -- pause and resume from cron -- without a stop that also drops the
    # registration.
    paused: bool = False
    # stream_usage asks the engine to report token usage on streamed replies, by
    # adding stream_options.include_usage to streamed OpenAI requests. Without it,
    # a host whose peers stream would meter nothing at all. See the injection
    # section below.
    stream_usage: bool = False
    # allow_all_routes proxies every path to the engine, which is what Bothy did
    # before it was built for the open internet. It is a deliberate choice for a
    # network you control: an engine's control routes are then reachable by
    # anyone who can reach this port. See the allowlist above.
    allow_all_routes: bool = False
    # allow_routes are extra engine paths this host proxies on top of the
    # inference routes, for an engine whose API Bothy does not know.
    allow_routes: List[RouteRule] = field(default_factory=list)
    # max_body caps a proxied request body in bytes. Zero means no cap, which is
    # only sane where every caller is known.
    max_body: int = 0
    engine: "engine.Options" = field(default_factory=engine.Options)

    def allows_route(self, method: str, path: str) -> bool:
        """Report whether the host proxies method and path to its engine.

        Denial is the default. The refusals are the point: on an open host this
        check is all that stands between a stranger and the host's models.
        """
        if self.allow_all_routes:
            return True
        if matches_routes(INFERENCE_ROUTES, method, path):
            return True
        for rule in self.allow_routes:
            if rule.method != "" and rule.method != method:
                continue
            if rule.path == path or (rule.path.endswith("/") and path.startswith(rule.path)):
                return True
        return False


# ---------------------------------------------------------------------------
# peers.go -- the key a request presents, resolved to a name
# ---------------------------------------------------------------------------


class Peers:
    """Resolves the key a request presents to a name.

    Names matter because usage is only actionable per person: "someone used two
    million tokens" tells you nothing, "alice used two million tokens" does. A
    host with no keys configured still meters, attributing usage to the caller's
    address, so limits and accounting apply even in the open case.
    """

    def __init__(self, by_key: Optional[Dict[str, str]] = None) -> None:
        self.by_key: Dict[str, str] = dict(by_key or {})

    def open(self) -> bool:
        """Whether the port is unauthenticated."""
        return len(self.by_key) == 0

    def resolve(self, req: httpx.Request) -> Tuple[str, bool]:
        """Return which peer a request belongs to, and whether it is allowed in."""
        if self.open():
            return "addr:" + caller_host(req), True
        presented = httpx.token_from(req.headers, httpx.KEY_HEADER)
        # Constant-time per key, so a wrong guess does not say how much of it was
        # right. The map is the host's own configuration, not the attacker's, so
        # walking it leaks nothing beyond how many peers there are.
        for key, name in self.by_key.items():
            if hmac.compare_digest(presented.encode("utf-8"), key.encode("utf-8")):
                return name, True
        return "", False


def parse_peers(spec: str) -> Peers:
    """Accept "alice:key1,bob:key2". Keys may themselves contain colons; the
    name is everything before the first one."""
    by_key: Dict[str, str] = {}
    for item in (spec or "").split(","):
        item = item.strip()
        if item == "":
            continue
        name, sep, key = item.partition(":")
        name, key = name.strip(), key.strip()
        if sep == "" or name == "" or key == "":
            raise ConfigError('bad share key "%s": want name:key' % item)
        if key in by_key:
            raise ConfigError('the key for "%s" is already used by another peer' % name)
        by_key[key] = name
    if len(by_key) == 0:
        raise ConfigError('no share keys found in "%s"' % spec)
    return Peers(by_key)


def resolve_peers(named: str, single: str) -> Peers:
    """Prefer named keys, fall back to a single key, and finally to metering by
    address."""
    if (named or "").strip() != "":
        return parse_peers(named)
    if (single or "").strip() != "":
        return Peers({single: "default"})
    return Peers()


def caller_host(req: httpx.Request) -> str:
    """The address a request came from, without its port.

    Go splits `r.RemoteAddr`; anything that is not host:port is left as it is
    rather than dropped, because an identity of some kind is what keeps an open
    host's limits per-caller.
    """
    host, _port, ok = _split_host_port(req.remote or "")
    if ok:
        return host
    return req.remote or ""


def with_peer(req: httpx.Request, name: str) -> httpx.Request:
    """Attribute a request to a peer and return it.

    Go threads the name through `context.WithValue`; the request object already
    travels with the request here, so `peer` on it is that carrier.
    """
    req.peer = name
    return req


def peer_from(req: httpx.Request) -> str:
    """The peer resolved by the host's authentication step."""
    return req.peer


# ---------------------------------------------------------------------------
# inject.go -- asking the engine for usage on streamed replies
# ---------------------------------------------------------------------------
#
# Streamed replies are where the meter goes blind.
#
# A whole response reports its token usage and the sniffer reads it, so nothing
# has to be asked for. A stream only reports usage if the engine decides to put it
# in the stream, and an OpenAI-compatible engine generally will not unless the
# request asks for it:
#
#     "stream_options": {"include_usage": true}
#
# So a host whose peers all stream could serve for hours and show nothing, and
# streaming is how interactive use arrives.
#
# The host asks on the caller's behalf. This is the one place a proxy in Bothy
# rewrites a request body, and it is deliberately narrow: two routes, only when
# the caller asked to stream, and never when the caller has already said anything
# about stream_options. An explicit choice is never overridden.
#
# The cost is that the body has to be read before it is forwarded, where the rest
# of the proxy streams it through untouched. MAX_INJECTABLE bounds that: a body
# larger than this (a prompt carrying images, say) is forwarded as it is and
# simply lands in the meter as unmetered, which is what would have happened
# anyway.

# MAX_INJECTABLE is how much of a request body will be read in order to add two
# short fields to it. Past this the request is forwarded untouched.
MAX_INJECTABLE = 1 << 20

STREAM_OPTIONS_FIELD = "stream_options"

# STREAMING_ROUTES are the OpenAI routes that can stream, and the only ones this
# touches. The engine's native API has its own shape -- Ollama spells the same
# idea differently and reports usage without being asked -- and Bothy does not
# guess at a body it has not been taught.
STREAMING_ROUTES = frozenset({"/v1/chat/completions", "/v1/completions"})


def wants_stream_usage(req: httpx.Request) -> bool:
    """Whether this request is one the host should ask the engine for usage on."""
    return req.method == "POST" and req.path in STREAMING_ROUTES


def add_stream_usage(body: bytes) -> Tuple[bytes, bool]:
    """Return body with stream_options.include_usage set, and whether it changed
    anything. It is a pure function of the body, so the decision table is testable
    without a server.

    Go re-marshals a map of raw JSON values, so a nested object the host did not
    write keeps its bytes exactly. Python's JSON module has no raw-value type, so
    the whole body is parsed and re-encoded -- compactly, and with the keys in a
    stable order. The decision table, the field added, and the surviving fields
    are the Go's; only the whitespace and key order inside a body the engine reads
    as JSON are not.
    """
    try:
        parsed: Any = json.loads(body)
    except ValueError:
        return body, False  # not JSON, so not ours to touch
    if not isinstance(parsed, dict):
        return body, False  # JSON, but not an object
    stream = parsed.get("stream")
    # A stream field that is absent, or not a boolean, is not a request to
    # stream: Go's decode into a bool refuses "yes" and null exactly as this
    # does, and a zero value there means the field was not understood.
    if not isinstance(stream, bool) or not stream:
        return body, False
    if STREAM_OPTIONS_FIELD in parsed:
        return body, False  # the caller has an opinion; leave it alone
    parsed[STREAM_OPTIONS_FIELD] = {"include_usage": True}
    try:
        rewritten = json.dumps(parsed, separators=(",", ":"), sort_keys=True, ensure_ascii=False).encode("utf-8")
    except (TypeError, ValueError):
        return body, False
    return rewritten, True


class _PrefixedReader:
    """A reader that serves bytes already read, then the stream they came from.

    Go puts the body back together with `io.MultiReader(bytes.NewReader(body),
    original)` and keeps the original as the Closer. There is nothing to close
    here -- the request body is the connection, and the server owns it -- so this
    is the multi-reader half on its own.
    """

    __slots__ = ("_prefix", "_rest")

    def __init__(self, prefix: bytes, rest: Any) -> None:
        self._prefix = prefix
        self._rest = rest

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            rest = self._rest.read() if self._rest is not None else b""
            out, self._prefix = self._prefix + (rest or b""), b""
            return out
        if not self._prefix:
            return self._rest.read(size)
        if size <= len(self._prefix):
            out, self._prefix = self._prefix[:size], self._prefix[size:]
            return out
        out, self._prefix = self._prefix, b""
        return out + self._rest.read(size - len(out))

    def readline(self, size: int = -1) -> bytes:
        """A line, spanning the boundary if it has to.

        A chunked request body is read a line at a time, and a line split across
        the buffered prefix and the socket would otherwise be read as two.
        """
        if not self._prefix:
            return self._rest.readline(size)
        index = self._prefix.find(b"\n")
        if index >= 0:
            line, self._prefix = self._prefix[: index + 1], self._prefix[index + 1 :]
            return line if size is None or size < 0 else line[:size]
        out, self._prefix = self._prefix, b""
        if size is not None and size >= 0:
            left = max(size - len(out), 0)
            return out + (self._rest.readline(left) if left else b"")
        return out + self._rest.readline()


def inject_stream_usage(req: httpx.Request) -> bool:
    """Read and rewrite req's body when the request is one that should ask for
    streamed usage. It reports whether the body changed.

    A chunked body is left alone. Go's net/http de-chunks a request body before
    the handler ever sees it, so a Go host can rewrite one and put it back; here
    the body is still framed on the socket, and re-framing a partly read stream
    to add two fields is not worth corrupting a prompt for. The outcome is the
    one the Go comment already describes for a body the host declines to touch:
    the reply is metered as unreported, which is where it was headed anyway.
    """
    if not wants_stream_usage(req) or req.chunked:
        return False
    if req.content_length > MAX_INJECTABLE:
        return False  # too big to buffer, so forward it as it stands

    body = bytearray()
    try:
        for chunk in req.body_chunks():
            body += chunk
            if len(body) > MAX_INJECTABLE:
                break
    except OSError:
        # Part-read bodies cannot be put back together from here, so this request
        # is forwarded with what was read and the engine will report the error.
        req.raw = _PrefixedReader(bytes(body), req.raw)
        return False

    if len(body) > MAX_INJECTABLE:
        # A half-read body would be a worse bug than an unmetered reply, so the
        # bytes that were read are served back ahead of the rest.
        req.raw = _PrefixedReader(bytes(body), req.raw)
        return False

    rewritten, changed = add_stream_usage(bytes(body))
    if not changed:
        req.raw = _PrefixedReader(bytes(body), req.raw)
        return False

    req.raw = io.BytesIO(rewritten)
    req.content_length = len(rewritten)
    # The engine has to be told the new length, or it reads a truncated body and
    # answers with a parse error that mentions nothing about metering. Go's
    # `Header.Set` replaces the header; `Message.__setitem__` would add a second
    # one and leave the first -- which is the one the engine would read.
    try:
        req.headers.replace_header("Content-Length", str(len(rewritten)))
    except (KeyError, AttributeError, TypeError):
        try:
            req.headers["Content-Length"] = str(len(rewritten))
        except (AttributeError, TypeError):
            pass
    return True


# ---------------------------------------------------------------------------
# The engine proxy
# ---------------------------------------------------------------------------

# What a forwarded request must not carry: the share key is the peers' secret and
# the engine has no business seeing it, an Authorization header is either that
# key in another spelling or a secret of the caller's, and the framing headers are
# replaced by whatever the forwarded body actually is.
_DROPPED_HEADERS = ("Host", httpx.KEY_HEADER, "Authorization", "Content-Length", "Transfer-Encoding")

# Methods that carry a body by convention. Go's transport sends
# `Content-Length: 0` for these when the body is empty, which is what stops an
# engine waiting for a body that is never coming.
_BODY_METHODS = frozenset({"POST", "PUT", "PATCH"})


class ProxyError(BothyError):
    """A proxied request that could not be answered, carrying the status that
    says why.

    Go reaches these through the reverse proxy's ErrorHandler, which is told the
    error and picks the status. A request stopped by the host's own time limit is
    a 504 -- not an unreachable engine, and saying so would send the caller
    looking for the fault in the wrong place -- and anything else is a 502.
    """

    def __init__(self, status: int, message: str) -> None:
        self.status = status
        super().__init__(message)


def _cut_connection(resp: httpx.Response) -> None:
    """End a response without finishing it.

    The head is already on the wire -- a stream that hit the host's time limit
    mid-body has no status left to change -- so the only honest signal left is a
    connection that stops mid-stream, which is what a caller sees as a truncated
    response rather than a clean early end. Closing the connection is the flag the
    server reads after this handler returns; there is no public way to ask for it,
    because a response that ends properly never needs one.
    """
    handler = getattr(resp, "_h", None)
    if handler is not None:
        handler.close_connection = True


class EngineProxy:
    """Forwards unhandled requests to the engine, streaming both ways.

    Go's `httputil.ReverseProxy`, for the one engine a host serves: the same
    Director decisions (the engine's own Host, no key, no Authorization), the same
    immediate flush on every read, and the same error handler. One connection per
    request rather than a pool: a pool would be the first thing to reach into a
    proxy library for, and one generation per connection is what the GPU does
    anyway.
    """

    def __init__(self, target: urllib.parse.SplitResult) -> None:
        self.scheme = (target.scheme or "http").lower()
        netloc = target.netloc
        if "@" in netloc:
            netloc = netloc.rpartition("@")[2]
        # The Host header is the engine's own name, port and all, exactly as the
        # engine URL spelled it.
        self.host = netloc
        try:
            self.hostname = target.hostname or ""
            self.port = target.port
        except ValueError as err:
            raise ConfigError("engine URL %r: %s" % (target.geturl(), err)) from None
        if self.port is None:
            self.port = 443 if self.scheme == "https" else 80
        self.base_path = target.path or ""
        self.base_query = target.query or ""

    def serve(
        self, req: httpx.Request, resp: httpx.Response, deadline: Optional[float], limit: int = 0
    ) -> Optional[meter.Sniffer]:
        """Forward one request and stream the answer back.

        `limit` bounds the request body in bytes as it is streamed out, which is
        the half of `max_body` that catches a client which lied about its length
        or sent a chunked body nobody counted. Zero means no bound, which the
        caller has already decided is safe.

        Returns the sniffer watching the response, when there was one, so the
        caller can meter what the whole reply cost after it has been streamed --
        the slot is held for the duration, which is the point.
        """
        if self.scheme not in ("http", "https"):
            raise ProxyError(502, "engine unreachable: unsupported protocol scheme %r" % self.scheme)
        conn = self._connect(deadline)
        upstream = None
        try:
            self._send(conn, req, deadline, limit)
            upstream = self._read_response(conn, deadline)
            # The sniffer is wrapped around the body the engine is sending, so
            # every byte still passes through untouched and nothing is held back.
            # Only a 200 is sniffed: anything else is not a completion and is
            # recorded as unmetered rather than as a response that cost nothing.
            reader = _Stream(upstream)
            sniffer: Optional[meter.Sniffer] = None
            if upstream.status == 200:
                sniffer = meter.Sniffer(reader, upstream.headers.get("Content-Type"))
                source: Any = sniffer
            else:
                source = reader
            headers = httpx.end_to_end(upstream.headers)
            resp.send_stream(
                upstream.status,
                self._chunks(source, conn, deadline),
                headers=headers,
                content_length=upstream.length,
            )
            return sniffer
        finally:
            if upstream is not None:
                try:
                    upstream.close()
                except OSError:
                    pass
            try:
                conn.close()
            except OSError:
                pass

    def _connect(self, deadline: Optional[float]) -> http.client.HTTPConnection:
        """Open the connection, reporting a refusal here rather than on the first
        write, so "engine unreachable" names the dial and not the body."""
        try:
            if self.scheme == "https":
                conn: http.client.HTTPConnection = http.client.HTTPSConnection(
                    self.hostname, self.port, timeout=_DIAL_TIMEOUT
                )
            else:
                conn = http.client.HTTPConnection(self.hostname, self.port, timeout=_DIAL_TIMEOUT)
            conn.connect()
        except socket.timeout as err:
            raise self._failed(deadline, err) from None
        except (OSError, http.client.HTTPException) as err:
            raise ProxyError(502, "engine unreachable: %s" % err) from None
        return conn

    def _send(
        self, conn: http.client.HTTPConnection, req: httpx.Request, deadline: Optional[float], limit: int = 0
    ) -> None:
        """Write the request head and stream the body out."""
        conn.putrequest(req.method, self._path(req), skip_host=True, skip_accept_encoding=True)
        conn.putheader("Host", self.host)
        for key, value in httpx.end_to_end(req.headers, drop=_DROPPED_HEADERS):
            conn.putheader(key, value)
        chunked = False
        if req.content_length > 0:
            conn.putheader("Content-Length", str(req.content_length))
        elif req.chunked:
            chunked = True
            conn.putheader("Transfer-Encoding", "chunked")
        elif req.method in _BODY_METHODS:
            conn.putheader("Content-Length", "0")
        try:
            conn.endheaders()
            for chunk in _bounded(req.body_chunks(), limit):
                if not chunk:
                    continue
                # A body write is bounded by the limit for the same reason a read
                # is: an engine that has stopped reading must not hold the peer's
                # slot for longer than the host said one request may take.
                self._apply_deadline(conn, deadline)
                if chunked:
                    conn.send(b"%x\r\n" % len(chunk) + chunk + b"\r\n")
                else:
                    conn.send(chunk)
            if chunked:
                conn.send(b"0\r\n\r\n")
        except socket.timeout as err:
            raise self._failed(deadline, err, armed=deadline is not None) from None
        except (OSError, http.client.HTTPException) as err:
            raise ProxyError(502, "engine unreachable: %s" % err) from None

    def _read_response(self, conn: http.client.HTTPConnection, deadline: Optional[float]) -> http.client.HTTPResponse:
        self._apply_deadline(conn, deadline)
        try:
            return conn.getresponse()
        except socket.timeout as err:
            raise self._failed(deadline, err, armed=deadline is not None) from None
        except (OSError, http.client.HTTPException) as err:
            raise ProxyError(502, "engine unreachable: %s" % err) from None

    def _chunks(self, source: Any, conn: http.client.HTTPConnection, deadline: Optional[float]) -> Iterator[bytes]:
        """Read the engine's answer a chunk at a time, so it arrives as it is
        produced rather than in one lump at the end."""
        while True:
            self._apply_deadline(conn, deadline)
            try:
                chunk = source.read(httpx.CHUNK)
            except socket.timeout as err:
                raise self._failed(deadline, err, armed=deadline is not None) from None
            except (OSError, http.client.HTTPException) as err:
                raise ProxyError(502, "engine unreachable: %s" % err) from None
            if not chunk:
                return
            yield chunk

    def _apply_deadline(self, conn: http.client.HTTPConnection, deadline: Optional[float]) -> None:
        """Bound the next read by what is left of the request's time.

        Go puts the deadline on the request context and the transport stops the
        read when it expires. A socket timeout is the same lever one layer down,
        and it is re-armed before every read so a long generation is measured from
        where it started rather than from where the last chunk arrived.
        """
        sock = getattr(conn, "sock", None)
        if sock is None:
            return
        if deadline is None:
            sock.settimeout(None)
            return
        left = deadline - time.monotonic()
        if left <= 0:
            raise self._failed(deadline, None, armed=True)
        sock.settimeout(left)

    def _failed(self, deadline: Optional[float], err: Optional[BaseException], armed: bool = False) -> ProxyError:
        """The refusal for a read or write that did not arrive.

        A deadline that has passed is the host's own limit doing its job: 504, so
        the caller does not go looking for the fault in the wrong place. `armed`
        says the socket's own timeout was set from that deadline a moment ago,
        which is what makes a timeout it raised the deadline rather than a slow
        engine: a timeout is not guaranteed to fire a hair *after* the instant it
        was given, and one that fired a millisecond early is still the limit.
        """
        if armed or (deadline is not None and time.monotonic() >= deadline):
            return ProxyError(504, "the host's time limit for one request was reached, so the request was stopped")
        if err is None:
            return ProxyError(504, "the host's time limit for one request was reached, so the request was stopped")
        return ProxyError(502, "engine unreachable: %s" % err)

    def _path(self, req: httpx.Request) -> str:
        """The engine path for this request: its base path, the request's, and
        both query strings, the way Go's proxy joins them."""
        path = _join_path(self.base_path, req.path)
        query = urllib.parse.urlencode(req.query, doseq=True)
        if self.base_query == "" or query == "":
            query = query + self.base_query
        else:
            query = query + "&" + self.base_query
        return path + ("?" + query if query else "")


class _Stream:
    """Reads an upstream body as it arrives, rather than in one lump at the end.

    `http.client`'s `read(n)` waits until it has n bytes or the body is over, so a
    response read that way reaches the client only once the engine has finished
    -- which is exactly what a proxy must not do to a stream. `read1` returns
    whatever one read produced, which is what Go's `io.CopyBuffer` gets from the
    transport, and it understands a chunked body (Python 3.9 and later).

    A response object without `read1` falls back to `read`: a body with a declared
    length still ends where it says it does, and a whole response is identical
    either way.
    """

    __slots__ = ("_response", "_read1")

    def __init__(self, response: Any) -> None:
        self._response = response
        self._read1 = getattr(response, "read1", None)

    def read(self, size: int = -1) -> bytes:
        if self._read1 is not None:
            return self._read1(size)
        return self._response.read(size)

    def close(self) -> None:
        return self._response.close()


def _bounded(chunks: Iterator[bytes], limit: int) -> Iterator[bytes]:
    """A body that is refused once it has passed `limit` bytes.

    Go wraps the body in `http.MaxBytesReader`, which is the half of the limit
    that catches a client who declared a small length and sent a large body, or
    sent a chunked one nobody declared. The count is on the bytes that actually
    arrived, and the request is refused before the rest of it is read.
    """
    if limit <= 0:
        yield from chunks
        return
    total = 0
    for chunk in chunks:
        total += len(chunk)
        if total > limit:
            raise BodyTooLarge("request body is larger than %d bytes" % limit)
        yield chunk


def _join_path(base: str, path: str) -> str:
    """Join two URL paths without doubling or dropping the slash between them."""
    base_slash = base.endswith("/")
    path_slash = path.startswith("/")
    if base_slash and path_slash:
        return base + path[1:]
    if not base_slash and not path_slash:
        return base + "/" + path
    return base + path


def _parse_engine_url(text: str) -> urllib.parse.SplitResult:
    """Parse an engine URL the way `New` has to before it can proxy anywhere.

    Go parses the URL and defaults the scheme to http. It does not check the host,
    which is why a typo in it turns up as a per-request dial failure; a host that
    cannot dial its engine is a misconfiguration, so it is refused here instead.
    """
    raw = (text or "").strip()
    if raw.startswith("://"):
        raise ConfigError('engine URL "%s": missing protocol scheme' % text)
    try:
        target = urllib.parse.urlsplit(raw)
    except ValueError as err:
        raise ConfigError('engine URL "%s": %s' % (text, err)) from None
    if target.scheme == "":
        target = target._replace(scheme="http")
    if target.netloc == "":
        raise ConfigError('engine URL "%s": missing host' % text)
    return target


# ---------------------------------------------------------------------------
# host.go -- the host itself
# ---------------------------------------------------------------------------


class Host:
    """Announces an engine's models, meters usage, and proxies to the engine."""

    def __init__(self, cfg: Config, lister: engine.Lister, log: logging.Logger) -> None:
        self.cfg = cfg
        self.engine = lister
        self.log = log
        self.name = hostname()
        self._peers = resolve_peers(cfg.share_keys, cfg.share_key)
        self._meter = meter.Meter(
            meter.Options(
                max_concurrent=cfg.max_concurrent,
                owner_reserve=cfg.owner_reserve,
                peer_max_concurrent=cfg.peer_max_concurrent,
                peer_quota=parse_quota(cfg.peer_quota),
                requests_per_minute=cfg.requests_per_minute,
            )
        )
        self._models: Optional[List[model.Model]] = None
        self._models_lock = threading.Lock()
        # paused is the owner's hand on the tap. paused_at is when it last went
        # on, zero when it is off.
        self._paused = threading.Event()
        self._paused_at = 0.0
        self._paused_lock = threading.Lock()
        # wake asks the announce loop to re-announce now rather than at the next
        # heartbeat, so resuming does not leave clients waiting one out. It is
        # coalescing: two nudges before the loop looks are one announcement.
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._server: Optional[httpx.Server] = None
        self._proxy = EngineProxy(_parse_engine_url(cfg.engine_url))
        self._disc: Optional[registry.Client] = None
        if cfg.discovery_url != "":
            self._disc = registry.Client(cfg.discovery_url, cfg.register_token)
        if cfg.paused:
            self._paused.set()
            self._paused_at = time.time()

    # -- lifecycle ---------------------------------------------------------

    def handler(self) -> httpx.Handler:
        """Return the host's routes.

        Health is unauthenticated so a container healthcheck needs no key;
        everything else is attributed to a peer.
        """
        return httpx.log_requests(self.log, self._root)

    def serve(self, ctx: Any = None) -> None:
        """Run the proxy and the announce loop until ctx is cancelled or `stop`
        is called.

        Go's `Serve(ctx)`, with the context replaced by anything that answers
        `is_set()` -- a `threading.Event` is the usual one, and `None` means
        nobody will ask, so it serves until `stop` is called or the process is
        interrupted. Failures are raised rather than returned, the way every
        Python module here reports them.
        """
        self.describe_limits()
        self.log.info(
            "sharing listen=%s engine=%s kind=%s discovery=%s address=%s",
            self.cfg.listen,
            self.cfg.engine_url,
            self.engine.kind(),
            self.cfg.discovery_url,
            self.cfg.public_address,
        )
        announcer = threading.Thread(target=self._announce_loop, args=(ctx,), name="bothy-announce", daemon=True)
        announcer.start()
        server = httpx.Server(self.cfg.listen, self.handler(), self.log)
        self._server = server
        self.log.info("listening addr=%s", server.addr)
        server.start()
        try:
            # `None` means nobody outside will ask, so the loop below is what
            # serves: it polls rather than blocking on the server thread, because
            # `stop` is the other way to end a host and it has to be noticed.
            while not self._stopping(ctx):
                time.sleep(_POLL)
        except KeyboardInterrupt:
            # A person asking to stop is not a failure.
            pass
        finally:
            server.shutdown()

    def stop(self) -> None:
        """Ask the serve loop and the announce loop to stop, without a context.

        Idempotent, and safe from another thread -- which is how a test or a
        supervising process asks a host to finish rather than leaving it to an
        exception.
        """
        self._stop.set()

    def _stopping(self, ctx: Any) -> bool:
        if self._stop.is_set():
            return True
        return ctx is not None and ctx.is_set()

    def describe_limits(self) -> None:
        """Say which limits are in force, and which are not.

        The limits a host is *not* enforcing are the ones worth saying out loud: a
        silent default is how somebody ends up giving their GPU away without
        meaning to.
        """
        if self._peers.open():
            self.log.warning("no share key set: anyone who can reach this port can use your GPU, metered by address")
        elif len(self._peers.by_key) == 1:
            self.log.info("share key required peers=1")
        else:
            self.log.info("per-peer share keys required peers=%d", len(self._peers.by_key))
        if self.cfg.max_concurrent <= 0:
            self.log.warning("no concurrency cap: a single peer can occupy the GPU indefinitely")
        if self.cfg.owner_reserve <= 0:
            self.log.info("no slots kept back for you: peers may use every slot the cap allows")
        elif self.cfg.max_concurrent <= 0:
            self.log.warning("owner-reserve has no effect without a concurrency cap owner_reserve=%d", self.cfg.owner_reserve)
        else:
            self.log.info(
                "slots kept for you owner_reserve=%d peer_slots=%d",
                self.cfg.owner_reserve,
                self._meter.peer_slots(),
            )
        quota = self._meter.quota()
        if quota.enabled():
            self.log.info("per-peer budget requests=%d window=%s", quota.requests, format_duration(quota.window))
        else:
            self.log.info("no per-peer budget: a peer may use the GPU all day, a request at a time")
        if self.cfg.peer_max_concurrent > 0:
            self.log.info("per-peer slot cap slots=%d", self.cfg.peer_max_concurrent)
        elif self.cfg.max_concurrent > 0:
            self.log.warning(
                "no per-peer slot cap: one peer may hold every peer slot the host has peer_slots=%d "
                "hint=set -peer-max-concurrent to share the GPU between callers",
                self._meter.peer_slots(),
            )
        if self.cfg.max_request_time > 0:
            self.log.info("time limit per request max_request_time=%s", format_duration(self.cfg.max_request_time))
        else:
            self.log.info("no time limit per request: one generation may hold the GPU for as long as it likes")
        if self.cfg.max_body > 0:
            self.log.info("largest request body max_body=%d", self.cfg.max_body)
        else:
            self.log.warning("no request body limit: a peer may stream a body until the host runs out of disk or memory")
        if self.cfg.allow_all_routes:
            self.log.warning(
                "proxying every engine route, including the engine's own control routes: "
                "a peer can reach /api/delete and /api/pull"
            )
        if self.cfg.requests_per_minute <= 0:
            self.log.info("no per-peer request rate cap")
        if self.cfg.admin_key == "":
            self.log.info("no admin key: sharing cannot be paused remotely")
        if self._paused.is_set():
            self.log.warning("starting paused: peers are refused until you resume")

    # -- routing -----------------------------------------------------------

    def _root(self, req: httpx.Request, resp: httpx.Response) -> None:
        """The root mux: the two routes that are not a peer's request, and
        everything else.

        Go registers "GET /bothy/healthz" and "POST /bothy/sharing" and lets a
        catch-all take the rest, falling back to it when a path matches but the
        method does not -- so POST /bothy/healthz is a proxy request, not a 405.
        The methods are matched here for that reason, and HEAD with GET because
        that is what Go's mux does for a GET pattern.
        """
        if req.method in ("GET", "HEAD") and req.path == "/bothy/healthz":
            self._handle_health(req, resp)
            return
        # The control endpoint sits outside the share-key middleware because it
        # answers to the admin key instead. Share keys are handed to peers, and a
        # peer who can stop your host is worse than no control at all.
        if req.method == "POST" and req.path == "/bothy/sharing":
            self._handle_sharing(req, resp)
            return
        self._authenticated(req, resp)

    def _authenticated(self, req: httpx.Request, resp: httpx.Response) -> None:
        """Resolve the presenting key to a peer and pass it down. The meter needs
        a name, so this runs before anything that counts."""
        peer, ok = self._peers.resolve(req)
        if not ok:
            resp.error(401, "missing or invalid key")
            return
        with_peer(req, peer)
        self._api(req, resp)

    def _api(self, req: httpx.Request, resp: httpx.Response) -> None:
        """The routes that belong to an identified peer."""
        if req.method in ("GET", "HEAD") and req.path == "/bothy/models":
            self._handle_models(req, resp)
            return
        if req.method in ("GET", "HEAD") and req.path == "/bothy/usage":
            self._handle_usage(req, resp)
            return
        self._handle_proxy(req, resp)

    # -- the proxy ---------------------------------------------------------

    def _handle_proxy(self, req: httpx.Request, resp: httpx.Response) -> None:
        """Where the meter, the limiter and the engine proxy meet.

        The slot is taken before the request goes out and released only once the
        whole response has been streamed, so a slow completion counts against its
        peer for as long as it occupies the GPU.
        """
        peer = peer_from(req)
        start = time.time()

        # The allowlist is checked first. It is a policy this host chose rather
        # than a resource it is short of, so a request it will not proxy must not
        # spend anyone's budget, and must not reach the engine. 404 rather than
        # 403: a path this host does not proxy should be indistinguishable from
        # one the engine does not have, which is also all a probing peer should
        # learn.
        if not self.cfg.allows_route(req.method, req.path):
            self.log.warning("refused peer=%s reason=route not proxied method=%s path=%s", peer, req.method, req.path)
            resp.error(
                404,
                "this host proxies inference routes only, and %s %s is not one of them" % (req.method, req.path),
            )
            return

        # Paused is checked before the meter, on purpose. The refusal is not the
        # peer's doing, so it must not count against their budget, and 503 with a
        # reason says "not now" rather than the 429 that means "too fast" -- which
        # is also the one answer a client should treat as try-somewhere-else.
        if self._paused.is_set():
            self.log.info("refused peer=%s reason=paused path=%s", peer, req.path)
            resp.error(503, "the owner has paused sharing; this host is not serving peers right now")
            return

        # A body nobody bounded is a body that arrives until the disk is full.
        # The declared length is checked first because it is free and exact for an
        # honest client; the proxy then bounds one that lies about it or arrives
        # chunked, as it streams.
        if self.cfg.max_body > 0 and req.content_length > self.cfg.max_body:
            self.log.warning("refused peer=%s reason=body too large bytes=%d", peer, req.content_length)
            resp.error(
                413,
                "request body is %d bytes; this host accepts up to %d" % (req.content_length, self.cfg.max_body),
            )
            return

        # The wall-clock limit is the only thing that bounds how long one
        # generation can hold the GPU, and it starts before the meter so a request
        # that runs away is still counted, and still released, when it ends.
        deadline = None
        if self.cfg.max_request_time > 0:
            deadline = time.monotonic() + self.cfg.max_request_time

        try:
            self._meter.begin(peer, start)
        except meter.LimitError as err:
            headers: List[Tuple[str, str]] = []
            if err.retry_after > 0:
                # Whole seconds, rounded the way Go rounds a Duration.
                headers.append(("Retry-After", str(int(err.retry_after + 0.5))))
            self.log.warning("refused peer=%s reason=%s path=%s", peer, err.reason, req.path)
            resp.error(429, str(err), headers=headers)
            return

        sniffer: Optional[meter.Sniffer] = None
        try:
            # Ask for streamed usage before the body goes out. Without this a
            # streamed reply arrives with no token counts and can only be metered
            # as unreported, which is most interactive use.
            if self.cfg.stream_usage:
                inject_stream_usage(req)
            sniffer = self._proxy.serve(req, resp, deadline, self.cfg.max_body)
        except ProxyError as err:
            if resp.sent:
                _cut_connection(resp)
            else:
                resp.error(err.status, str(err))
        except BodyTooLarge as err:
            # The client declared one length and sent another, or sent a chunked
            # body nobody counted. Nothing has been sent, because the length was
            # what was being read.
            if resp.sent:
                _cut_connection(resp)
            else:
                resp.error(413, str(err))
        finally:
            usage = meter.Usage()
            reported = False
            response_bytes = 0
            if sniffer is not None:
                usage, reported = sniffer.usage()
                response_bytes = sniffer.bytes()
            # Released after the whole response has been streamed, so a slow
            # completion counts against its peer for as long as it occupies the
            # GPU -- and released however the request ended, so an engine outage
            # does not quietly fill the host's cap.
            self._meter.end(peer, usage, reported, response_bytes, time.time())
            duration = "%dms" % round((time.time() - start) * 1000)
            if reported:
                self.log.info(
                    "served peer=%s prompt_tokens=%d completion_tokens=%d duration=%s",
                    peer,
                    usage.prompt_tokens,
                    usage.completion_tokens,
                    duration,
                )
            else:
                self.log.info("served peer=%s tokens=unreported duration=%s", peer, duration)

    # -- the host's own routes ---------------------------------------------

    def _handle_health(self, req: httpx.Request, resp: httpx.Response) -> None:
        payload: Dict[str, Any] = {
            "ok": True,
            "host": self.name,
            "engine_kind": self.engine.kind(),
            "address": self.cfg.public_address,
            "model_count": len(self.current_models() or ()),
            "discovery": self.cfg.discovery_url,
            "key_required": not self._peers.open(),
        }
        payload.update(self._state())
        resp.json(200, payload)

    def _state(self) -> Dict[str, Any]:
        """The limits and the load, which health and usage both report."""
        return {
            "in_flight": self._meter.in_flight(),
            "free": self._meter.free_slots(),
            "max_concurrent": self.cfg.max_concurrent,
            "owner_reserve": self.cfg.owner_reserve,
            "peer_slots": self._meter.peer_slots(),
            "peer_max_concurrent": self.cfg.peer_max_concurrent,
            "requests_per_minute": self.cfg.requests_per_minute,
            "peer_quota": self.cfg.peer_quota,
            "max_request_time": format_duration(self.cfg.max_request_time),
            "max_body": self.cfg.max_body,
            "routes": self.routes_mode(),
            "paused": self._paused.is_set(),
        }

    def routes_mode(self) -> str:
        """What the proxy will forward, so an operator can see the answer to "can
        a peer reach my engine's own control routes?" without reading flags back
        out of their shell history."""
        if self.cfg.allow_all_routes:
            return "all"
        if len(self.cfg.allow_routes) > 0:
            return "inference+extra"
        return "inference"

    def _handle_models(self, req: httpx.Request, resp: httpx.Response) -> None:
        """Report what this host serves, with digests. A client pointed straight
        at an address uses this to learn what it can verify."""
        payload: Dict[str, Any] = {
            "host": self.name,
            "address": self.cfg.public_address,
            "free": self._meter.free_slots(),
            # null rather than [] until the engine has been listed once: the
            # difference is "nobody has asked the engine yet" against "the engine
            # serves nothing", which is what Go's nil slice says.
            "models": _models_json(self.current_models()),
        }
        resp.json(200, payload)

    def _handle_usage(self, req: httpx.Request, resp: httpx.Response) -> None:
        """The answer to "who is using my GPU?"."""
        payload: Dict[str, Any] = {"host": self.name, "address": self.cfg.public_address}
        payload.update(self._state())
        payload["peers"] = [_usage_row(row) for row in self._meter.snapshot()]
        resp.json(200, payload)

    def _handle_sharing(self, req: httpx.Request, resp: httpx.Response) -> None:
        """Pause and resume sharing, so that "not right now" does not have to mean
        stopping the process. Stopping works, but it also drops the host out of
        the registry and leaves clients with a connection error rather than an
        answer; this says what is happening.
        """
        if self.cfg.admin_key == "":
            # 404 rather than 403: without a key there is no control surface here
            # at all, and implying one exists that refused you would be a lie.
            resp.error(
                404,
                "no admin key is set, so remote control is disabled; start the host with -admin-key to enable it",
            )
            return
        presented = httpx.token_from(req.headers, httpx.KEY_HEADER)
        # Constant-time, because the admin key arrives from the network one byte
        # at a time and a comparison that stops at the first wrong byte tells an
        # attacker how much of a guess was right.
        if not hmac.compare_digest(presented.encode("utf-8"), self.cfg.admin_key.encode("utf-8")):
            resp.error(401, "missing or invalid admin key")
            return
        paused = _sharing_paused(req)
        if paused is None:
            resp.error(400, 'send {"paused": true} to stop serving peers, or {"paused": false} to resume')
            return
        self.set_paused(paused)
        resp.json(200, self.sharing_state())

    def set_paused(self, paused: bool) -> None:
        """Flip the tap and nudge the announce loop, so a resume is visible to
        clients straight away rather than at the next heartbeat."""
        with self._paused_lock:
            if self._paused.is_set() == paused:
                return
            if paused:
                self._paused.set()
                self._paused_at = time.time()
                self.log.warning(
                    "sharing paused: peers are refused with 503, and this host will stop being advertised "
                    "as its registry entry expires"
                )
            else:
                self._paused.clear()
                self._paused_at = 0.0
                self.log.info("sharing resumed")
        self._wake.set()

    def sharing_state(self) -> Dict[str, Any]:
        """The control endpoint's own answer: paused, and since when."""
        state: Dict[str, Any] = {"paused": self._paused.is_set()}
        since = self._paused_at
        if since:
            state["since"] = datetime.fromtimestamp(since, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        return state

    # -- the announce loop -------------------------------------------------

    def current_models(self) -> Optional[List[model.Model]]:
        """What the engine last said it serves, or None until it has been asked."""
        with self._models_lock:
            if self._models is None:
                return None
            return list(self._models)

    def announce(self) -> None:
        """List the engine and re-register. Registration is the heartbeat."""
        try:
            models = self.engine.list_models()
        except Exception as err:
            # A host starts before its engine is ready -- compose starts services
            # in whatever order it likes -- so an engine that cannot be listed is
            # a warning and not a crash. Go catches every error here for the same
            # reason; a lister that raises something unexpected is still just an
            # engine that could not be read this time.
            self.log.warning("cannot list engine models engine=%s err=%s", self.cfg.engine_url, err)
            return
        with self._models_lock:
            self._models = list(models)
        self.log.info("engine models models=%s", model.format_list(models))

        if self._paused.is_set():
            # Stop announcing while paused, so clients route to somebody else
            # rather than to a host that will refuse them. There is no delete in
            # the registry protocol, so "stop saying it" is the mechanism, and the
            # entry then expires on the registry's own TTL.
            self.log.info("paused: not announcing; this host's registry entry will expire on its own")
            return
        if self._disc is None:
            return

        # How busy this host is right now, which is what lets clients route to
        # whoever is least loaded rather than to whoever was listed first.
        #
        # A host running without a cap has no number to report, so it reports
        # nothing: "uncapped" is not "busy", and sending zero is exactly how an
        # uncapped host ended up sorted behind a full one. The reservation is
        # already out of the count, because free_slots reports peer slots.
        free: Optional[int] = None
        if self.cfg.max_concurrent > 0:
            free = self._meter.free_slots()

        entries = []
        for item in models:
            if item.digest == "":
                self.log.warning("model has no digest; clients cannot verify it model=%s", item.name)
            entries.append(
                registry.Entry(
                    model=item.name,
                    digest=item.digest,
                    address=self.cfg.public_address,
                    host=self.name,
                    free=free,
                )
            )
        try:
            self._disc.register(entries)
        except Exception as err:
            self.log.warning("registration failed err=%s", err)
            return
        self.log.info(
            "announced models=%s address=%s free=%s",
            model.format_list(models),
            self.cfg.public_address,
            "unknown" if free is None else str(free),
        )

    def _announce_loop(self, ctx: Any) -> None:
        """Re-register on a timer, or when asked to.

        Registration is the heartbeat, so a host that dies stops being advertised
        after the registry's TTL. The wake is a request to announce now rather
        than at the next heartbeat: resuming should be visible to clients straight
        away, not one heartbeat from now.
        """
        self.announce()
        next_at = time.monotonic() + self.cfg.heartbeat
        while not self._stopping(ctx):
            left = max(next_at - time.monotonic(), 0.0)
            woke = self._wake.wait(min(left, _POLL) if left > 0 else 0.0)
            if woke:
                self._wake.clear()
            if self._stopping(ctx):
                return
            if woke or time.monotonic() >= next_at:
                self.announce()
                next_at = time.monotonic() + self.cfg.heartbeat


def new(cfg: Config, log: Optional[logging.Logger] = None) -> Host:
    """Build a host. The engine is probed rather than connected to here, so a
    host can start before its engine is ready.

    Every refusal here is a misconfiguration that would otherwise look like a
    working host, so they are raised rather than logged: a heartbeat of zero would
    panic the announce loop rather than announce, a negative reserve hands peers
    more slots than the cap allows, and a reserve that swallows the whole cap is a
    host that serves nobody -- and pausing is the way to say "not right now".
    """
    log = log if log is not None else _log
    lister = engine.new(cfg.engine_kind, cfg.engine_url, cfg.engine)
    _parse_engine_url(cfg.engine_url)
    resolve_peers(cfg.share_keys, cfg.share_key)
    parse_quota(cfg.peer_quota)
    if cfg.heartbeat <= 0:
        raise ConfigError(
            "heartbeat %s must be positive: the host re-announces on that interval, "
            "and a non-positive one would panic rather than announce" % format_duration(cfg.heartbeat)
        )
    if cfg.owner_reserve < 0:
        raise ConfigError(
            "owner-reserve %d is negative, which would hand peers more slots than the cap allows"
            % cfg.owner_reserve
        )
    # Refused rather than warned about: a reserve that swallows the whole cap is a
    # misconfiguration that looks like a working host serving nobody. Pausing is
    # the way to say "not right now", and it says so out loud.
    if cfg.max_concurrent > 0 and cfg.owner_reserve >= cfg.max_concurrent:
        raise ConfigError(
            "owner-reserve %d leaves no slots for peers under max-concurrent %d: "
            "use -owner-reserve 0, raise -max-concurrent, or start with -paused"
            % (cfg.owner_reserve, cfg.max_concurrent)
        )
    return Host(cfg, lister, log)


def parse_quota(spec: str) -> meter.Quota:
    """Read "200/1h" into a budget, at startup, so that a typo is an error rather
    than a limit that silently does nothing."""
    spec = (spec or "").strip()
    if spec == "":
        return meter.Quota()
    count, sep, period = spec.partition("/")
    if sep == "":
        raise ConfigError('peer quota "%s": want count/period, e.g. 200/1h' % spec)
    n = _atoi(count.strip())
    if n is None or n <= 0:
        raise ConfigError('peer quota "%s": "%s" is not a positive number of requests' % (spec, count.strip()))
    try:
        window = parse_duration(period.strip())
    except ConfigError:
        window = 0.0
    if window <= 0:
        raise ConfigError('peer quota "%s": "%s" is not a duration like 1h or 24h' % (spec, period.strip()))
    return meter.Quota(requests=n, window=window)


# ---------------------------------------------------------------------------
# The command line
# ---------------------------------------------------------------------------


def default_address(listen: str) -> str:
    """The address to advertise when none was given: this machine's hostname plus
    the port we listen on.

    It is part of the public surface because the one-command mode builds a host
    config too, and an advertised address that disagreed between the two would be
    a peer-facing bug.
    """
    _host, port, ok = _split_host_port(listen or "")
    if not ok:
        # Go keeps the whole string as the port when it cannot split one, which is
        # the same refusal to guess: an address that cannot be parsed is not going
        # to become dialable by guessing half of it.
        port = listen or ""
    return _join_host_port(_node_name("localhost"), port)


def hostname() -> str:
    """This machine's name, for announcing and for identifying its hosts."""
    return _node_name("unknown")


def _node_name(fallback: str) -> str:
    try:
        name = socket.gethostname()
    except OSError:
        return fallback
    return name or fallback


def _split_host_port(text: str) -> Tuple[str, str, bool]:
    """Go's net.SplitHostPort: (host, port, ok).

    "host:port", ":port" for every interface, and "[::1]:80" all split; anything
    else -- a bare port, or an address with too many colons and no brackets --
    reports ok=False rather than being guessed at.
    """
    if text.startswith("["):
        end = text.find("]")
        if end < 0:
            return "", "", False
        rest = text[end + 1 :]
        if not rest.startswith(":"):
            return "", "", False
        return text[1:end], rest[1:], True
    host, sep, port = text.rpartition(":")
    if sep == "" or ":" in host:
        return "", "", False
    return host, port, True


def _join_host_port(host: str, port: str) -> str:
    """Go's net.JoinHostPort, including the brackets an IPv6 host needs."""
    if ":" in host:
        host = "[" + host + "]"
    return host + ":" + port


def _atoi(text: str) -> Optional[int]:
    """Go's strconv.Atoi: an optionally signed run of decimal digits, and nothing
    else. Python's int() would take "1_0" and non-ASCII digits."""
    body = text[1:] if text[:1] in ("+", "-") else text
    if body == "" or not body.isascii() or not body.isdigit():
        return None
    return int(text)


def _duration(text: str) -> float:
    """A duration flag value, in seconds.

    Go's flag package reports a bad value and exits; argparse does the same when
    its `type` raises ArgumentTypeError, with the same nonzero exit and the reason
    in the message.
    """
    try:
        return parse_duration(text)
    except ConfigError as err:
        raise argparse.ArgumentTypeError(str(err)) from None


class _GoHelp(argparse.HelpFormatter):
    """Help that prints a default the way Go's flag package does: "(default 4)".

    argparse's own ArgumentDefaultsHelpFormatter writes "(default: 4)" and only
    for actions it can render; the defaults here are already resolved from the
    environment and any config file by the time the parser is built, so showing
    them is the documented way to see which limits a host is actually running
    with.
    """

    def _get_help_string(self, action: argparse.Action) -> str:
        text = action.help or ""
        if "%(default)" in text:
            return text
        shown = _default_text(action.default)
        # Go leaves the default out when it is the zero value, so an unset key or
        # a limit nobody configured does not read as `(default )`.
        return text + " (default %s)" % shown if shown else text


class _Parser(argparse.ArgumentParser):
    """An ArgumentParser that exits like Go's flag package.

    Go's ExitOnError exits 2 for a bad flag and 0 for -h, and writes both the
    error and the help to stderr. argparse's `print_help` and `print_usage` name
    stdout before they reach `_print_message`, so both are overridden here: help
    on stdout would be mixed into whatever the caller was reading the command's
    output for, and a person piping it through a pager would never see it.
    """

    def _print_message(self, message: str, file: Any = None) -> None:
        if message:
            (file or sys.stderr).write(message)

    def print_usage(self, file: Any = None) -> None:
        self._print_message(self.format_usage(), file or sys.stderr)

    def print_help(self, file: Any = None) -> None:
        self._print_message(self.format_help(), file or sys.stderr)

    def error(self, message: str) -> None:  # pragma: no cover - exercised through run()
        self.print_usage(sys.stderr)
        self.exit(2, "%s: error: %s\n" % (self.prog, message))


def _go_bool(text: str) -> bool:
    """A boolean flag value, in the spellings Bothy accepts everywhere else.

    Go's flag package takes `-stream-usage=false`, and the same set of words is
    what the environment and the config file accept, so a flag written the way a
    compose file writes it means the same thing.
    """
    value = (text or "").strip().lower()
    if value in ("yes", "y", "on", "1", "t", "true"):
        return True
    if value in ("no", "n", "off", "0", "f", "false"):
        return False
    raise argparse.ArgumentTypeError("invalid boolean value %r" % text)


def _bool_flag(parser: argparse.ArgumentParser, *names: str, default: bool, help: str) -> None:
    """A boolean flag with Go's semantics: bare means true, `=false` means false.

    `nargs="?"` with `const=True` is what makes both spellings one flag, and the
    argument is optional so `-stream-usage -paused` does not read the second flag
    as the first one's value.
    """
    parser.add_argument(*names, nargs="?", const=True, type=_go_bool, default=default, help=help)


def _default_text(value: Any) -> str:
    """A default as Go's PrintDefaults would write it, or "" to leave it out.

    Go omits the default when it is the zero value, and a name is quoted because
    an address with a space in it would otherwise be unreadable.
    """
    if isinstance(value, bool):
        return "true" if value else ""
    if isinstance(value, (int, float)):
        if value == 0:
            return ""
        if isinstance(value, float):
            return format_duration(value)
        return str(value)
    text = str(value)
    if text == "":
        return ""
    return json.dumps(text, ensure_ascii=False)


def _share_parser() -> argparse.ArgumentParser:
    """The "share" command's flags, with Go's names and Go's defaults."""
    parser = _Parser(
        prog="bothy share",
        add_help=False,
        allow_abbrev=False,
        formatter_class=_GoHelp,
        description="share your inference engine with peers",
    )
    parser.add_argument("-h", "-help", "--help", action="help", help="show this help and exit")
    parser.add_argument(
        "-listen", "--listen", default=_listen(":7777"), help="address to listen on for peers"
    )
    parser.add_argument(
        "-engine-url",
        "--engine-url",
        default=_env_text("BOTHY_ENGINE_URL", "http://localhost:11434"),
        help="your inference engine's base URL",
    )
    parser.add_argument(
        "-engine-kind",
        "--engine-kind",
        default=_env_text("BOTHY_ENGINE_KIND", "auto"),
        help="auto, ollama, openai, mock or static",
    )
    parser.add_argument(
        "-discovery-url",
        "--discovery-url",
        default=_env_text("BOTHY_DISCOVERY_URL", ""),
        help="registry to announce to (optional)",
    )
    parser.add_argument(
        "-register-token",
        "--register-token",
        default=_env_text("BOTHY_REGISTRY_TOKEN", ""),
        help="token for registering with the registry",
    )
    parser.add_argument(
        "-share-key", "--share-key", default=_env_text("BOTHY_SHARE_KEY", ""), help="single key peers must present"
    )
    parser.add_argument(
        "-share-keys",
        "--share-keys",
        default=_env_text("BOTHY_SHARE_KEYS", ""),
        help="per-peer keys as name:key,name:key; wins over -share-key",
    )
    parser.add_argument(
        "-address",
        "--address",
        default=_env_text("BOTHY_PUBLIC_ADDRESS", ""),
        help="address to advertise to clients (default: hostname plus listen port)",
    )
    parser.add_argument(
        "-heartbeat",
        "--heartbeat",
        type=_duration,
        default=_env_dur("BOTHY_HEARTBEAT", 20.0),
        help="how often to re-announce",
    )
    parser.add_argument(
        "-max-concurrent",
        "--max-concurrent",
        type=int,
        default=_env_int("BOTHY_MAX_CONCURRENT", 4),
        help="requests to serve at once across all peers (0 = no cap)",
    )
    parser.add_argument(
        "-owner-reserve",
        "--owner-reserve",
        type=int,
        default=_env_int("BOTHY_OWNER_RESERVE", 1),
        help="of max-concurrent, how many slots peers may not use (default 1, keeping one free for you)",
    )
    parser.add_argument(
        "-peer-max-concurrent",
        "--peer-max-concurrent",
        type=int,
        default=_env_int("BOTHY_PEER_MAX_CONCURRENT", 0),
        help="how many slots one peer may hold at once (0 = no separate cap)",
    )
    parser.add_argument(
        "-max-request-time",
        "--max-request-time",
        type=_duration,
        default=_env_dur("BOTHY_MAX_REQUEST_TIME", 0.0),
        help="wall-clock limit for one request, e.g. 10m (0 = no limit)",
    )
    parser.add_argument(
        "-peer-quota",
        "--peer-quota",
        default=_env_text("BOTHY_PEER_QUOTA", ""),
        help="per-peer request budget as count/period, e.g. 200/1h (empty = no budget)",
    )
    parser.add_argument(
        "-max-requests-per-minute",
        "--max-requests-per-minute",
        type=int,
        default=_env_int("BOTHY_MAX_REQUESTS_PER_MINUTE", 0),
        help="request rate allowed per peer (0 = no cap)",
    )
    parser.add_argument(
        "-admin-key",
        "--admin-key",
        default=_env_text("BOTHY_ADMIN_KEY", ""),
        help="key for POST /bothy/sharing, which pauses and resumes sharing (empty = remote control disabled)",
    )
    _bool_flag(
        parser,
        "-paused",
        "--paused",
        default=_env_bool("BOTHY_PAUSED", False),
        help="start paused: refuse peers until resumed",
    )
    _bool_flag(
        parser,
        "-stream-usage",
        "--stream-usage",
        default=_env_bool("BOTHY_STREAM_USAGE", True),
        help="ask the engine for token usage on streamed replies, so they can be metered",
    )
    _bool_flag(
        parser,
        "-allow-all-routes",
        "--allow-all-routes",
        default=_env_bool("BOTHY_ALLOW_ALL_ROUTES", False),
        help="proxy every engine path, including its control routes (for a network you control)",
    )
    parser.add_argument(
        "-allow-routes",
        "--allow-routes",
        default=_env_text("BOTHY_ALLOW_ROUTES", ""),
        help="extra engine paths to proxy, as path or 'METHOD path', comma separated",
    )
    parser.add_argument(
        "-max-body",
        "--max-body",
        type=int,
        default=_env_int64("BOTHY_MAX_BODY", DEFAULT_MAX_BODY),
        help="largest request body to proxy, in bytes (0 = no cap)",
    )
    parser.add_argument(
        "-models-dir",
        "--models-dir",
        default=_env_text("BOTHY_MODELS_DIR", ""),
        help="Ollama models directory, for real weights digests",
    )
    parser.add_argument(
        "-weights",
        "--weights",
        default=_env_text("BOTHY_WEIGHTS_PATH", ""),
        help="weights file to hash (.gguf/.safetensors)",
    )
    parser.add_argument(
        "-model",
        "--model",
        default=_env_text("BOTHY_MODEL", ""),
        help="model name the weights file belongs to",
    )
    parser.add_argument(
        "-models",
        "--models",
        default=_env_text("BOTHY_MODELS", ""),
        help="static model list as name=digest,name=digest",
    )
    return parser


def _value_flags(parser: argparse.ArgumentParser) -> set:
    """The spellings of every flag that takes a value.

    Go's flag package takes the next argument as a flag's value even when it looks
    like another flag -- `-heartbeat -1s` -- and argparse reads that "-1s" as an
    option and refuses. Joining the two with "=" before parsing gives Go's
    behaviour without teaching argparse about durations.
    """
    out = set()
    for action in parser._actions:  # noqa: SLF001 - argparse exposes no other list
        # Only a flag that must be given a value can swallow the argument after
        # it. A boolean flag takes none, and a bool with `nargs="?"` may not.
        if action.nargs is not None:
            continue
        out.update(action.option_strings)
    return out


def _join_flag_values(args: Sequence[str], takes_value: Iterable[str]) -> List[str]:
    wanted = set(takes_value)
    out: List[str] = []
    index = 0
    while index < len(args):
        arg = args[index]
        if arg in wanted and index + 1 < len(args):
            out.append(arg + "=" + args[index + 1])
            index += 2
            continue
        out.append(arg)
        index += 1
    return out


def run(ctx: Any, log: Optional[logging.Logger], args: List[str]) -> None:
    """Parse flags for the "share" command and serve until ctx is cancelled.

    Go returns an error; the same failures are raised as a `ConfigError`, so a
    caller that wants to report one gets a sentence rather than a traceback. `ctx`
    is the stand-in for a Go context: anything with an `is_set()` method, and None
    means nobody will ask.
    """
    parser = _share_parser()
    opts = parser.parse_args(_join_flag_values(list(args), _value_flags(parser)))

    routes = parse_routes(opts.allow_routes)
    engine_opts = engine.Options(
        models_dir=opts.models_dir,
        weights_path=opts.weights,
        weights_model=opts.model,
        static=model.parse_list(opts.models),
    )
    cfg = Config(
        listen=opts.listen,
        engine_url=opts.engine_url,
        engine_kind=opts.engine_kind,
        discovery_url=opts.discovery_url,
        register_token=opts.register_token,
        share_key=opts.share_key,
        share_keys=opts.share_keys,
        public_address=opts.address,
        heartbeat=opts.heartbeat,
        max_concurrent=opts.max_concurrent,
        owner_reserve=opts.owner_reserve,
        peer_max_concurrent=opts.peer_max_concurrent,
        max_request_time=opts.max_request_time,
        peer_quota=opts.peer_quota,
        requests_per_minute=opts.max_requests_per_minute,
        admin_key=opts.admin_key,
        paused=opts.paused,
        stream_usage=opts.stream_usage,
        allow_all_routes=opts.allow_all_routes,
        allow_routes=routes,
        max_body=opts.max_body,
        engine=engine_opts,
    )
    if cfg.public_address == "":
        cfg.public_address = default_address(cfg.listen)
    new(cfg, log).serve(ctx)


# ---------------------------------------------------------------------------
# Small conversions
# ---------------------------------------------------------------------------


def _sharing_paused(req: httpx.Request) -> Optional[bool]:
    """The `{"paused": true}` body, or None when it is not one.

    At most a few KiB is read: the endpoint is meant to be reachable from wherever
    the owner happens to be, so it is not a place to accept an unbounded body. Go
    bounds it with an `io.LimitReader` and lets a decode that cannot finish refuse
    the request; so does this, which is also why a body that is not the documented
    object is refused rather than guessed at.
    """
    data = bytearray()
    for chunk in req.body_chunks():
        data += chunk
        if len(data) >= _SHARING_BODY_LIMIT:
            break
    try:
        payload = json.loads(bytes(data[:_SHARING_BODY_LIMIT]))
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    # Strictly a boolean: Go decodes into a *bool, so "yes" and 1 are refused
    # rather than read as true.
    paused = payload.get("paused")
    if not isinstance(paused, bool):
        return None
    return paused


def _models_json(models: Optional[List[model.Model]]) -> Optional[List[Dict[str, str]]]:
    """The wire form of a model list, or null when the engine has not been asked."""
    if models is None:
        return None
    return model.models_to_json(models)


def _usage_row(row: meter.PeerUsage) -> Dict[str, Any]:
    """One usage row, in the field names PROTOCOL.md documents.

    The two quota fields are left out when there is nothing to say -- no quota, or
    a window this peer has not opened -- which is what Go's `omitempty` means for
    them.
    """
    out: Dict[str, Any] = {
        "peer": row.peer,
        "in_flight": row.in_flight,
        "requests": row.requests,
        "limited": row.limited,
        "prompt_tokens": row.prompt_tokens,
        "completion_tokens": row.completion_tokens,
        "response_bytes": row.response_bytes,
        "unmetered_responses": row.unmetered,
        "last_seen": _timestamp(row.last_seen),
    }
    if row.quota_used:
        out["quota_used"] = row.quota_used
    if row.quota_reset:
        out["quota_reset"] = row.quota_reset
    return out


def _timestamp(seconds: float) -> str:
    """A timestamp as the usage report writes one.

    Go marshals a `time.Time` as RFC3339, and its zero value -- a peer that has
    been refused but never finished a request -- is the year 1 rather than the
    epoch, which is what 0.0 is here. Writing 1970 where Go writes 0001 would
    make a row look like it had been seen when it never had.
    """
    if not seconds:
        return "0001-01-01T00:00:00Z"
    return datetime.fromtimestamp(seconds, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
