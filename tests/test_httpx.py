"""The HTTP layer every Bothy service shares, and the parts Go's net/http used
to supply.

The Go package this replaces was a handful of helpers -- a token reader, a JSON
encoder, an error body, a logging wrapper -- because `http.Handler`, ServeMux and
a streaming `ResponseWriter` came from the standard library. Python's
`http.server` supplies much less, so this module is also the router, the request
and response objects, the header deadline and the server lifecycle; those have no
Go test to port and are pinned here for the first time.

Two properties are worth more than the rest, because breaking either would be
invisible until something large was underway: a frame of a stream reaches the
client while the generator producing it is still blocked, and a request already
being served is allowed to finish when the server is told to stop.
"""

from __future__ import annotations

import contextlib
import errno
import http.client
import io
import json
import logging
import re
import socket
import struct
import threading
import time
import unittest
from email.message import Message

from bothy import httpx
from bothy.errors import BodyTooLarge, BothyError, ConfigError


class _Sink(io.BytesIO):
    """A body that remembers how often it was flushed.

    A response that is written through a wrapper and never flushed is a response
    the client is still waiting for, so the flush count is the observable proof
    that streaming survived the wrapping.
    """

    def __init__(self):
        super().__init__()
        self.flushes = 0

    def flush(self):
        self.flushes += 1
        super().flush()


class _Recorder:
    """The stdlib handler surface that `Response` writes through.

    Stands in for `BaseHTTPRequestHandler` so a handler can be exercised without
    a socket, the way Go's recorder tests use `httptest.NewRecorder`.
    """

    def __init__(self):
        self.status = 0
        self.headers = []
        self.wfile = _Sink()
        self.close_connection = False

    def send_response_only(self, code, message=None):
        self.status = code

    def send_header(self, keyword, value):
        self.headers.append((keyword, str(value)))

    def end_headers(self):
        pass

    def header(self, name):
        for key, value in self.headers:
            if key.lower() == name.lower():
                return value
        return None

    def body(self):
        return self.wfile.getvalue()


def _message(*pairs):
    """A case-insensitive header bag, the shape `Request.headers` really is."""
    m = Message()
    for key, value in pairs:
        m[key] = value
    return m


def _request(method="GET", path="/", query=None, headers=None, body=b"", version="HTTP/1.1"):
    return httpx.Request(method, path, query or {}, headers or _message(), io.BytesIO(body), "127.0.0.1", version)


def _respond(handler, req):
    """Run one handler against a recording writer, and hand both back."""
    rec = _Recorder()
    resp = httpx.Response(req, rec)
    handler(req, resp)
    return rec, resp


class _Capture(logging.Handler):
    """Collects the lines a logger emitted, which is what a log test asserts on."""

    def __init__(self):
        super().__init__()
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())


def _log():
    log = logging.getLogger("bothy.httpx.test")
    log.setLevel(logging.INFO)
    log.propagate = False
    log.handlers[:] = []
    log.addHandler(logging.NullHandler())
    return log


class _ServerTest(unittest.TestCase):
    """A real server on an ephemeral port: the counterpart of httptest.NewServer."""

    def serve(self, handler, **kwargs):
        log = _log()
        log.handlers[:] = [_Capture()]
        server = httpx.Server("127.0.0.1:0", handler, log, **kwargs)
        self.addCleanup(server.shutdown)
        server.start()
        return server

    def connect(self, server, timeout=5):
        conn = http.client.HTTPConnection("127.0.0.1", server.port(), timeout=timeout)
        self.addCleanup(conn.close)
        return conn


# --------------------------------------------------------------------------
# Ported from the Go package's logging_test.go, which this replaced
# --------------------------------------------------------------------------


class TestSnippetReadsAtMostN(unittest.TestCase):
    def test_snippet_reads_at_most_n(self):
        stream = io.BytesIO(b"0123456789abcdef")
        self.assertEqual(httpx.snippet(stream, 4), "0123", "want the first 4 bytes")
        self.assertEqual(
            stream.tell(),
            4,
            "snippet read %d bytes, want 4 -- an error body must not be buffered whole" % stream.tell(),
        )


class TestSnippetTrimsWhitespace(unittest.TestCase):
    def test_snippet_trims_whitespace(self):
        self.assertEqual(httpx.snippet(b"  \n  no such model \n ", 300), "no such model", "want the trimmed body")


class TestLogRequestsRecordsOneLinePerRequest(unittest.TestCase):
    # The screenshot people look at when something breaks is this line, so it has
    # to carry the fields it claims to: method, path, status, bytes and duration.
    def test_log_requests_records_one_line_per_request(self):
        log = _log()
        capture = _Capture()
        log.handlers[:] = [capture]

        def ok(req, resp):
            resp.send_bytes(201, b"hello")

        def broken(req, resp):
            raise RuntimeError("handler bug")

        logged = httpx.log_requests(log, ok)
        _respond(logged, _request("POST", "/v1/chat/completions"))
        with self.assertRaises(RuntimeError):
            _respond(httpx.log_requests(log, broken), _request("GET", "/v1/models"))

        self.assertEqual(len(capture.lines), 2, "want exactly one line per request: %r" % (capture.lines,))
        line = capture.lines[0]
        match = re.match(r"^request method=(\S+) path=(\S+) status=(\d+) bytes=(\d+) duration=(\d+)ms$", line)
        self.assertIsNotNone(match, "log line is not the documented one: %r" % line)
        self.assertEqual(match.group(1), "POST")
        self.assertEqual(match.group(2), "/v1/chat/completions")
        self.assertEqual(match.group(3), "201")
        self.assertEqual(match.group(4), "5")
        self.assertTrue(
            re.match(r"^request method=GET path=/v1/models status=(\d+) bytes=0 duration=\d+ms$", capture.lines[1]),
            "a failing request still takes one line: %r" % capture.lines[1],
        )


class TestRecorderDefaultsToOK(unittest.TestCase):
    # A handler that only writes a body never names a status, and the log must
    # still say 200 rather than 0.
    def test_recorder_defaults_to_ok(self):
        log = _log()
        capture = _Capture()
        log.handlers[:] = [capture]

        def quiet(req, resp):
            pass

        _respond(httpx.log_requests(log, quiet), _request("GET", "/"))
        self.assertIn("status=200", capture.lines[0], "want 200 for a handler that named no status")


class TestRecorderKeepsStreamingWorking(unittest.TestCase):
    # Wrapping a response must not cost streaming: the flush has to reach the
    # writer underneath, or a streamed reply would sit in a buffer.
    def test_recorder_keeps_streaming_working(self):
        log = _log()
        log.handlers[:] = [_Capture()]

        def stream(req, resp):
            resp.send_stream(200, [b"one", b"two"])

        rec, resp = _respond(httpx.log_requests(log, stream), _request("GET", "/events"))
        self.assertGreaterEqual(rec.wfile.flushes, 3, "the chunks and the end of the stream must each be flushed")
        self.assertEqual(rec.header("Transfer-Encoding"), "chunked")
        self.assertEqual(rec.body(), b"3\r\none\r\n3\r\ntwo\r\n0\r\n\r\n")

        rec, resp = _respond(lambda req, resp: resp.flush(), _request("GET", "/"))
        self.assertEqual(rec.wfile.flushes, 1, "Flush did not reach the wrapped writer")


class TestRecorderCountsWhatWasWritten(unittest.TestCase):
    def test_recorder_counts_what_was_written(self):
        rec, resp = _respond(lambda req, resp: resp.send_bytes(202, b"abcdefgh"), _request("GET", "/"))
        self.assertEqual(rec.status, 202, "want the status captured from the handler")
        self.assertEqual(resp.bytes_written, 8, "want the bytes the handler wrote")


class TestJSONNilWritesNoBody(unittest.TestCase):
    # A nil payload is how the code says "status only" -- an empty 204 with a JSON
    # content type, not the string "null".
    def test_json_nil_writes_no_body(self):
        rec, _ = _respond(lambda req, resp: resp.json(204, None), _request("GET", "/"))
        self.assertEqual(rec.status, 204, "want 204")
        self.assertEqual(rec.body(), b"", "a 204 must not carry a body")
        self.assertIsNone(rec.header("Content-Length"), "a 204 must not carry a Content-Length")
        self.assertEqual(rec.header("Content-Type"), "application/json")


class TestTokenFromEdgeCases(unittest.TestCase):
    def test_token_from_edge_cases(self):
        cases = {
            "nothing at all": ("", "", ""),
            "a key header": ("k", "", "k"),
            "a bearer token": ("", "Bearer k", "k"),
            "a lowercase bearer": ("", "bearer k", "k"),
            "extra whitespace": ("", "Bearer   k  ", "k"),
            "a bare scheme with no credential": ("", "Bearer", ""),
            "a scheme we do not use": ("", "Basic k", ""),
            "a blank key header falls back to bearer": ("   ", "Bearer k", "k"),
        }
        for name, (key, auth, want) in cases.items():
            with self.subTest(name):
                pairs = []
                if key != "":
                    pairs.append((httpx.KEY_HEADER, key))
                if auth != "":
                    pairs.append(("Authorization", auth))
                got = httpx.token_from(_message(*pairs))
                self.assertEqual(got, want)


class TestRequireTokenRefusesADifferentLength(unittest.TestCase):
    # A key of the wrong length must be refused rather than compared byte by byte,
    # which is the whole reason the comparison is constant time.
    def test_require_token_refuses_a_different_length(self):
        handler = httpx.require_token("correct-horse", lambda req, resp: resp.json(200, {"ok": True}))
        for key in ("correct", "correct-horse-battery", "CORRECT-HORSE", ""):
            with self.subTest(key=key):
                pairs = [(httpx.KEY_HEADER, key)] if key else []
                rec, _ = _respond(handler, _request("GET", "/", headers=_message(*pairs)))
                self.assertEqual(rec.status, 401, "key %r must be refused" % key)


# --------------------------------------------------------------------------
# Ported from the Go package's serve_test.go, which this replaced
# --------------------------------------------------------------------------


class TestRequireToken(unittest.TestCase):
    # An empty token means open, which callers are supposed to warn about; a set
    # token means every route behind it is closed.
    def test_require_token(self):
        handler = httpx.require_token("secret", lambda req, resp: resp.json(200, {"ok": True}))
        cases = {
            "the key header": (httpx.KEY_HEADER, "secret", 200),
            "a bearer token": ("Authorization", "Bearer secret", 200),
            "no credential": ("", "", 401),
            "the wrong key": (httpx.KEY_HEADER, "wrong", 401),
        }
        for name, (header, value, want) in cases.items():
            with self.subTest(name):
                pairs = [(header, value)] if header else []
                rec, _ = _respond(handler, _request("GET", "/", headers=_message(*pairs)))
                self.assertEqual(rec.status, want)
                if want == 401:
                    body = json.loads(rec.body())
                    self.assertEqual(body["error"]["message"], "missing or invalid key")
                    self.assertEqual(body["error"]["type"], "bothy_error")

        held = lambda req, resp: None
        self.assertIs(httpx.require_token("", held), held, "an empty token leaves the handler in place unwrapped")


class TestErrorIsOpenAIShaped(unittest.TestCase):
    # Errors are deliberately OpenAI-shaped so that a client which already parses
    # error.message shows something instead of a blank failure.
    def test_error_is_openai_shaped(self):
        rec, _ = _respond(lambda req, resp: resp.error(429, "slow down"), _request("GET", "/"))
        self.assertEqual(rec.status, 429, "want 429")
        self.assertEqual(rec.header("Content-Type"), "application/json")
        body = json.loads(rec.body())
        self.assertEqual(body["error"]["message"], "slow down")
        self.assertEqual(body["error"]["type"], "bothy_error")


class TestTokenFromPrefersTheKeyHeader(unittest.TestCase):
    def test_token_from_prefers_the_key_header(self):
        headers = _message((httpx.KEY_HEADER, " key "), ("Authorization", "Bearer other"))
        self.assertEqual(httpx.token_from(headers), "key", "want the trimmed key header")
        del headers[httpx.KEY_HEADER]
        self.assertEqual(httpx.token_from(headers), "other", "want the bearer fallback")


class TestServeServesThenShutsDownOnCancel(_ServerTest):
    # Every one of the four roles ends its life through this lifecycle on SIGTERM,
    # and `docker compose stop` relies on it.
    def test_serve_serves_then_shuts_down_on_cancel(self):
        server = self.serve(lambda req, resp: resp.json(200, {"ok": True}))

        conn = self.connect(server)
        conn.request("GET", "/")
        resp = conn.getresponse()
        body = resp.read()
        self.assertIn(b'"ok": true', body, "the server never answered, so this proves nothing: %r" % body)

        server.shutdown()
        with self.assertRaises(OSError):
            socket.create_connection(("127.0.0.1", server.port()), timeout=2).close()


class TestServeReturnsTheBindErrorInsteadOfHanging(_ServerTest):
    # A role that cannot bind has to fail with the reason rather than start up and
    # sit there, looking exactly like a service that is up and idle.
    def test_serve_returns_the_bind_error_instead_of_hanging(self):
        held = socket.socket()
        self.addCleanup(held.close)
        held.bind(("127.0.0.1", 0))
        held.listen(1)
        addr = "127.0.0.1:%d" % held.getsockname()[1]

        with self.assertRaises(OSError) as caught:
            httpx.Server(addr, lambda req, resp: None, _log())
        self.assertTrue(
            caught.exception.errno in (errno.EADDRINUSE, 10048)
            or "address already in use" in str(caught.exception).lower(),
            "error %r does not say the port was taken" % (caught.exception,),
        )


# --------------------------------------------------------------------------
# No Go counterpart: the router
# --------------------------------------------------------------------------


class TestRouterExactBeatsTheLongestPrefix(unittest.TestCase):
    def test_router_exact_beats_the_longest_prefix(self):
        router = httpx.Router()
        router.handle("GET", "/api/v1/models", lambda req, resp: resp.json(200, {"route": "exact"}))
        router.handle_prefix("GET", "/api/v1", lambda req, resp: resp.json(200, {"route": "longest"}))
        router.handle_prefix("GET", "/api", lambda req, resp: resp.json(200, {"route": "shortest"}))

        cases = {
            "/api/v1/models": "exact",
            "/api/v1/models/llama3.1": "longest",
            "/api/health": "shortest",
        }
        for path, want in cases.items():
            with self.subTest(path=path):
                rec, _ = _respond(router, _request("GET", path))
                self.assertEqual(rec.status, 200)
                self.assertEqual(json.loads(rec.body())["route"], want)


class TestRouterAnswersHeadWithTheGetRoute(unittest.TestCase):
    def test_router_answers_head_with_the_get_route(self):
        router = httpx.Router()
        router.handle("GET", "/v1/models", lambda req, resp: resp.send_bytes(200, b"models"))

        rec, _ = _respond(router, _request("HEAD", "/v1/models"))
        self.assertEqual(rec.status, 200, "a GET route has to answer a HEAD, or nobody can probe it")
        self.assertEqual(rec.header("Content-Length"), "6", "HEAD must send the headers GET would, minus the body")
        self.assertEqual(rec.body(), b"", "HEAD must not send a body")


class TestRouterNotFound(unittest.TestCase):
    def test_router_not_found(self):
        router = httpx.Router()
        router.handle("GET", "/v1/models", lambda req, resp: resp.send_bytes(200, b"models"))

        rec, _ = _respond(router, _request("GET", "/v1/nothing"))
        self.assertEqual(rec.status, 404)
        self.assertEqual(json.loads(rec.body())["error"]["type"], "bothy_error")


class TestRouterMethodNotAllowed(unittest.TestCase):
    def test_router_method_not_allowed(self):
        router = httpx.Router()
        router.handle("GET", "/v1/models", lambda req, resp: resp.send_bytes(200, b"models"))

        rec, _ = _respond(router, _request("POST", "/v1/models"))
        self.assertEqual(rec.status, 405)
        self.assertEqual(rec.header("Allow"), "GET, HEAD", "a 405 must name what would have worked")
        self.assertEqual(json.loads(rec.body())["error"]["type"], "bothy_error")

    def test_router_405_from_a_prefix_names_only_what_is_registered(self):
        router = httpx.Router()
        router.handle_prefix("POST", "/v1", lambda req, resp: resp.send_bytes(200, b""))

        rec, _ = _respond(router, _request("DELETE", "/v1/models"))
        self.assertEqual(rec.status, 405)
        self.assertEqual(rec.header("Allow"), "POST")


class TestRouterRefusesADuplicateRoute(unittest.TestCase):
    # Like ServeMux's panic on a duplicate pattern: the second registration is
    # always a mistake, and finding out at startup beats finding out from a
    # request that came back with the wrong answer.
    def test_router_refuses_a_duplicate_route(self):
        router = httpx.Router()
        router.handle("GET", "/v1/models", lambda req, resp: None)
        with self.assertRaises(ConfigError):
            router.handle("GET", "/v1/models", lambda req, resp: None)
        router.handle_prefix("GET", "/v1", lambda req, resp: None)
        with self.assertRaises(ConfigError):
            router.handle_prefix("GET", "/v1", lambda req, resp: None)


class TestRouterFallsBackToTheDefault(unittest.TestCase):
    def test_router_falls_back_to_the_default(self):
        router = httpx.Router()
        router.handle("GET", "/own", lambda req, resp: resp.send_bytes(200, b"mine"))
        router.default(lambda req, resp: resp.send_bytes(200, b"proxied"))

        rec, _ = _respond(router, _request("POST", "/v1/chat/completions"))
        self.assertEqual(rec.body(), b"proxied", "an unmatched request is where a proxy's catch-all lives")
        rec, _ = _respond(router, _request("GET", "/own"))
        self.assertEqual(rec.body(), b"mine")


# --------------------------------------------------------------------------
# No Go counterpart: the request body
# --------------------------------------------------------------------------


class TestRequestDechunksABody(unittest.TestCase):
    def test_request_dechunks_a_body(self):
        framed = b"5\r\nhello\r\n6\r\n world\r\n0\r\nX-Trailer: yes\r\n\r\n"
        req = httpx.Request(
            "POST", "/v1/chat/completions", {}, _message(("Transfer-Encoding", "chunked")), io.BytesIO(framed), "127.0.0.1"
        )
        self.assertTrue(req.chunked)
        self.assertEqual(b"".join(req.body_chunks()), b"hello world", "want the chunks joined, trailers dropped")

    def test_request_dechunks_a_body_into_json(self):
        body = b'{"stream": true}'
        framed = b"%x\r\n%s\r\n0\r\n\r\n" % (len(body), body)
        req = httpx.Request(
            "POST", "/v1/chat/completions", {}, _message(("Transfer-Encoding", "chunked")), io.BytesIO(framed), "127.0.0.1"
        )
        # One read, because a body off a socket can only be read once -- which is
        # the point of dechunking it as it arrives.
        self.assertEqual(req.json(), {"stream": True})


class TestRequestReadsOnlyTheDeclaredLength(unittest.TestCase):
    def test_request_reads_only_the_declared_length(self):
        req = _request("POST", "/", headers=_message(("Content-Length", "5")), body=b"helloextra")
        self.assertEqual(req.body_bytes(), b"hello", "a body must end where its Content-Length says")
        self.assertEqual(req.content_length, 5)


class TestRequestEmptyBodyIsNone(unittest.TestCase):
    def test_request_empty_body_is_none(self):
        # Several routes take no arguments at all -- pausing a host is one.
        req = _request("POST", "/pause", headers=_message(("Content-Length", "0")))
        self.assertIsNone(req.json())


class TestRequestInvalidJSONIsARefusal(unittest.TestCase):
    def test_request_invalid_json_is_a_refusal(self):
        req = _request("POST", "/", headers=_message(("Content-Length", "1")), body=b"{")
        with self.assertRaises(httpx.BadRequest):
            req.json()


class TestRequestBodyTooLarge(unittest.TestCase):
    def test_request_body_too_large(self):
        req = _request("POST", "/", headers=_message(("Content-Length", "10")), body=b"0123456789")
        with self.assertRaises(BodyTooLarge):
            req.body_bytes(limit=4)

        # The half of the limit that protects the host: a declared length over the
        # limit is refused before a byte of it is read.
        raw = io.BytesIO(b"0123456789")
        req = httpx.Request("POST", "/", {}, _message(("Content-Length", "10")), raw, "127.0.0.1")
        with self.assertRaises(BodyTooLarge):
            req.body_bytes(limit=4)
        self.assertEqual(raw.tell(), 0, "want nothing read from a body already known to be too big")


class TestRequestBodyTooLargeIsAnsweredWith413(_ServerTest):
    def test_request_body_too_large_is_answered_with_413(self):
        def handler(req, resp):
            resp.json(200, req.json(limit=16))

        server = self.serve(handler)
        conn = self.connect(server)
        conn.request("POST", "/", body=b'{"prompt": "%s"}' % (b"x" * 64))
        resp = conn.getresponse()
        self.assertEqual(resp.status, 413)
        self.assertEqual(json.loads(resp.read())["error"]["type"], "bothy_error")


class TestRequestInvalidJSONIsAnsweredWith400(_ServerTest):
    def test_request_invalid_json_is_answered_with_400(self):
        def handler(req, resp):
            resp.json(200, req.json())

        server = self.serve(handler)
        conn = self.connect(server)
        conn.request("POST", "/", body=b"{not json")
        resp = conn.getresponse()
        self.assertEqual(resp.status, 400)
        body = json.loads(resp.read())
        self.assertEqual(body["error"]["type"], "bothy_error")
        self.assertTrue(body["error"]["message"].startswith("body is not valid JSON"), body["error"]["message"])


class TestServerDechunksARequestOnTheWire(_ServerTest):
    def test_server_dechunks_a_request_on_the_wire(self):
        got = {}

        def handler(req, resp):
            got["body"] = req.body_bytes()
            resp.send_bytes(200, b"ok")

        server = self.serve(handler)
        sock = socket.create_connection(("127.0.0.1", server.port()), timeout=5)
        self.addCleanup(sock.close)
        sock.sendall(
            b"POST /v1/chat/completions HTTP/1.1\r\n"
            b"Host: bothy\r\n"
            b"Transfer-Encoding: chunked\r\n\r\n"
            b"7\r\n{\"a\": 1\r\n0\r\n\r\n"
        )
        self.assertIn(b"200", sock.recv(4096))
        self.assertEqual(got["body"], b'{"a": 1')


# --------------------------------------------------------------------------
# No Go counterpart: the response
# --------------------------------------------------------------------------


class TestResponseContentLength(unittest.TestCase):
    def test_response_content_length(self):
        rec, _ = _respond(lambda req, resp: resp.send_bytes(200, b"hello"), _request("GET", "/"))
        self.assertEqual(rec.header("Content-Length"), "5")
        self.assertIsNone(rec.header("Transfer-Encoding"), "a known length is not chunked")
        self.assertEqual(rec.body(), b"hello")


def _gap(req, resp):
    """A response with no length: the streaming case."""
    resp.send_stream(200, [b"one", b"two"], content_type="text/event-stream")


class TestResponseChunksAStreamWithNoKnownLength(unittest.TestCase):
    def test_response_chunks_a_stream_with_no_known_length(self):
        rec, _ = _respond(_gap, _request("GET", "/events"))
        self.assertEqual(rec.header("Transfer-Encoding"), "chunked")
        self.assertIsNone(rec.header("Content-Length"), "a stream of unknown length cannot declare one")
        self.assertEqual(rec.header("Content-Type"), "text/event-stream")
        self.assertEqual(rec.body(), b"3\r\none\r\n3\r\ntwo\r\n0\r\n\r\n")


class TestResponseSendsNoBodyFor204(unittest.TestCase):
    def test_response_sends_no_body_for_204(self):
        rec, _ = _respond(lambda req, resp: resp.send_bytes(204, b""), _request("GET", "/"))
        self.assertEqual(rec.status, 204)
        self.assertEqual(rec.body(), b"")
        self.assertIsNone(rec.header("Content-Length"))
        self.assertIsNone(rec.header("Transfer-Encoding"))


class TestResponseSendsHeadersButNoBodyForHead(unittest.TestCase):
    def test_response_sends_headers_but_no_body_for_head(self):
        rec, _ = _respond(lambda req, resp: resp.send_bytes(200, b"hello"), _request("HEAD", "/"))
        self.assertEqual(rec.status, 200)
        self.assertEqual(rec.header("Content-Length"), "5", "a HEAD exists so a client can learn this")
        self.assertEqual(rec.body(), b"")


class TestResponseNeverCopiesFramingHeaders(unittest.TestCase):
    # The caller is proxying an upstream response whose framing describes the
    # upstream's body, not ours. Copying it would make one response's length
    # describe another's bytes.
    def test_response_never_copies_framing_headers(self):
        def handler(req, resp):
            resp.send_bytes(
                200,
                b"hi",
                headers=[
                    ("Content-Length", "9999"),
                    ("Transfer-Encoding", "chunked"),
                    ("Date", "Thu, 01 Jan 1970 00:00:00 GMT"),
                    ("Connection", "close"),
                    ("X-Upstream", "kept"),
                ],
            )

        rec, _ = _respond(handler, _request("GET", "/"))
        self.assertEqual(rec.header("Content-Length"), "2")
        self.assertIsNone(rec.header("Transfer-Encoding"))
        self.assertNotEqual(rec.header("Date"), "Thu, 01 Jan 1970 00:00:00 GMT")
        self.assertEqual(rec.header("X-Upstream"), "kept", "an ordinary upstream header is what a proxy is for")
        self.assertEqual(rec.header("Connection"), None)


class TestResponseRefusesASecondSend(unittest.TestCase):
    def test_response_refuses_a_second_send(self):
        def handler(req, resp):
            resp.send_bytes(200, b"first")
            resp.send_bytes(200, b"second")

        with self.assertRaises(BothyError):
            _respond(handler, _request("GET", "/"))


class TestResponseSurvivesAClientHangingUp(unittest.TestCase):
    # A client that hangs up mid-stream is not an error; the caller is still
    # charged for what it cost.
    def test_response_survives_a_client_hanging_up(self):
        class _Broken(_Sink):
            """Accepts one write, then reports the client gone."""

            def __init__(self):
                super().__init__()
                self.writes = 0

            def write(self, data):
                self.writes += 1
                if self.writes > 1:
                    raise BrokenPipeError(errno.EPIPE, "client went away")
                return super().write(data)

        rec = _Recorder()
        rec.wfile = _Broken()
        req = _request("GET", "/events")
        resp = httpx.Response(req, rec)

        def frames():
            yield b"one"
            yield b"two"

        resp.send_stream(200, frames())
        self.assertTrue(rec.close_connection, "want the connection marked for closing rather than a raise")
        self.assertGreater(resp.bytes_written, 0, "the frames that were produced still count")


class TestJSONBytesAreIndentedAndNewlineTerminated(unittest.TestCase):
    def test_json_bytes_are_indented_and_newline_terminated(self):
        self.assertEqual(httpx.json_bytes({"a": 1}), b'{\n  "a": 1\n}\n')

    def test_json_bodies_on_the_wire_are_readable(self):
        rec, _ = _respond(lambda req, resp: resp.json(200, {"models": []}), _request("GET", "/v1/models"))
        self.assertTrue(rec.body().endswith(b"\n"), "want the newline Go's encoder leaves")
        self.assertEqual(rec.body(), b'{\n  "models": []\n}\n')
        self.assertEqual(rec.header("Content-Type"), "application/json")


class TestEndToEndDropsHopByHopHeaders(unittest.TestCase):
    def test_end_to_end_drops_hop_by_hop_headers(self):
        headers = _message(
            ("Keep-Alive", "timeout=5"),
            ("Transfer-Encoding", "chunked"),
            ("Proxy-Connection", "keep-alive"),
            ("Upgrade", "h2c"),
            ("X-Request-Id", "abc"),
        )
        self.assertEqual(httpx.end_to_end(headers), [("X-Request-Id", "abc")])

    def test_end_to_end_drops_what_connection_names(self):
        # That second part is what people forget, and the part that lets a peer
        # pick which of its own headers a proxy will pass along.
        headers = _message(
            ("Connection", "close, X-Internal"),
            ("X-Internal", "secret"),
            ("X-Forwarded-For", "10.0.0.1"),
        )
        self.assertEqual(httpx.end_to_end(headers), [("X-Forwarded-For", "10.0.0.1")])

    def test_end_to_end_honours_an_explicit_drop_list(self):
        headers = [("X-Drop-Me", "1"), ("X-Keep-Me", "2")]
        self.assertEqual(httpx.end_to_end(headers, drop=["x-drop-me"]), [("X-Keep-Me", "2")])


# --------------------------------------------------------------------------
# No Go counterpart: the header deadline
# --------------------------------------------------------------------------


class TestHeaderDeadline(unittest.TestCase):
    def deadline(self, idle, header):
        ours, theirs = socket.socketpair()
        self.addCleanup(ours.close)
        self.addCleanup(theirs.close)
        return httpx._HeaderDeadline(ours.makefile("rb"), ours, idle, header), theirs

    def test_idle_timeout_bounds_the_wait_for_the_next_request(self):
        # A keep-alive connection may sit quiet between requests, but not forever.
        rd, peer = self.deadline(idle=0.25, header=30.0)
        start = time.monotonic()
        with self.assertRaises(socket.timeout):
            rd.readline()
        self.assertLess(time.monotonic() - start, 3, "the idle timeout should have ended this wait")

    def test_header_timeout_bounds_a_block_that_started_arriving(self):
        # Once the header block starts, the window is the header timeout -- much
        # shorter than the idle wait -- because a header that never finishes
        # arriving is the cheapest attack there is.
        rd, peer = self.deadline(idle=5.0, header=0.3)
        peer.sendall(b"GET / HTTP/1.1\r\n")
        self.assertEqual(rd.readline(), b"GET / HTTP/1.1\r\n")
        peer.sendall(b"Host: example")  # a line that never completes
        start = time.monotonic()
        with self.assertRaises(socket.timeout):
            rd.readline()
        self.assertLess(time.monotonic() - start, 3, "want the header deadline, not the idle wait")

    def test_header_timeout_bounds_a_stalled_request_line(self):
        # The partial request line is the case a per-recv timeout cannot see: the
        # block has started arriving, but no complete line ever comes.
        rd, peer = self.deadline(idle=5.0, header=0.3)
        peer.sendall(b"GET / HTTP/1.1")
        start = time.monotonic()
        with self.assertRaises(socket.timeout):
            rd.readline()
        self.assertLess(time.monotonic() - start, 3, "want the header deadline, not the idle wait")

    def test_the_header_deadline_is_total_not_per_line(self):
        # A peer that sends one header line per second must still be cut off, or
        # the timeout would only bound each line and never the request.
        rd, peer = self.deadline(idle=5.0, header=0.4)
        peer.sendall(b"GET / HTTP/1.1\r\n")
        self.assertEqual(rd.readline(), b"GET / HTTP/1.1\r\n")
        start = time.monotonic()
        with self.assertRaises(socket.timeout):
            while True:
                peer.sendall(b"X-Filler: yes\r\n")
                rd.readline()
        self.assertLess(time.monotonic() - start, 3, "each arriving line must not reset the deadline")

    def test_a_body_has_neither_deadline(self):
        # Go has no read timeout on a body either, which is what makes a slow but
        # real upload possible.
        rd, peer = self.deadline(idle=0.2, header=0.2)
        peer.sendall(b"GET / HTTP/1.1\r\n\r\n")
        rd.readline()
        rd.readline()
        rd.release()

        late = threading.Timer(0.6, peer.sendall, (b"late",))
        late.start()
        self.addCleanup(late.cancel)
        start = time.monotonic()
        self.assertEqual(rd.read(4), b"late", "a body must not be cut off by the header or idle deadline")
        self.assertGreaterEqual(time.monotonic() - start, 0.5)


# --------------------------------------------------------------------------
# No Go counterpart: the server lifecycle
# --------------------------------------------------------------------------


class TestServerStreamsWhileTheGeneratorIsStillBlocked(_ServerTest):
    def test_server_streams_while_the_generator_is_still_blocked(self):
        frame = b'data: {"n": 1}\n\n'
        produced = threading.Event()
        release = threading.Event()

        def frames():
            yield frame
            produced.set()
            if not release.wait(5):
                raise AssertionError("the test never released the generator")
            yield b'data: {"n": 2}\n\n'

        def handler(req, resp):
            resp.send_stream(200, frames(), content_type="text/event-stream")

        server = self.serve(handler)
        conn = self.connect(server)
        conn.request("GET", "/events")
        resp = conn.getresponse()
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.getheader("Transfer-Encoding"), "chunked")

        # The generator has produced one frame and is now blocked, so a list
        # literal here would prove nothing: the frame can only be in the client's
        # hands because it was flushed as it was produced.
        self.assertEqual(resp.read(len(frame)), frame)
        self.assertTrue(produced.wait(5), "the generator never got past its first frame")
        self.assertFalse(release.is_set(), "the frame arrived while the generator was still blocked")

        release.set()
        self.assertEqual(resp.read(len(b'data: {"n": 2}\n\n')), b'data: {"n": 2}\n\n')
        self.assertEqual(resp.read(), b"", "want the chunked stream properly terminated")


class TestServerFinishesAnInflightRequestOnShutdown(_ServerTest):
    def test_server_finishes_an_inflight_request_on_shutdown(self):
        entered = threading.Event()
        release = threading.Event()
        stopped = threading.Event()

        def handler(req, resp):
            entered.set()
            release.wait(10)
            resp.json(200, {"ok": True})

        server = self.serve(handler)
        got = {}

        def client():
            conn = http.client.HTTPConnection("127.0.0.1", server.port(), timeout=10)
            try:
                conn.request("GET", "/slow")
                resp = conn.getresponse()
                got["status"] = resp.status
                got["body"] = resp.read()
            finally:
                conn.close()

        caller = threading.Thread(target=client)
        caller.start()
        self.assertTrue(entered.wait(5), "the request never reached the handler")

        stopper = threading.Thread(target=lambda: (server.shutdown(), stopped.set()))
        stopper.start()
        time.sleep(0.3)
        self.assertFalse(stopped.is_set(), "shutdown cut off a request that was already being served")

        release.set()
        stopper.join(10)
        caller.join(10)
        self.assertTrue(stopped.is_set(), "shutdown did not return once the in-flight request was done")
        self.assertEqual(got.get("status"), 200)
        self.assertIn(b'"ok": true', got.get("body", b""))

        with self.assertRaises(OSError):
            socket.create_connection(("127.0.0.1", server.port()), timeout=2).close()


class TestServerAnswersAMalformedRequestWithText(_ServerTest):
    def test_server_answers_a_malformed_request_with_text(self):
        # A caller parsing JSON should not have to guess which kind of body it got
        # because of how badly it malformed something.
        server = self.serve(lambda req, resp: resp.send_bytes(200, b"never"))
        sock = socket.create_connection(("127.0.0.1", server.port()), timeout=5)
        self.addCleanup(sock.close)
        sock.sendall(b"NOT A REQUEST\r\n\r\n")

        chunks = []
        while True:
            data = sock.recv(4096)
            if not data:
                break
            chunks.append(data)
        reply = b"".join(chunks)
        self.assertIn(b"400", reply)
        self.assertIn(b"Content-Length", reply)
        self.assertNotIn(b"<html", reply.lower(), "want plain text, not the stdlib's error page")


class TestServerTreatsAClientResetAsNothing(_ServerTest):
    def test_server_treats_a_client_reset_as_nothing(self):
        # A browser tab closing mid-request, or a proxy hop dropping, must not put
        # a traceback on the service's stderr -- and must not stop the service.
        server = self.serve(lambda req, resp: resp.json(200, {"ok": True}))
        noise = io.StringIO()
        with contextlib.redirect_stderr(noise):
            sock = socket.create_connection(("127.0.0.1", server.port()), timeout=5)
            sock.sendall(b"GET / HTTP/1.1\r\nHost: both")
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
            sock.close()  # a reset rather than a clean close
            time.sleep(0.5)
        self.assertEqual(noise.getvalue(), "", "a client that vanished is not worth a traceback")

        conn = self.connect(server)
        conn.request("GET", "/")
        resp = conn.getresponse()
        self.assertEqual(resp.status, 200, "the service must still be serving")
        resp.read()


class TestServerServesThroughTheRealRouter(_ServerTest):
    def test_server_serves_through_the_real_router(self):
        router = httpx.Router()
        router.handle("GET", "/health", lambda req, resp: resp.json(200, {"ok": True}))
        server = self.serve(router)

        conn = self.connect(server)
        conn.request("GET", "/health")
        resp = conn.getresponse()
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.getheader("Content-Type"), "application/json")
        self.assertEqual(resp.read(), b'{\n  "ok": true\n}\n')

        conn.request("POST", "/health")
        resp = conn.getresponse()
        self.assertEqual(resp.status, 405)
        self.assertEqual(resp.getheader("Allow"), "GET, HEAD")
        resp.read()

        conn.request("HEAD", "/health")
        resp = conn.getresponse()
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.getheader("Content-Length"), "17")
        self.assertEqual(resp.read(), b"")


class TestServeBindsAndReturnsWhenStopped(unittest.TestCase):
    # `serve` is the whole lifecycle for a single-service process: bind, answer,
    # and come back once something stopped the server.
    def test_serve_binds_and_returns_when_stopped(self):
        started = threading.Event()
        made = []

        class _Recording(httpx.Server):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                made.append(self)

            def start(self):
                super().start()
                started.set()

        with _RecordingPatched(_Recording):
            worker = threading.Thread(
                target=httpx.serve,
                args=("127.0.0.1:0", lambda req, resp: resp.json(200, {"ok": True}), _log()),
            )
            worker.start()
            self.assertTrue(started.wait(5), "serve never started a server")
            server = made[0]

            conn = http.client.HTTPConnection("127.0.0.1", server.port(), timeout=5)
            try:
                conn.request("GET", "/")
                resp = conn.getresponse()
                self.assertIn(b'"ok": true', resp.read())
            finally:
                conn.close()

            server.shutdown()
            worker.join(10)
        self.assertFalse(worker.is_alive(), "serve did not return after the server stopped")


class _RecordingPatched:
    """Patches `httpx.Server` for one block, so `serve` can be watched."""

    def __init__(self, replacement):
        self._replacement = replacement
        self._original = None

    def __enter__(self):
        self._original = httpx.Server
        httpx.Server = self._replacement
        return self._replacement

    def __exit__(self, *exc):
        httpx.Server = self._original
        return False


class TestSplitAddr(unittest.TestCase):
    def test_split_addr(self):
        self.assertEqual(httpx.split_addr("127.0.0.1:7777"), ("127.0.0.1", 7777))
        self.assertEqual(httpx.split_addr(":7777"), ("", 7777), "an empty host means every interface")
        self.assertEqual(httpx.split_addr("[::1]:7777"), ("::1", 7777))
        with self.assertRaises(ConfigError):
            httpx.split_addr("7777")
        with self.assertRaises(ConfigError):
            httpx.split_addr("127.0.0.1:http")
