"""The small HTTP helpers every Bothy service shares: peer token auth, JSON
responses, routing, and request logging.

Go's net/http hands these out for free -- `http.Handler`, `http.ServeMux`, and a
streaming `ResponseWriter` -- and the standard library's `http.server` gives us
less, so what Go gets from its platform lives here instead. That is a fair trade
rather than a loss: every Bothy service answers through this one module, so the
shape of an error body, the framing of a stream, and the number logged for a
response have exactly one implementation to disagree with.

Two things are deliberate. The request body is never buffered on the way in and
the response body is never buffered on the way out -- a proxy that collects a
stream to count its bytes has undone the reason to stream. And a handler is
called with objects rather than a socket, so a service can be tested without
binding a port.
"""

from __future__ import annotations

import http.server
import json
import logging
import os
import socket
import threading
import time
from email.utils import formatdate
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple
from urllib.parse import parse_qs, urlsplit

from .errors import BodyTooLarge, BothyError, ConfigError

# KeyHeader lets a peer present its key without pretending it is a Bearer token.
KEY_HEADER = "X-Bothy-Key"

# How much is read from a body at a time. Large enough that a proxied model
# response is not death by a thousand syscalls, small enough that two peers
# streaming at once do not cost hundreds of megabytes of buffers.
CHUNK = 64 * 1024

# Headers that are about one connection and mean something different on the next
# one, so a proxy must not pass them on.
HOP_BY_HOP = frozenset(
    {
        "connection",
        "proxy-connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)

# A handler answers one request by writing to the response.
Handler = Callable[["Request", "Response"], None]


def token_from(headers, header: str = KEY_HEADER) -> str:
    """Extract a peer key from the X-Bothy-Key header or from Authorization: Bearer.

    The named header wins when both are present, and values are trimmed, because
    leading and trailing whitespace is not part of a header value and a share key
    pasted out of a chat window arrives with some.
    """
    h = (headers.get(header) or "").strip()
    if h:
        return h
    a = (headers.get("Authorization") or "").strip()
    for prefix in ("Bearer ", "bearer "):
        if a.startswith(prefix):
            return a[len(prefix):].strip()
    return ""


def require_token(token: str, inner: Handler) -> Handler:
    """Reject requests that don't carry `token`.

    An empty token leaves the handler open -- callers should warn loudly when that
    happens, because on a reachable port it means anyone can spend your GPU.

    The comparison is constant-time. A share key is a shared secret that arrives
    from the network one byte at a time, and a comparison that stops at the first
    wrong byte tells an attacker how much of a guess was right.
    """
    if not token:
        return inner

    def guarded(req: Request, resp: Response) -> None:
        import hmac

        if not hmac.compare_digest(req.token, token):
            resp.error(401, "missing or invalid key")
            return
        inner(req, resp)

    return guarded


def json_bytes(payload: Any) -> bytes:
    """Encode a response body the way every Bothy service does.

    Indented, because these bodies are read by people debugging with curl far
    more often than by a parser that cares, and newline-terminated, the way Go's
    encoder leaves them.
    """
    return (json.dumps(payload, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def error_body(message: str) -> Dict[str, Any]:
    """The OpenAI-shaped error body.

    Existing clients already know how to surface `error.message`, so a caller
    that speaks OpenAI gets a sentence rather than a blank failure.
    """
    return {"error": {"message": message, "type": "bothy_error"}}


def snippet(data: Any, n: int) -> str:
    """Render up to n bytes of a body for a log line or an error message.

    Accepts bytes or a file-like object, because both turn up: bytes when a
    response was already read, a stream when it is an error body being reported
    without buffering all of it.
    """
    if hasattr(data, "read"):
        data = data.read(n)
    else:
        data = data[:n]
    if isinstance(data, str):
        return data.strip()
    return data.decode("utf-8", "replace").strip()


def end_to_end(headers, drop: Sequence[str] = ()) -> List[Tuple[str, str]]:
    """The headers worth forwarding, in order.

    Hop-by-hop headers are removed, and so is anything the Connection header names
    -- that second part is what people forget, and it is the part that lets a peer
    pick which of its own headers a proxy will pass along.
    """
    items = headers.items() if hasattr(headers, "items") else headers
    items = list(items)
    names = {k.lower() for k in drop}
    named = set()
    for k, v in items:
        if k.lower() == "connection":
            named |= {p.strip().lower() for p in (v or "").split(",") if p.strip()}
    return [(k, v) for k, v in items if k.lower() not in HOP_BY_HOP and k.lower() not in named and k.lower() not in names]


def split_addr(addr: str) -> Tuple[str, int]:
    """Split "host:port".

    An empty host means every interface, which is what a bare ":7777" asks for
    inside a container -- and the difference between that and "127.0.0.1" is the
    difference between a host this machine can share and one only it can reach.
    A bare port is refused rather than guessed at: it is a typo in one half of the
    address or the other, and picking one turns a bad setting into a service
    nobody can reach.
    """
    host, _, port = (addr or "").rpartition(":")
    if not port or ":" not in addr:
        raise ConfigError('bad listen address "%s": want host:port' % addr)
    try:
        number = int(port)
    except ValueError:
        raise ConfigError('bad listen address "%s": port is not a number' % addr) from None
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    return host, number


def _int_header(headers, name: str) -> int:
    try:
        return int(headers.get(name) or 0)
    except (TypeError, ValueError):
        return 0


class Request:
    """One incoming request.

    The body is left on the socket rather than read into memory: the point of a
    proxy is passing large bodies and long streams through, so `body_chunks()`
    reads as it arrives and `body_bytes()` is for the few routes that genuinely
    need the whole thing.

    `headers` is a case-insensitive mapping, `path` has no query string in it, and
    `peer` is filled in by whoever authenticates the request.
    """

    __slots__ = (
        "method",
        "path",
        "query",
        "headers",
        "raw",
        "remote",
        "version",
        "content_length",
        "chunked",
        "token",
        "peer",
    )

    def __init__(self, method: str, path: str, query: Dict[str, List[str]], headers, raw, remote: str = "", version: str = "HTTP/1.1"):
        self.method = method
        self.path = path
        self.query = query
        self.headers = headers
        self.raw = raw
        self.remote = remote
        self.version = version
        self.token = token_from(headers)
        self.peer = ""
        self.content_length = _int_header(headers, "Content-Length")
        self.chunked = "chunked" in (headers.get("Transfer-Encoding") or "").lower()

    def param(self, name: str, default: str = "") -> str:
        """One query parameter, or `default` when it was not given."""
        values = self.query.get(name)
        return values[0] if values else default

    def body_chunks(self) -> Iterator[bytes]:
        """Yield the request body as it arrives, dechunked.

        Chunked bodies are decoded rather than refused because a proxied request
        body is not ours to have opinions about: an engine that accepts them
        should keep accepting them, through us.
        """
        if self.chunked:
            yield from self._chunked_body()
            return
        remaining = self.content_length
        while remaining > 0:
            chunk = self.raw.read(min(CHUNK, remaining))
            if not chunk:
                return
            remaining -= len(chunk)
            yield chunk

    def _chunked_body(self) -> Iterator[bytes]:
        while True:
            line = self.raw.readline(1024)
            if not line:
                return
            size = line.split(b";", 1)[0].strip()
            if not size:
                continue
            try:
                n = int(size, 16)
            except ValueError:
                return
            if n == 0:
                # Trailers, then the blank line that ends the body.
                while True:
                    trailer = self.raw.readline(8192)
                    if not trailer or trailer in (b"\r\n", b"\n"):
                        return
            chunk = b""
            while len(chunk) < n:
                part = self.raw.read(n - len(chunk))
                if not part:
                    break
                chunk += part
            yield chunk
            self.raw.read(2)  # the CRLF after the chunk

    def body_bytes(self, limit: Optional[int] = None) -> bytes:
        """The whole body, or `BodyTooLarge`.

        A limit is passed where the body is about to be parsed anyway: it is
        cheaper to refuse an enormous one than to hold it, and the caller can
        answer 413 without keeping the bytes it just rejected. A declared length
        over the limit is refused before anything is read, which is the half of
        the limit that actually protects the host.
        """
        if limit is not None and not self.chunked and self.content_length > limit:
            raise BodyTooLarge("request body is larger than %d bytes" % limit)
        out = bytearray()
        for chunk in self.body_chunks():
            out.extend(chunk)
            if limit is not None and len(out) > limit:
                raise BodyTooLarge("request body is larger than %d bytes" % limit)
        return bytes(out)

    def json(self, limit: Optional[int] = 4 << 20) -> Any:
        """Parse the body as JSON.

        An empty body is `None` rather than an error, because several routes
        accept one -- pausing a host takes no arguments at all.
        """
        data = self.body_bytes(limit)
        if not data:
            return None
        try:
            return json.loads(data)
        except ValueError as err:
            raise BadRequest("body is not valid JSON: %s" % err) from None


class BadRequest(BothyError):
    """A request body that could not be understood, answered with 400."""


class Response:
    """One response being written.

    A handler writes through this and never touches the socket, so the JSON error
    shape, the chunked framing, and the byte count that gets logged all have one
    implementation.

    Sending is once-only: a second attempt is a bug in the handler rather than
    something to paper over, so it raises instead of appending a second head to
    one response.
    """

    __slots__ = ("request", "_h", "status", "bytes_written", "sent")

    def __init__(self, request: Request, handler):
        self.request = request
        self._h = handler
        self.status = 0
        self.bytes_written = 0
        self.sent = False

    def json(self, status: int, payload: Any, headers: Optional[Sequence[Tuple[str, str]]] = None) -> None:
        self.send_bytes(status, json_bytes(payload), headers=headers, content_type="application/json")

    def error(self, status: int, message: str, headers: Optional[Sequence[Tuple[str, str]]] = None) -> None:
        """An OpenAI-shaped error."""
        self.json(status, error_body(message), headers=headers)

    def send_bytes(
        self,
        status: int = 200,
        body: bytes = b"",
        headers: Optional[Sequence[Tuple[str, str]]] = None,
        content_type: Optional[str] = None,
    ) -> None:
        """A complete response held in memory.

        The length is always set, including for a body the caller cannot produce:
        a caller that said how much it wanted to hear gets an answer it can
        measure, even when the answer is a refusal.
        """
        if isinstance(body, str):
            body = body.encode("utf-8")
        self._write_head(status, headers, len(body), chunked=False, content_type=content_type)
        if body and self._may_send_body():
            self._write(body)
        self._finish()

    def send_stream(
        self,
        status: int = 200,
        chunks: Iterable[bytes] = (),
        headers: Optional[Sequence[Tuple[str, str]]] = None,
        content_type: Optional[str] = None,
        content_length: Optional[int] = None,
    ) -> None:
        """A response written as it arrives.

        When the length is known it is declared and the bytes go out raw; when it
        is not -- which is the streaming case, and the one that matters -- the
        response is chunked so the connection can stay open and a client sees each
        frame as it is produced.

        A client that hangs up mid-stream is not an error. Streaming stops, and
        the caller is still charged for what it cost, which is the same thing the
        Go implementation does and the reason the byte count is tracked here.
        """
        chunked = content_length is None and self.request.version.upper() >= "HTTP/1.1"
        self._write_head(status, headers, content_length, chunked=chunked, content_type=content_type)
        if not chunked and content_length is None:
            # HTTP/1.0 without a length: the only thing that can delimit this body
            # is the connection ending, so it has to end.
            self._h.close_connection = True
        if self._may_send_body():
            try:
                for chunk in chunks:
                    if not chunk:
                        continue
                    if chunked:
                        self._write(b"%x\r\n" % len(chunk) + chunk + b"\r\n")
                    else:
                        self._write(chunk)
                    self._h.wfile.flush()
                if chunked:
                    self._write(b"0\r\n\r\n")
            except (BrokenPipeError, ConnectionResetError):
                self._h.close_connection = True
        self._finish()

    def flush(self) -> None:
        try:
            self._h.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            self._h.close_connection = True

    def _may_send_body(self) -> bool:
        if 100 <= self.status < 200 or self.status in (204, 304):
            return False
        return self.request.method != "HEAD"

    def _write_head(self, status, headers, length, *, chunked, content_type) -> None:
        if self.sent:
            raise BothyError("response already sent")
        self.status = status
        self.sent = True
        h = self._h
        h.send_response_only(status)
        h.send_header("Date", formatdate(usegmt=True))
        if content_type:
            h.send_header("Content-Type", content_type)
        for key, value in headers or ():
            if key.lower() in ("content-length", "transfer-encoding", "date", "connection"):
                continue
            h.send_header(key, value)
        if self._may_send_body() or self.request.method == "HEAD":
            if chunked:
                h.send_header("Transfer-Encoding", "chunked")
            elif length is not None:
                h.send_header("Content-Length", str(length))
        h.end_headers()

    def _write(self, data: bytes) -> None:
        self._h.wfile.write(data)
        self.bytes_written += len(data)

    def _finish(self) -> None:
        try:
            self._h.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            self._h.close_connection = True


class Router:
    """A mux: exact paths first, then the longest matching prefix.

    Go's ServeMux gives both kinds of pattern and this gives the two Bothy uses,
    without guessing beyond them. The distinction is load-bearing in one place:
    the registry's human page is registered for exactly "/", because a prefix
    pattern there would answer every unknown path with a page and make a typo look
    like a working registry.

    A path that matches but does not take this method is a 405 with an Allow
    header, rather than a 404 that sends a reader looking for a typo that is not
    there.
    """

    def __init__(self) -> None:
        self._exact: Dict[Tuple[str, str], Handler] = {}
        self._prefix: List[Tuple[str, str, Handler]] = []
        self._default: Optional[Handler] = None

    def handle(self, method: str, path: str, fn: Handler) -> None:
        """Register an exact route.

        A duplicate is an error rather than a silent replacement, which is what
        Go's ServeMux does too: the second registration is always a mistake, and
        finding out at startup beats finding out from a request that came back
        with the wrong answer.
        """
        if (method, path) in self._exact:
            raise ConfigError("duplicate route: %s %s" % (method, path))
        self._exact[(method, path)] = fn

    def handle_prefix(self, method: str, prefix: str, fn: Handler) -> None:
        for have_method, have_prefix, _ in self._prefix:
            if (have_method, have_prefix) == (method, prefix):
                raise ConfigError("duplicate route: %s %s" % (method, prefix))
        self._prefix.append((method, prefix, fn))
        self._prefix.sort(key=lambda route: len(route[1]), reverse=True)

    def default(self, fn: Handler) -> None:
        """What an unmatched request gets -- where a proxy's catch-all lives."""
        self._default = fn

    def __call__(self, req: Request, resp: Response) -> None:
        # A GET route answers HEAD too, which is what Go's ServeMux does and what
        # every client that probes a service expects: HEAD is defined as GET minus
        # the body, so a route that could not answer it would be a route nobody
        # could check without paying for the body.
        wanted = ("HEAD", "GET") if req.method == "HEAD" else (req.method,)
        for (method, path), fn in self._exact.items():
            if path == req.path and (method in wanted or method == "*"):
                fn(req, resp)
                return
        allowed = {m for (m, path) in self._exact if path == req.path}
        for method, prefix, fn in self._prefix:
            if not req.path.startswith(prefix):
                continue
            if method in wanted or method == "*":
                fn(req, resp)
                return
            allowed.add(method)
        if allowed:
            if "GET" in allowed:
                allowed.add("HEAD")
            resp.error(405, "method %s is not allowed here" % req.method, headers=[("Allow", ", ".join(sorted(allowed)))])
            return
        if self._default is not None:
            self._default(req, resp)
            return
        resp.error(404, "no such route: %s" % req.path)


def log_requests(log: logging.Logger, inner: Handler) -> Handler:
    """Log one line per request with its status and duration.

    The duration is the one number that cannot be recovered afterwards, which is
    why it is here rather than left to whatever the caller does with the response.
    """

    def logged(req: Request, resp: Response) -> None:
        start = time.monotonic()
        try:
            inner(req, resp)
        finally:
            # A handler that never named a status gets 200 on the wire, the same
            # default Go's ResponseWriter applies on the first write; logging the
            # zero would put a status in the line that no client ever saw.
            log.info(
                "request method=%s path=%s status=%d bytes=%d duration=%s",
                req.method,
                req.path,
                resp.status or 200,
                resp.bytes_written,
                "%dms" % round((time.monotonic() - start) * 1000),
            )

    return logged


class _HeaderDeadline:
    """A reader that enforces a total deadline on the request header block.

    Go gives an http.Server two separate bounds and they mean different things:
    IdleTimeout for the wait until the next request starts arriving, and
    ReadHeaderTimeout for the header block once it has. A socket timeout in Python
    bounds one recv, so a peer sending a byte every nine seconds would reset it
    forever -- and a request header that never finishes arriving is the cheapest
    attack there is, on services that answer whoever can dial them.

    So the wait is bounded by the idle timeout, the block by the header timeout,
    and the body by neither: Go has no read timeout on a body either, which is
    what makes a slow but real upload possible.

    What is read underneath has to be buffered -- a `BufferedReader`, which is
    what `socket.makefile` hands back -- because starting the header clock
    without consuming the first byte means being able to look at it.
    """

    __slots__ = ("_rfile", "_conn", "_idle", "_header", "_deadline", "_released")

    def __init__(self, rfile, conn, idle: float, header: float):
        self._rfile = rfile
        self._conn = conn
        self._idle = idle
        self._header = header
        self._deadline: Optional[float] = None
        self._released = False

    def reset(self) -> None:
        """Start timing a new request on this connection."""
        self._deadline = None
        self._released = False

    def release(self) -> None:
        """The headers are in: stop timing, because a body is not a header."""
        self._released = True
        try:
            self._conn.settimeout(None)
        except OSError:
            pass

    def _arm(self) -> None:
        if self._released:
            return
        if self._deadline is None:
            # Waiting for the first byte of the next request is bounded by the
            # idle timeout rather than the header timeout, because a keep-alive
            # connection is allowed to sit quiet for that long. The wait is spent
            # in a peek so that the header clock starts when the peer does, not
            # when a whole line has arrived: a request line that never ends would
            # otherwise be free to hold the connection for the idle timeout,
            # which is the cheapest version of the attack this exists to stop.
            self._conn.settimeout(self._idle)
            if not self._rfile.peek(1):
                return  # the connection is over; let the read report it
            self._deadline = time.monotonic() + self._header
        left = self._deadline - time.monotonic()
        if left <= 0:
            raise socket.timeout("request header deadline exceeded")
        self._conn.settimeout(left)

    def readline(self, size: int = -1) -> bytes:
        self._arm()
        return self._rfile.readline(size)

    def read(self, size: int = -1) -> bytes:
        self._arm()
        return self._rfile.read(size)

    def __getattr__(self, name):
        return getattr(self._rfile, name)


class _BothyHandler(http.server.BaseHTTPRequestHandler):
    """The stdlib handler, adapted to Bothy's Request/Response.

    Requests are dispatched to whatever this server was given, and the stdlib's
    own logging is suppressed: one line per request is written by `log_requests`,
    where the status and byte count are actually known.
    """

    protocol_version = "HTTP/1.1"
    server_version = "bothy"
    sys_version = ""

    def setup(self) -> None:
        super().setup()
        self.rfile = _HeaderDeadline(self.rfile, self.connection, self.server.idle_timeout, self.server.read_header_timeout)

    def handle_one_request(self) -> None:
        self.rfile.reset()
        try:
            super().handle_one_request()
        except socket.timeout:
            self.close_connection = True
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
            # A client that vanishes between requests -- a closed tab, a dropped
            # proxy hop -- is a closed connection, not something to answer with a
            # traceback on the service's stderr.
            self.close_connection = True

    def _dispatch(self) -> None:
        self.rfile.release()
        parts = urlsplit(self.path)
        req = Request(
            self.command,
            parts.path,
            parse_qs(parts.query, keep_blank_values=True),
            self.headers,
            self.rfile,
            self.client_address[0] if self.client_address else "",
            self.request_version,
        )
        resp = Response(req, self)
        try:
            self.server.bothy_handler(req, resp)
            if not resp.sent:
                resp.send_bytes(200, b"")
        except BodyTooLarge as err:
            resp.error(413, str(err))
        except BadRequest as err:
            resp.error(400, str(err))
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except Exception as err:  # a handler bug is a 500, never a dead connection
            self.server.log.error("handler failed method=%s path=%s error=%s", req.method, req.path, err, exc_info=True)
            if not resp.sent:
                resp.error(500, "internal error")

    def log_message(self, fmt, *args) -> None:
        """Silence the stdlib's per-request stderr line; ours is structured."""

    def send_error(self, code, message=None, explain=None) -> None:
        """Answer a request we could not even parse, the way net/http does.

        The stdlib would send a page of HTML here; a caller parsing JSON should
        not have to guess which kind of body it got because of how badly it
        malformed something.

        The status line is written whatever the request claimed to be, because a
        request that failed the version check never had one recorded: the stdlib
        keeps the HTTP/0.9 default and drops the whole head, leaving a client
        with a body and nothing to attach it to.
        """
        try:
            self.request_version = "HTTP/1.1"
            reason = self.responses.get(code, ("", ""))[0]
            body = ((message or reason) + "\n").encode("utf-8")
            self.send_response(code, reason or None)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            if getattr(self, "command", "") != "HEAD":
                self.wfile.write(body)
        except (OSError, ValueError):
            pass
        self.close_connection = True

    def do_GET(self) -> None:
        self._dispatch()

    do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = _dispatch

    def __getattr__(self, name):
        if name.startswith("do_"):
            return self._dispatch
        raise AttributeError(name)


class _ThreadingServer(http.server.ThreadingHTTPServer):
    """ThreadingHTTPServer with Bothy's knobs on it.

    Threads rather than processes because every request is waiting on somebody
    else -- an engine, a peer, a client reading a stream -- and there is nothing
    to compute.

    The handler threads are daemons, so a stream nobody is waiting for cannot
    hold the process open, and that is exactly why the requests in flight are
    counted here: socketserver's own bookkeeping skips daemon threads when it
    closes, which would cut a two-minute generation in half on every restart.
    """

    daemon_threads = True
    # SO_REUSEADDR buys a short TIME_WAIT on POSIX. On Windows it lets a second
    # process bind an address that is already being listened on, which turns "the
    # port is taken" into a service that looks up and answers nobody.
    allow_reuse_address = os.name != "nt"

    def __init__(self, addr, handler: Handler, log: logging.Logger, read_header_timeout: float, idle_timeout: float):
        self.bothy_handler = handler
        self.log = log
        self.read_header_timeout = read_header_timeout
        self.idle_timeout = idle_timeout
        self.request_queue_size = 128
        self._inflight = 0
        self._inflight_done = threading.Condition()
        super().__init__(addr, _BothyHandler)

    def process_request(self, request, client_address) -> None:
        with self._inflight_done:
            self._inflight += 1
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._request_done()
            raise

    def process_request_thread(self, request, client_address) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._request_done()

    def _request_done(self) -> None:
        with self._inflight_done:
            self._inflight -= 1
            self._inflight_done.notify_all()

    def finish_requests(self, timeout: float) -> None:
        """Give the requests already accepted `timeout` seconds to finish.

        This is the half of Go's `http.Server.Shutdown` that a Python shutdown
        leaves out: the listening socket closes either way, but the answer to a
        request that is already being served has to be allowed out.
        """
        deadline = time.monotonic() + timeout
        with self._inflight_done:
            while self._inflight > 0:
                left = deadline - time.monotonic()
                if left <= 0:
                    return
                self._inflight_done.wait(left)


class Server:
    """An HTTP server with Bothy's one lifecycle: bind, serve, shut down.

    Binding happens in the constructor and therefore before anything is announced
    anywhere: "address already in use" has to be an error the caller can report
    while naming which half of the service failed, rather than a thread that dies
    quietly a moment later.
    """

    def __init__(
        self,
        addr: str,
        handler: Handler,
        log: logging.Logger,
        *,
        read_header_timeout: float = 10.0,
        idle_timeout: float = 120.0,
        shutdown_timeout: float = 5.0,
    ):
        host, port = split_addr(addr)
        self._httpd = _ThreadingServer((host, port), handler, log, read_header_timeout, idle_timeout)
        self._thread: Optional[threading.Thread] = None
        self._log = log
        self._shutdown_timeout = shutdown_timeout

    @property
    def addr(self) -> str:
        host, port = self._httpd.server_address[:2]
        return "%s:%d" % (host, port)

    def port(self) -> int:
        """The port actually bound, which matters when 0 was asked for."""
        return self._httpd.server_address[1]

    def start(self) -> None:
        self._thread = threading.Thread(target=self._httpd.serve_forever, kwargs={"poll_interval": 0.2}, name="bothy-http", daemon=True)
        self._thread.start()

    def wait(self) -> None:
        """Block until the server stops."""
        if self._thread is not None:
            self._thread.join()

    def shutdown(self) -> None:
        """Stop listening and let in-flight requests finish.

        Graceful rather than abrupt: a generation that has been running for two
        minutes should not be cut in half by a restart, and the peer on the other
        end of it has no way to tell that from a crash. The wait is bounded the
        way Go's `Server.Shutdown` bounds it, because a stream to a client that
        has silently gone away must not leave a service that ignores SIGTERM.

        Calling this twice is what `serve` does on its way out, so it has to be
        safe to call twice.
        """
        self._httpd.shutdown()
        self._httpd.finish_requests(self._shutdown_timeout)
        self._httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=10)


def serve(addr: str, handler: Handler, log: logging.Logger, **kwargs) -> None:
    """Serve until interrupted, which is what a single-service process does."""
    server = Server(addr, handler, log, **kwargs)
    log.info("listening addr=%s", server.addr)
    server.start()
    try:
        server.wait()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
