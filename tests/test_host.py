"""The share side: the allowlist, the meter, the proxy and the announce loop.

Ported from the Go package's host_test.go, peers_test.go, inject_test.go,
routes_test.go, limits_test.go, owner_test.go and serve_test.go -- all of them in
git history now. Those tests were written around one idea worth keeping in the
port: a good half of what a host promises is about what the *engine* does not see
-- not a refused route, not a body over the limit, not a peer's share key -- so
most of what is asserted here is what did not arrive, alongside the status the
peer got.

Two shapes of test live side by side, exactly as they did in Go. Most calls go
straight through `Host.handler` with a recording writer, which is what makes a
refusal exact and instant. Anything about concurrency, streaming or serving needs
a real socket, so those use `httpx.Server` and a real client -- a test of a
stream that never leaves the process would prove nothing about a stream.
"""

from __future__ import annotations

import http.client
import io
import json
import logging
import socket
import threading
import time
import unittest
from email.message import Message
from typing import Any, Dict, List, Optional, Tuple

from bothy import engine, host, httpx, meter, model, registry
from bothy.errors import BothyError, ConfigError

# The engine's answer to a whole completion, with the usage the sniffer reads.
_USAGE_BODY: Dict[str, Any] = {
    "choices": [{"message": {"content": "from the engine"}}],
    "usage": {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7},
}

# A completion with no usage at all, which is what an engine that reports nothing
# looks like.
_NO_USAGE_BODY: Dict[str, Any] = {"choices": [{"message": {"content": "no counts here"}}]}

_CHAT_BODY = '{"model":"llama3.1:8b","messages":[]}'


# ---------------------------------------------------------------------------
# Helpers: the recorder, the request, the fake engine, the fake registry
# ---------------------------------------------------------------------------


class _Recorder:
    """The stdlib handler surface that `Response` writes through.

    Stands in for `BaseHTTPRequestHandler`, so a handler can be exercised without
    a socket the way Go's recorder tests use `httptest.NewRecorder`.
    """

    def __init__(self) -> None:
        self.status = 0
        self.headers: List[Tuple[str, str]] = []
        self.wfile = io.BytesIO()
        self.close_connection = False

    def send_response_only(self, code, message=None) -> None:
        self.status = code

    def send_header(self, keyword, value) -> None:
        self.headers.append((keyword, str(value)))

    def end_headers(self) -> None:
        pass

    def header(self, name: str) -> Optional[str]:
        for key, value in self.headers:
            if key.lower() == name.lower():
                return value
        return None

    def body(self) -> bytes:
        """The body as a client would read it: an undeclared length is written
        with chunked framing, and that framing is not part of the body."""
        data = self.wfile.getvalue()
        if (self.header("Transfer-Encoding") or "").lower() == "chunked":
            return _dechunk(data)
        return data

    def json(self) -> Any:
        return json.loads(self.body().decode("utf-8"))

    def error_message(self) -> str:
        """The message out of a refusal body, in the documented error shape."""
        return self.json()["error"]["message"]


def _dechunk(data: bytes) -> bytes:
    """Decode chunked framing, the way a client does."""
    out = bytearray()
    rest = data
    while True:
        line, sep, rest = rest.partition(b"\r\n")
        if not sep:
            return bytes(out)
        size = int(line.split(b";", 1)[0].strip() or b"0", 16)
        if size == 0:
            return bytes(out)
        out += rest[:size]
        rest = rest[size + 2 :]


def _frame(data: bytes) -> bytes:
    """Chunked-encode a body, in two pieces, so the decoder is exercised."""
    half = max(len(data) // 2, 1)
    parts = [data[:half], data[half:]] if data else []
    return b"".join(b"%x\r\n%s\r\n" % (len(p), p) for p in parts) + b"0\r\n\r\n"


def _message(*pairs) -> Message:
    """A case-insensitive header bag, the shape `Request.headers` really is."""
    bag = Message()
    for key, value in pairs:
        bag[key] = value
    return bag


def _request(method="GET", path="/", key=None, body=b"", headers=(), remote="127.0.0.1:1234", chunked=False, query=None):
    """A request as the server would hand it to a handler."""
    pairs = list(headers)
    if key:
        pairs.append((httpx.KEY_HEADER, key))
    raw = body.encode("utf-8") if isinstance(body, str) else body
    reader = io.BytesIO(raw)
    if chunked:
        reader = io.BytesIO(_frame(raw))
        pairs.append(("Transfer-Encoding", "chunked"))
    elif raw:
        pairs.append(("Content-Length", str(len(raw))))
    return httpx.Request(method, path, dict(query or {}), _message(*pairs), reader, remote)


def _call(h, method, path, key=None, body=b"", headers=(), remote="127.0.0.1:1234", chunked=False, query=None):
    """One request through the host's handler, with a recording writer.

    Go's `do` helper: no socket, so a refusal is asserted exactly and instantly.
    """
    req = _request(method, path, key=key, body=body, headers=headers, remote=remote, chunked=chunked, query=query)
    rec = _Recorder()
    h.handler()(req, httpx.Response(req, rec))
    return rec, req


class _Capture(logging.Handler):
    """Collects the lines a logger emitted, which is what a log test asserts on."""

    def __init__(self) -> None:
        super().__init__()
        self.lines: List[str] = []

    def emit(self, record) -> None:
        self.lines.append(record.getMessage())

    def text(self) -> str:
        return "\n".join(self.lines)


def _log(name: str = "host", capture: Optional[_Capture] = None) -> logging.Logger:
    log = logging.getLogger("bothy.host.test.%s" % name)
    log.setLevel(logging.INFO)
    log.propagate = False
    log.handlers[:] = [capture or logging.NullHandler()]
    return log


def _captured(name: str = "captured") -> Tuple[logging.Logger, _Capture]:
    """A logger and the lines it collects, for the promises only ever said out loud."""
    capture = _Capture()
    return _log(name, capture), capture


def _asked_for_usage(body: bytes) -> bool:
    """Whether a request asked the engine to report usage on its stream."""
    try:
        parsed = json.loads(body or b"{}")
    except ValueError:
        return False
    if not isinstance(parsed, dict):
        return False
    options = parsed.get("stream_options")
    return isinstance(options, dict) and options.get("include_usage") is True


class FakeEngine:
    """Stands in for Ollama or vLLM, on a real socket.

    It records what it was sent, because the promises about what the engine does
    *not* see are half of the point of a host; and it can hold a request at the
    door so a test can fill a host's cap for real rather than by pretending.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.paths: List[str] = []
        self.headers: List[Message] = []
        self.hosts: List[str] = []
        self.bodies: List[bytes] = []
        self.queries: List[Dict[str, List[str]]] = []
        self.arrived: Optional[threading.Semaphore] = None
        self.release: Optional[threading.Event] = None
        self.status = 200
        self.payload: Any = _USAGE_BODY
        self.content_type = "application/json"
        self.raw_body: Optional[bytes] = None
        self.frames: Optional[List[bytes]] = None
        self.gates: Optional[List[Optional[threading.Event]]] = None
        self._server = httpx.Server("127.0.0.1:0", self._handle, _log("fake-engine"))
        self._server.start()

    @property
    def url(self) -> str:
        return "http://127.0.0.1:%d" % self._server.port()

    @property
    def authority(self) -> str:
        """What the engine expects to be called, which is its own Host header."""
        return "127.0.0.1:%d" % self._server.port()

    def close(self) -> None:
        self._server.shutdown()

    def hold(self, release: threading.Event) -> None:
        self.release = release

    def announce(self, arrived: threading.Semaphore) -> None:
        self.arrived = arrived

    def seen(self) -> Tuple[List[str], List[Message], List[str]]:
        with self._lock:
            return list(self.paths), list(self.headers), list(self.hosts)

    def stream_usage(self, gates: Optional[List[Optional[threading.Event]]] = None) -> None:
        """Answer with a stream whose final frame carries the usage.

        An OpenAI-compatible engine reports usage in a stream only when the
        request asked for it, which is what the injection exists to make happen,
        so the usage frame is dropped when the ask did not arrive. The frames are
        gated so a test can prove one reached the client before the next was even
        produced.
        """
        self.content_type = "text/event-stream"
        self.frames = [
            b'data: {"choices":[{"delta":{"content":"one"}}]}\n\n',
            b'data: {"choices":[{"delta":{"content":"two"}}]}\n\n',
            b'data: {"usage":{"prompt_tokens":5,"completion_tokens":6}}\n\n',
            b"data: [DONE]\n\n",
        ]
        self.gates = gates

    def _handle(self, req: httpx.Request, resp: httpx.Response) -> None:
        body = b"".join(req.body_chunks())
        with self._lock:
            self.paths.append(req.path)
            self.headers.append(req.headers)
            self.hosts.append(req.headers.get("Host") or "")
            self.bodies.append(body)
            self.queries.append(dict(req.query))
            arrived, release = self.arrived, self.release
            status, payload, ctype = self.status, self.payload, self.content_type
            raw_body, frames, gates = self.raw_body, self.frames, self.gates
        if arrived is not None:
            arrived.release()
        if release is not None:
            release.wait(10)
        if frames is not None:
            resp.send_stream(status, self._stream(frames, gates, body), content_type=ctype)
            return
        if raw_body is not None:
            resp.send_bytes(status, raw_body, content_type=ctype)
            return
        resp.send_bytes(status, json.dumps(payload).encode("utf-8"), content_type=ctype)

    def _stream(self, frames, gates, body):
        asked = _asked_for_usage(body)
        for index, frame in enumerate(frames):
            if b'"usage"' in frame and not asked:
                continue  # a stream that was not asked reports nothing
            if gates is not None and index < len(gates) and gates[index] is not None:
                gates[index].wait(10)
            yield frame


class RecordingRegistry:
    """A registry that counts announcements and keeps the last body, so what a
    host advertises can be asserted rather than assumed."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._bodies: List[str] = []
        self._server = httpx.Server("127.0.0.1:0", self._handle, _log("fake-registry"))
        self._server.start()

    @property
    def url(self) -> str:
        return "http://127.0.0.1:%d" % self._server.port()

    def close(self) -> None:
        self._server.shutdown()

    def count(self) -> int:
        with self._lock:
            return len(self._bodies)

    def last(self) -> str:
        with self._lock:
            return self._bodies[-1] if self._bodies else ""

    def entries(self) -> List[Dict[str, Any]]:
        """The entries of the last registration, or an empty list for none."""
        if not self.last():
            return []
        return json.loads(self.last())["entries"]

    def _handle(self, req: httpx.Request, resp: httpx.Response) -> None:
        body = b"".join(req.body_chunks())
        with self._lock:
            self._bodies.append(body.decode("utf-8"))
        resp.json(200, {"registered": 1})


def _test_models() -> List[model.Model]:
    return [model.Model(name="llama3.1:8b", digest="sha256:1111")]


def _test_config(**overrides) -> host.Config:
    """Go's newTestHost: a host in front of an engine, with the defaults a test
    would otherwise repeat. The engine kind is static so no probe is needed and
    the test is only about the host."""
    fields: Dict[str, Any] = {
        "listen": "127.0.0.1:0",
        "engine_url": "http://127.0.0.1:1",
        "engine_kind": "static",
        "public_address": "host:7777",
        "heartbeat": 3600.0,
        "engine": engine.Options(static=_test_models()),
    }
    fields.update(overrides)
    return host.Config(**fields)


def _test_host(**overrides) -> host.Host:
    return host.new(_test_config(**overrides), _log("test-host"))


def _free_port() -> int:
    """A port nobody is using, which is what a test on a busy machine needs."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_until(predicate, timeout: float = 5.0, interval: float = 0.02) -> bool:
    """Poll for a condition, which is what a test has to do when the work it is
    waiting for happens on another thread."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def _read_until(sock: socket.socket, marker: bytes, timeout: float = 10.0) -> bytes:
    """Read from a socket until the marker appears.

    Reading a stream by hand is the only way to assert that a frame arrived while
    the next one was still being produced: a buffering proxy would leave this
    blocked until the whole body was over.
    """
    deadline = time.monotonic() + timeout
    data = bytearray()
    while marker not in data:
        left = deadline - time.monotonic()
        if left <= 0:
            raise AssertionError("never saw %r in %r" % (marker, bytes(data)))
        sock.settimeout(left)
        try:
            chunk = sock.recv(4096)
        except socket.timeout:
            raise AssertionError("timed out waiting for %r in %r" % (marker, bytes(data))) from None
        if not chunk:
            raise AssertionError("the connection closed before %r arrived: %r" % (marker, bytes(data)))
        data += chunk
    return bytes(data)


def _post(port: int, path: str, key: str, body: str, timeout: float = 10.0):
    """One request over a real socket, returning (status, body)."""
    payload = body.encode("utf-8")
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        conn.request("POST", path, body=payload, headers={httpx.KEY_HEADER: key})
        resp = conn.getresponse()
        return resp.status, resp.read()
    finally:
        conn.close()


def _get(port: int, path: str, key: str = "", timeout: float = 10.0):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        headers = {httpx.KEY_HEADER: key} if key else {}
        conn.request("GET", path, headers=headers)
        resp = conn.getresponse()
        return resp.status, resp.read()
    finally:
        conn.close()


class _HostServerMixin:
    """A real host on a real port, for the tests that need one."""

    def serve_host(self, h):
        server = httpx.Server("127.0.0.1:0", h.handler(), _log("host-server"))
        self.addCleanup(server.shutdown)
        server.start()
        return server


# ---------------------------------------------------------------------------
# The engine never sees the share key (host_test.go)
# ---------------------------------------------------------------------------


class TestTheEngineNeverSeesTheShareKey(unittest.TestCase):
    # SECURITY.md and PROTOCOL.md both promise that a share key is stripped before
    # the request reaches the engine. A careless reorder would leak every peer's
    # key into the engine's logs with the whole suite still green.
    def test_the_engine_never_sees_the_share_key(self):
        for name, header, value in (
            ("the key header", httpx.KEY_HEADER, "key-a"),
            ("bearer auth", "Authorization", "Bearer key-a"),
        ):
            with self.subTest(name):
                fake = FakeEngine()
                self.addCleanup(fake.close)
                h = _test_host(engine_url=fake.url, share_keys="alice:key-a")

                rec, _req = _call(h, "POST", "/v1/chat/completions", body=_CHAT_BODY, headers=[(header, value)])
                self.assertEqual(rec.status, 200, "the request never reached the engine: %s" % rec.body())

                _paths, headers, hosts = fake.seen()
                self.assertEqual(len(headers), 1, "the engine saw %d requests, want 1" % len(headers))
                self.assertEqual(headers[0].get(httpx.KEY_HEADER) or "", "", "the engine received the share key")
                self.assertEqual(headers[0].get("Authorization") or "", "", "the engine received an Authorization header")
                # And it is told its own name, not ours.
                self.assertEqual(hosts[0], fake.authority, "the engine saw Host %r, want its own" % hosts[0])


# ---------------------------------------------------------------------------
# The cap, the rate limit, the reserve, the pause (host_test.go, owner_test.go)
# ---------------------------------------------------------------------------


class TestTheConcurrencyCapRefusesRatherThanQueues(_HostServerMixin, unittest.TestCase):
    # "Set BOTHY_MAX_CONCURRENT. A GPU serialises work anyway; the cap is what
    # stops one peer from occupying it indefinitely." The meter's own logic is
    # tested there; this is the part that was not: that the host actually refuses,
    # counts the refusal, and does not reach the engine.
    def test_the_concurrency_cap_refuses_rather_than_queues(self):
        fake = FakeEngine()
        self.addCleanup(fake.close)
        release = threading.Event()
        arrived = threading.Semaphore(0)
        fake.hold(release)
        fake.announce(arrived)
        self.addCleanup(release.set)

        h = _test_host(engine_url=fake.url, share_key="k", max_concurrent=2)
        server = self.serve_host(h)

        results: List[Any] = [None, None]

        def one(index):
            results[index] = _post(server.port(), "/v1/chat/completions", "k", _CHAT_BODY)

        threads = [threading.Thread(target=one, args=(i,)) for i in range(2)]
        for thread in threads:
            thread.start()
        # Only once both are genuinely through to the engine is the cap full.
        for _ in range(2):
            self.assertTrue(arrived.acquire(timeout=5), "the first two requests never reached the engine")

        third, _req = _call(h, "POST", "/v1/chat/completions", "k", _CHAT_BODY)
        self.assertEqual(third.status, 429, "a request over the cap got %d, want 429" % third.status)
        self.assertEqual(len(fake.seen()[1]), 2, "a refused request must not be forwarded to the engine")

        usage, _req = _call(h, "GET", "/bothy/usage", "k")
        rows = usage.json()["peers"]
        self.assertEqual(len(rows), 1, "usage = %r, want one peer" % rows)
        self.assertEqual(rows[0]["limited"], 1, "the refusal was not counted")

        # Let the two through and check they were served. A test that walked away
        # leaving two requests blocked at the engine would pass while proving less
        # than it looks like it proves.
        release.set()
        for thread in threads:
            thread.join(timeout=10)
        for index, result in enumerate(results):
            self.assertIsNotNone(result, "request %d never finished" % index)
            self.assertEqual(result[0], 200, "a request within the cap got %r" % (result,))


class TestTheOwnerReserveIsHeldBackOnTheWire(_HostServerMixin, unittest.TestCase):
    # The feature end to end: a host whose cap would allow more, refusing because
    # the extra slot is not the peers'.
    def test_the_owner_reserve_is_held_back_on_the_wire(self):
        fake = FakeEngine()
        self.addCleanup(fake.close)
        release = threading.Event()
        arrived = threading.Semaphore(0)
        fake.hold(release)
        fake.announce(arrived)
        self.addCleanup(release.set)

        h = _test_host(engine_url=fake.url, share_key="k", max_concurrent=2, owner_reserve=1)
        server = self.serve_host(h)
        held: List[Any] = []

        def one():
            held.append(_post(server.port(), "/v1/chat/completions", "k", _CHAT_BODY))

        thread = threading.Thread(target=one)
        thread.start()
        self.assertTrue(arrived.acquire(timeout=5), "the first request never reached the engine")

        second, _req = _call(h, "POST", "/v1/chat/completions", "k", _CHAT_BODY)
        self.assertEqual(
            second.status, 429, "a request beyond the peer slots got %d, want 429" % second.status
        )
        self.assertEqual(len(fake.seen()[1]), 1, "the engine saw a request it should never have seen")

        # And the reservation is visible rather than a mystery about why a host
        # with a cap of 2 refuses the second request.
        for path in ("/bothy/healthz", "/bothy/usage"):
            rec, _req = _call(h, "GET", path, "k")
            body = rec.json()
            self.assertEqual(body["owner_reserve"], 1, "%s owner_reserve" % path)
            self.assertEqual(body["peer_slots"], 1, "%s peer_slots" % path)
            self.assertEqual(body["free"], 0, "%s capacity: a reserved slot must not be advertised" % path)

        release.set()
        thread.join(timeout=10)
        self.assertEqual(held[0][0], 200, "the held request was not served: %r" % (held,))


class TestNoReserveIsReportedAsNoReserve(unittest.TestCase):
    def test_no_reserve_is_reported_as_no_reserve(self):
        h = _test_host(share_key="k", max_concurrent=3)
        rec, _req = _call(h, "GET", "/bothy/healthz", "k")
        body = rec.json()
        self.assertEqual(body["owner_reserve"], 0, "owner_reserve")
        self.assertEqual(body["peer_slots"], 3, "peer_slots")


class TestTheRateLimitIsPerPeerAndSaysWhenToRetry(unittest.TestCase):
    # The README calls the rate limit per-peer, which is the whole reason to have
    # one: a noisy consumer should not slow anyone else down. Retry-After is part
    # of that contract, since a client that knows when to come back does not hammer.
    def test_the_rate_limit_is_per_peer_and_says_when_to_retry(self):
        fake = FakeEngine()
        self.addCleanup(fake.close)
        h = _test_host(
            engine_url=fake.url,
            share_keys="alice:key-a,bob:key-b",
            requests_per_minute=1,
            max_concurrent=0,
        )

        first, _req = _call(h, "POST", "/v1/chat/completions", "key-a", _CHAT_BODY)
        self.assertEqual(first.status, 200, "alice's first request got %d" % first.status)

        second, _req = _call(h, "POST", "/v1/chat/completions", "key-a", _CHAT_BODY)
        self.assertEqual(second.status, 429, "alice's second request got %d, want 429" % second.status)
        retry = second.header("Retry-After") or ""
        self.assertTrue(retry.isdigit() and int(retry) >= 1, "Retry-After = %r, want a positive number of seconds" % retry)

        # Bob is a different peer, so alice's limit must not touch him.
        bob, _req = _call(h, "POST", "/v1/chat/completions", "key-b", _CHAT_BODY)
        self.assertEqual(bob.status, 200, "bob got %d while alice was over her limit, want 200" % bob.status)


class TestPausedRefusesPeersWithoutCountingItAgainstThem(unittest.TestCase):
    # The refusal is not the peer's doing, so it must not look like one in the
    # usage report, and it must not be the 429 that means "too fast".
    def test_paused_refuses_peers_without_counting_it_against_them(self):
        fake = FakeEngine()
        self.addCleanup(fake.close)
        h = _test_host(engine_url=fake.url, share_key="k", max_concurrent=4, paused=True)

        rec, _req = _call(h, "POST", "/v1/chat/completions", "k", _CHAT_BODY)
        self.assertEqual(rec.status, 503, "a paused host answered %d, want 503: %s" % (rec.status, rec.body()))
        self.assertIn("paused", rec.error_message(), "the refusal does not say the host is paused")
        self.assertIsNone(rec.header("Retry-After"), "a pause is not a rate limit, so there is no retry-after")
        self.assertEqual(len(fake.seen()[1]), 0, "the engine saw a request while the host was paused")

        health, _req = _call(h, "GET", "/bothy/healthz", "k")
        self.assertIs(health.json()["paused"], True, "healthz does not say the host is paused")

        # Nothing was counted, so no peer row claims a refusal they did not earn.
        usage, _req = _call(h, "GET", "/bothy/usage", "k")
        self.assertEqual(usage.json()["peers"], [], "a pause is not a peer's usage")


class TestAPeerBudgetIsAFinalAnswerAndSaysWhenToComeBack(unittest.TestCase):
    # Unlike a rate limit, a spent budget is not refilled by waiting a moment, so
    # it is the one refusal where the peer most needs to be told when to try again.
    def test_a_peer_budget_is_a_final_answer_and_says_when_to_come_back(self):
        fake = FakeEngine()
        self.addCleanup(fake.close)
        h = _test_host(engine_url=fake.url, share_key="k", max_concurrent=10, peer_quota="2/1h")

        for index in range(2):
            rec, _req = _call(h, "POST", "/v1/chat/completions", "k", _CHAT_BODY)
            self.assertEqual(rec.status, 200, "request %d of the budget got %d" % (index + 1, rec.status))

        rec, _req = _call(h, "POST", "/v1/chat/completions", "k", _CHAT_BODY)
        self.assertEqual(rec.status, 429, "a request over budget got %d, want 429" % rec.status)
        self.assertIn("budget", rec.error_message(), "the refusal does not say it is a budget")
        retry = rec.header("Retry-After") or ""
        self.assertNotEqual(retry, "", "a refusal with no Retry-After leaves the peer guessing")
        self.assertNotEqual(retry, "0", "Retry-After is 0 for an hour-long budget")
        self.assertEqual(len(fake.seen()[1]), 2, "a refused request must not be forwarded")

        usage, _req = _call(h, "GET", "/bothy/usage", "k")
        self.assertEqual(usage.json()["peer_quota"], "2/1h", "usage does not report the budget")


# ---------------------------------------------------------------------------
# What the proxy will and will not forward (limits_test.go, routes)
# ---------------------------------------------------------------------------


class TestEngineControlRoutesAreNotProxied(unittest.TestCase):
    # With no share key -- the supported way to run a public host -- reaching an
    # engine's control routes took nothing but a port number. The assertion is not
    # only that the peer is refused, but that the engine never hears about it.
    def test_engine_control_routes_are_not_proxied(self):
        for method, path in (
            ("DELETE", "/api/delete"),
            ("POST", "/api/pull"),
            ("POST", "/api/push"),
            ("POST", "/api/create"),
            ("POST", "/api/copy"),
            ("GET", "/api/blobs/sha256:abc"),
            ("POST", "/v1/files"),
            ("GET", "/metrics"),
        ):
            with self.subTest("%s %s" % (method, path)):
                fake = FakeEngine()
                self.addCleanup(fake.close)
                h = _test_host(engine_url=fake.url, share_keys="alice:key-a")

                rec, _req = _call(h, method, path, "key-a", '{"name":"llama3.1:8b"}')
                self.assertEqual(rec.status, 404, "the engine's control routes are not inference")
                self.assertIn("inference routes only", rec.error_message(), "the refusal does not say why")
                self.assertEqual(fake.seen()[0], [], "the engine was asked for a route it must never see")


class TestInferenceRoutesAreProxied(unittest.TestCase):
    def test_inference_routes_are_proxied(self):
        for method, path in (
            ("POST", "/v1/chat/completions"),
            ("POST", "/v1/completions"),
            ("POST", "/v1/embeddings"),
            ("GET", "/v1/models"),
            ("GET", "/v1/models/llama3.1:8b"),
            ("POST", "/api/chat"),
            ("POST", "/api/generate"),
            ("GET", "/api/tags"),
            ("GET", "/api/ps"),
            ("POST", "/api/show"),
        ):
            with self.subTest("%s %s" % (method, path)):
                fake = FakeEngine()
                self.addCleanup(fake.close)
                h = _test_host(engine_url=fake.url, share_keys="alice:key-a")

                rec, _req = _call(h, method, path, "key-a", _CHAT_BODY)
                self.assertEqual(rec.status, 200, "status %d: %s" % (rec.status, rec.body()))
                self.assertEqual(fake.seen()[0], [path], "the engine was asked for %r" % fake.seen()[0])


class TestTheEngineSeesThePathAndQuery(unittest.TestCase):
    # A proxied request keeps its query string, because that is how an engine
    # takes its parameters, and an engine URL with a path prefix keeps it: Go's
    # proxy joins the two, and an engine mounted under a prefix is not rare.
    def test_a_query_string_is_forwarded(self):
        fake = FakeEngine()
        self.addCleanup(fake.close)
        h = _test_host(engine_url=fake.url, share_key="k")

        rec, _req = _call(h, "GET", "/api/tags", "k", query={"model": ["llama3.1:8b"]})
        self.assertEqual(rec.status, 200, "the request was refused: %s" % rec.body())
        self.assertEqual(fake.queries, [{"model": ["llama3.1:8b"]}], "the engine lost the query string")

    def test_a_base_path_is_kept(self):
        fake = FakeEngine()
        self.addCleanup(fake.close)
        h = _test_host(engine_url=fake.url + "/mounted", share_key="k")

        rec, _req = _call(h, "GET", "/v1/models", "k")
        self.assertEqual(rec.status, 200, "the request was refused: %s" % rec.body())
        self.assertEqual(fake.seen()[0], ["/mounted/v1/models"], "the engine's base path was dropped")


class TestAllowAllRoutesRestoresTheWholeEngine(unittest.TestCase):
    # The old behaviour is still available, deliberately, because a private
    # network may want the engine's whole API. It has to be asked for.
    def test_allow_all_routes_restores_the_whole_engine(self):
        fake = FakeEngine()
        self.addCleanup(fake.close)
        h = _test_host(engine_url=fake.url, share_keys="alice:key-a", allow_all_routes=True)

        rec, _req = _call(h, "DELETE", "/api/delete", "key-a", '{"name":"m"}')
        self.assertEqual(rec.status, 200, "the delete route was not proxied: %s" % rec.body())
        self.assertEqual(fake.seen()[0], ["/api/delete"], "the engine did not see the delete route")


class TestExtraRoutesOpenOnePathAndNotTheRest(unittest.TestCase):
    # An engine Bothy does not know needs its own routes, and an operator can open
    # exactly those rather than everything.
    def test_extra_routes_open_one_path_and_not_the_rest(self):
        fake = FakeEngine()
        self.addCleanup(fake.close)
        h = _test_host(
            engine_url=fake.url,
            share_keys="alice:key-a",
            allow_routes=[host.RouteRule(method="POST", path="/api/pull")],
        )

        opened, _req = _call(h, "POST", "/api/pull", "key-a", "{}")
        self.assertEqual(opened.status, 200, "an opened route was refused: %d %s" % (opened.status, opened.body()))
        closed, _req = _call(h, "DELETE", "/api/delete", "key-a", "{}")
        self.assertEqual(closed.status, 404, "opening one route must not open the others")


class TestParseRoutes(unittest.TestCase):
    def test_parse_routes(self):
        rules = host.parse_routes("POST /api/pull, /api/blobs/,GET /api/tags")
        self.assertEqual(
            rules,
            [
                host.RouteRule(method="POST", path="/api/pull"),
                host.RouteRule(method="", path="/api/blobs/"),
                host.RouteRule(method="GET", path="/api/tags"),
            ],
            "rules are a method and a path, in the order they were written",
        )
        self.assertEqual(host.parse_routes(""), [], "an empty spec opens nothing")
        self.assertEqual(host.parse_routes("  ,  "), [], "blank items are not rules")

    def test_a_rule_without_a_leading_slash_is_refused(self):
        for spec in ("api/pull", "POST pull", "POST"):
            with self.subTest(spec):
                with self.assertRaises(ConfigError) as caught:
                    host.parse_routes(spec)
                self.assertIn("bad route", str(caught.exception), "the refusal does not name the rule")

    def test_a_rule_matches_the_method_it_names(self):
        cfg = host.Config(allow_routes=host.parse_routes("POST /api/blobs/,GET /api/tags"))
        self.assertTrue(cfg.allows_route("POST", "/api/blobs/sha256:abc"), "a subtree rule must match under it")
        self.assertFalse(cfg.allows_route("GET", "/api/blobs/sha256:abc"), "the method on the rule is not advisory")
        self.assertTrue(cfg.allows_route("GET", "/api/tags"), "an exact rule must match")
        self.assertFalse(cfg.allows_route("POST", "/api/tags"), "an exact rule is not a subtree")
        ruleless = host.Config(allow_routes=[host.RouteRule(path="/api/version")])
        self.assertTrue(ruleless.allows_route("PUT", "/api/version"), "a rule with no method applies to every one")


class TestARefusedRouteSpendsNoBudget(unittest.TestCase):
    # A route this host will not proxy is a policy, not a shortage, so a peer
    # cannot use it up: if it did, a probe would be a way to spend somebody's
    # budget without any work being done.
    def test_a_refused_route_spends_no_budget(self):
        fake = FakeEngine()
        self.addCleanup(fake.close)
        h = _test_host(engine_url=fake.url, share_key="k", peer_quota="1/1h", max_concurrent=4)

        for _ in range(3):
            rec, _req = _call(h, "DELETE", "/api/delete", "k", "{}")
            self.assertEqual(rec.status, 404, "a control route was not refused")

        served, _req = _call(h, "POST", "/v1/chat/completions", "k", _CHAT_BODY)
        self.assertEqual(served.status, 200, "the refusals spent the peer's budget: %s" % served.body())
        over, _req = _call(h, "POST", "/v1/chat/completions", "k", _CHAT_BODY)
        self.assertEqual(over.status, 429, "the budget of one request was not enforced")
        self.assertEqual(len(fake.seen()[1]), 1, "a refused route reached the engine")


# ---------------------------------------------------------------------------
# Bodies, time limits and an engine that cannot be reached (limits_test.go)
# ---------------------------------------------------------------------------


class TestABodyOverTheLimitNeverReachesTheEngine(unittest.TestCase):
    # A body nobody bounded arrives until the host runs out of room.
    def test_a_body_over_the_limit_never_reaches_the_engine(self):
        fake = FakeEngine()
        self.addCleanup(fake.close)
        h = _test_host(engine_url=fake.url, share_keys="alice:key-a", max_body=64)

        big = '{"model":"llama3.1:8b","prompt":"%s"}' % ("x" * 200)
        rec, _req = _call(h, "POST", "/v1/chat/completions", "key-a", big)
        self.assertEqual(rec.status, 413, "a body over the limit got %d, want 413" % rec.status)
        self.assertEqual(fake.seen()[0], [], "the engine was asked for a body it must never see")

        under, _req = _call(h, "POST", "/v1/chat/completions", "key-a", '{"model":"m"}')
        self.assertEqual(under.status, 200, "a body under the limit was refused: %d" % under.status)

    def test_a_chunked_body_over_the_limit_is_refused_while_it_arrives(self):
        # The declared length is the free half of the limit; this is the half that
        # catches a client which declared nothing.
        fake = FakeEngine()
        self.addCleanup(fake.close)
        h = _test_host(engine_url=fake.url, share_keys="alice:key-a", max_body=64)

        big = '{"model":"llama3.1:8b","prompt":"%s"}' % ("x" * 400)
        rec, _req = _call(h, "POST", "/v1/chat/completions", "key-a", big, chunked=True)
        self.assertEqual(rec.status, 413, "a chunked body over the limit got %d, want 413" % rec.status)
        self.assertEqual(fake.seen()[0], [], "the engine was asked for a body it must never see")

        under, _req = _call(h, "POST", "/v1/chat/completions", "key-a", '{"model":"m"}', chunked=True)
        self.assertEqual(under.status, 200, "a chunked body under the limit was refused: %d" % under.status)


class TestARequestPastTheTimeLimitIsReportedAsATimeout(unittest.TestCase):
    # One generation can hold the GPU for as long as it likes unless the host says
    # otherwise, and the answer to the caller has to say which limit stopped it.
    def test_a_request_past_the_time_limit_is_reported_as_a_timeout(self):
        fake = FakeEngine()
        self.addCleanup(fake.close)
        release = threading.Event()
        fake.hold(release)
        self.addCleanup(release.set)
        h = _test_host(engine_url=fake.url, share_keys="alice:key-a", max_request_time=0.05)

        rec, _req = _call(h, "POST", "/v1/chat/completions", "key-a", '{"model":"m"}')
        self.assertEqual(rec.status, 504, "status = %d, want 504 for the host's own time limit" % rec.status)
        self.assertIn("time limit", rec.error_message(), "the refusal does not name the time limit")
        # The engine was reached and held, so the slot has to be released anyway.
        self.assertEqual(h._meter.in_flight(), 0, "the slot was not released after a timed-out request")


class TestAnUnreachableEngineIsABadGateway(unittest.TestCase):
    # An engine that dies mid-session has to be a clear failure at the edge,
    # because a client that gets a hang cannot tell it from a slow model.
    def test_an_unreachable_engine_is_a_bad_gateway(self):
        h = _test_host(engine_url="http://127.0.0.1:%d" % _free_port(), share_key="k", max_concurrent=4)
        rec, _req = _call(h, "POST", "/v1/chat/completions", "k", _CHAT_BODY)

        self.assertEqual(rec.status, 502, "status = %d, want 502: %s" % (rec.status, rec.body()))
        self.assertIn("engine unreachable", rec.error_message(), "the refusal does not say the engine was unreachable")
        # The slot was taken before the request went out, so a failed request must
        # give it back -- otherwise an engine outage quietly fills the host's cap.
        self.assertEqual(h._meter.in_flight(), 0, "InFlight() after a failed request, want 0")


# ---------------------------------------------------------------------------
# The host's own routes (host_test.go)
# ---------------------------------------------------------------------------


class TestHealthIsOpenAndEverythingElseIsNot(unittest.TestCase):
    # Health has to stay unauthenticated or a container healthcheck needs a key,
    # and everything else has to refuse without one.
    def test_health_is_open_and_everything_else_is_not(self):
        fake = FakeEngine()
        self.addCleanup(fake.close)
        h = _test_host(engine_url=fake.url, share_key="k")

        health, _req = _call(h, "GET", "/bothy/healthz")
        self.assertEqual(health.status, 200, "healthz without a key got %d, want 200" % health.status)
        self.assertIs(health.json()["key_required"], True, "healthz does not say a key is required")

        for path in ("/v1/models", "/bothy/models", "/bothy/usage"):
            rec, _req = _call(h, "GET", path)
            self.assertEqual(rec.status, 401, "%s without a key got %d, want 401" % (path, rec.status))
            self.assertIn("error", rec.json(), "a refusal has to be the OpenAI error shape: %s" % rec.body())


class TestUsageAndModelsReportWhatTheProtocolDescribes(unittest.TestCase):
    # The two routes a client reads over the protocol, plus the meter wiring
    # behind them: a proxied request has to come back countable, per peer.
    def test_usage_and_models_report_what_the_protocol_describes(self):
        fake = FakeEngine()
        self.addCleanup(fake.close)
        h = _test_host(engine_url=fake.url, share_keys="alice:key-a")
        h.announce()

        served, _req = _call(h, "POST", "/v1/chat/completions", "key-a", _CHAT_BODY)
        self.assertEqual(served.status, 200, "the proxied request got %d" % served.status)

        rec, _req = _call(h, "GET", "/bothy/models", "key-a")
        self.assertEqual(rec.status, 200, "/bothy/models got %d" % rec.status)
        served_models = rec.json()
        self.assertEqual(served_models["address"], "host:7777", "the address a client should dial")
        self.assertEqual(len(served_models["models"]), 1, "the models on offer")
        self.assertEqual(served_models["models"][0]["digest"], "sha256:1111", "the digest a client verifies")

        rec, _req = _call(h, "GET", "/bothy/usage", "key-a")
        self.assertEqual(rec.status, 200, "/bothy/usage got %d" % rec.status)
        usage = rec.json()
        self.assertEqual(len(usage["peers"]), 1, "usage reports %d peers, want 1" % len(usage["peers"]))
        row = usage["peers"][0]
        self.assertEqual(row["peer"], "alice", "the peer a key is attributed to")
        self.assertEqual(row["requests"], 1, "one request was served")
        # The counts came from the engine's reported usage, through the sniffer.
        self.assertEqual((row["prompt_tokens"], row["completion_tokens"]), (3, 4), "the response was not metered")
        self.assertEqual(row["unmetered_responses"], 0, "an engine that reports usage is not unmetered")


class TestTheUsageRouteSerialisesEveryDocumentedField(unittest.TestCase):
    # A route that 500s is invisible to a test that only calls the meter, so this
    # one goes over the wire and reads the row as PROTOCOL.md documents it.
    def test_the_usage_route_serialises_every_documented_field(self):
        fake = FakeEngine()
        self.addCleanup(fake.close)
        h = _test_host(
            engine_url=fake.url,
            share_keys="alice:key-a",
            max_concurrent=4,
            owner_reserve=1,
            peer_quota="5/1h",
            requests_per_minute=7,
            max_request_time=30.0,
            max_body=1024,
            admin_key="admin-secret",
            discovery_url="http://127.0.0.1:%d" % _free_port(),
        )
        h.announce()
        served, _req = _call(h, "POST", "/v1/chat/completions", "key-a", _CHAT_BODY)
        self.assertEqual(served.status, 200, "the request that fills the row was refused: %s" % served.body())

        rec, _req = _call(h, "GET", "/bothy/usage", "key-a")
        self.assertEqual(rec.status, 200, "the usage route answered %d: %s" % (rec.status, rec.body()))
        usage = rec.json()

        # The limits block, which is what makes a peer's stop explainable.
        for field, want in (
            ("host", h.name),
            ("address", "host:7777"),
            ("in_flight", 0),
            ("free", 3),
            ("max_concurrent", 4),
            ("owner_reserve", 1),
            ("peer_slots", 3),
            ("peer_max_concurrent", 0),
            ("requests_per_minute", 7),
            ("peer_quota", "5/1h"),
            ("max_request_time", "30s"),
            ("max_body", 1024),
            ("routes", "inference"),
            ("paused", False),
        ):
            self.assertEqual(usage[field], want, "usage %s = %r, want %r" % (field, usage.get(field), want))

        # One flat object per peer, with the field names the protocol documents.
        self.assertEqual(len(usage["peers"]), 1, "want one row: %r" % (usage["peers"],))
        row = usage["peers"][0]
        self.assertEqual(
            sorted(row),
            sorted(
                [
                    "peer",
                    "in_flight",
                    "requests",
                    "limited",
                    "prompt_tokens",
                    "completion_tokens",
                    "response_bytes",
                    "unmetered_responses",
                    "last_seen",
                    "quota_used",
                    "quota_reset",
                ]
            ),
            "the row is missing or inventing fields: %r" % (row,),
        )
        self.assertEqual(row["peer"], "alice", "a share key attributes usage to a person")
        self.assertEqual(row["requests"], 1, "one served request")
        self.assertEqual(row["limited"], 0, "nothing was refused")
        self.assertEqual((row["prompt_tokens"], row["completion_tokens"]), (3, 4), "the engine's counts")
        self.assertGreater(row["response_bytes"], 0, "the response bytes that passed through")
        self.assertEqual(row["unmetered_responses"], 0, "an engine that reports usage is not unmetered")
        self.assertNotEqual(row["last_seen"], "", "a served request has a time")
        self.assertNotIn("1970", row["last_seen"], "the epoch is written where Go writes its zero time")
        self.assertEqual(row["quota_used"], 1, "the peer spent one of its five requests")
        self.assertTrue(row["quota_reset"], "and the row says when the window turns over")

        # Health carries the same limits plus the fields a healthcheck needs.
        rec, _req = _call(h, "GET", "/bothy/healthz", "k")
        health = rec.json()
        for field in ("ok", "engine_kind", "model_count", "discovery", "key_required"):
            self.assertIn(field, health, "healthz is missing %s: %r" % (field, health))
        self.assertEqual(health["model_count"], 1, "healthz counts the models a client could verify")


class TestAnAgentThatReportsNothingIsUnmeteredNotZero(unittest.TestCase):
    # "Small" and "unknown" are different answers, and a host that reported an
    # unmetered reply as zero tokens would make an engine that reports nothing
    # look like an engine that serves nothing.
    def test_a_whole_response_without_usage_is_unmetered(self):
        fake = FakeEngine()
        self.addCleanup(fake.close)
        fake.payload = _NO_USAGE_BODY
        h = _test_host(engine_url=fake.url, share_key="k")

        rec, _req = _call(h, "POST", "/v1/chat/completions", "k", _CHAT_BODY)
        self.assertEqual(rec.status, 200, "the request was refused: %s" % rec.body())
        usage, _req = _call(h, "GET", "/bothy/usage", "k")
        row = usage.json()["peers"][0]
        self.assertEqual(row["unmetered_responses"], 1, "a reply with no usage is unmetered, not zero")
        self.assertEqual((row["prompt_tokens"], row["completion_tokens"]), (0, 0), "nothing was reported")

    def test_a_failed_response_is_unmetered_rather_than_zero(self):
        # Only a 200 is sniffed: a non-200 is not a completion, so it is recorded
        # as unmetered rather than as a request that cost no tokens, even when the
        # engine put usage in the body.
        fake = FakeEngine()
        self.addCleanup(fake.close)
        fake.status = 400
        h = _test_host(engine_url=fake.url, share_key="k")

        rec, _req = _call(h, "POST", "/v1/chat/completions", "k", _CHAT_BODY)
        self.assertEqual(rec.status, 400, "the engine's answer did not come through: %d" % rec.status)
        usage, _req = _call(h, "GET", "/bothy/usage", "k")
        row = usage.json()["peers"][0]
        self.assertEqual(row["unmetered_responses"], 1, "a non-200 is not sniffed, so it is unmetered")
        self.assertEqual(row["requests"], 1, "and it is still a request against the peer")


# ---------------------------------------------------------------------------
# Streaming (the part a recording writer cannot prove)
# ---------------------------------------------------------------------------


class TestAStreamedReplyReachesTheClientAsItIsProduced(_HostServerMixin, unittest.TestCase):
    # Streaming must not be buffered in either direction: a proxy that collects a
    # stream to count its bytes has undone the reason to stream. The second frame
    # is gated at the engine, so a host that buffered the reply would leave the
    # client waiting for a frame that cannot be produced until the test lets it.
    def test_a_streamed_reply_reaches_the_client_as_it_is_produced(self):
        fake = FakeEngine()
        self.addCleanup(fake.close)
        gate = threading.Event()
        self.addCleanup(gate.set)
        fake.stream_usage(gates=[None, gate, None, None])

        h = _test_host(engine_url=fake.url, share_key="k", max_concurrent=4, stream_usage=True)
        server = self.serve_host(h)

        body = json.dumps({"model": "llama3.1:8b", "stream": True, "messages": []}).encode("utf-8")
        sock = socket.create_connection(("127.0.0.1", server.port()), timeout=10)
        self.addCleanup(sock.close)
        sock.sendall(
            b"POST /v1/chat/completions HTTP/1.1\r\n"
            b"Host: host:7777\r\n"
            + httpx.KEY_HEADER.encode("ascii")
            + b": k\r\nContent-Type: application/json\r\nContent-Length: "
            + str(len(body)).encode("ascii")
            + b"\r\nConnection: close\r\n\r\n"
            + body
        )

        first = _read_until(sock, b"one")
        self.assertIn(b"200 OK", first, "the head did not arrive with the first frame")
        self.assertIn(b"chunked", first.lower(), "an undeclared length has to be framed so it can stay open")
        gate.set()
        rest = _read_until(sock, b"[DONE]")

        # The engine was asked for streamed usage on the caller's behalf, and the
        # caller's own fields survived the rewrite.
        _paths, _headers, _hosts = fake.seen()
        asked = json.loads(fake.bodies[0].decode("utf-8"))
        self.assertEqual(asked["stream_options"], {"include_usage": True}, "the host did not ask for usage")
        self.assertIs(asked["stream"], True, "the caller's stream field was lost")
        self.assertEqual(asked["model"], "llama3.1:8b", "the caller's body was mangled: %r" % asked)

        # And the reply is metered once the stream has finished. The row exists
        # from the moment the slot is taken -- in flight, with nothing counted yet
        # -- so what is waited for is the request being over, not merely a row.
        def metered():
            rec, _req = _call(h, "GET", "/bothy/usage", "k")
            rows = rec.json()["peers"]
            return bool(rows) and rows[0]["requests"] > 0

        self.assertTrue(_wait_until(metered), "the streamed reply was never metered")
        usage, _req = _call(h, "GET", "/bothy/usage", "k")
        row = usage.json()["peers"][0]
        self.assertEqual(row["in_flight"], 0, "the slot was not released once the stream was over")
        self.assertEqual(
            (row["prompt_tokens"], row["completion_tokens"]), (5, 6), "the final usage frame was not read"
        )
        self.assertEqual(row["unmetered_responses"], 0, "a reply whose usage arrived is metered, not unmetered")
        self.assertIn(b"two", rest, "the rest of the stream did not arrive")


class TestARequestCutOffByTheTimeLimitEndsTruncated(_HostServerMixin, unittest.TestCase):
    # A reply already streaming is cut off when the limit is reached: the headers
    # are long gone, so the caller sees a truncated response rather than a clean
    # error.
    def test_a_request_cut_off_by_the_time_limit_ends_truncated(self):
        fake = FakeEngine()
        self.addCleanup(fake.close)
        gate = threading.Event()
        self.addCleanup(gate.set)
        fake.stream_usage(gates=[None, gate, None, None])

        h = _test_host(engine_url=fake.url, share_key="k", max_concurrent=4, max_request_time=0.4)
        server = self.serve_host(h)

        body = json.dumps({"model": "m", "stream": True}).encode("utf-8")
        sock = socket.create_connection(("127.0.0.1", server.port()), timeout=10)
        self.addCleanup(sock.close)
        sock.sendall(
            b"POST /v1/chat/completions HTTP/1.1\r\nHost: host:7777\r\n"
            + httpx.KEY_HEADER.encode("ascii")
            + b": k\r\nContent-Length: "
            + str(len(body)).encode("ascii")
            + b"\r\n\r\n"
            + body
        )
        self.assertIn(b"one", _read_until(sock, b"one"), "the first frame never arrived")

        # The second frame is never produced, so the host's deadline is what ends
        # the response -- and the stream ends without its terminating frames.
        data = bytearray()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            sock.settimeout(max(deadline - time.monotonic(), 0.1))
            try:
                chunk = sock.recv(4096)
            except (socket.timeout, ConnectionResetError):
                break
            if not chunk:
                break
            data += chunk
        self.assertNotIn(b"[DONE]", bytes(data), "a cut-off stream must not look finished")
        self.assertNotIn(b"0\r\n\r\n", bytes(data), "the terminating chunk says the body was complete")

        self.assertTrue(_wait_until(lambda: h._meter.in_flight() == 0), "the slot was not released")
        usage, _req = _call(h, "GET", "/bothy/usage", "k")
        row = usage.json()["peers"][0]
        self.assertEqual(row["requests"], 1, "a cut-off request still costs its peer a request")
        self.assertEqual(row["unmetered_responses"], 1, "no usage frame arrived, so it is unmetered")


# ---------------------------------------------------------------------------
# The control endpoint (owner_test.go)
# ---------------------------------------------------------------------------


class TestSharingCanOnlyBeControlledWithTheAdminKey(unittest.TestCase):
    # The security property: a share key is handed to peers, so a peer who can
    # stop the host is worse than no control surface at all.
    def test_no_admin_key_means_no_control_surface_at_all(self):
        h = _test_host(share_key="peer-key")
        rec, _req = _call(h, "POST", "/bothy/sharing", "peer-key", '{"paused": true}')
        self.assertEqual(rec.status, 404, "without a key there is no endpoint here")
        self.assertIn("admin key", rec.error_message(), "the refusal does not say what is missing")

    def test_a_peer_cannot_pause_the_host(self):
        h = _test_host(share_keys="alice:key-a", admin_key="admin-secret")
        rec, _req = _call(h, "POST", "/bothy/sharing", "key-a", '{"paused": true}')
        self.assertEqual(rec.status, 401, "a peer's share key got %d, want 401" % rec.status)
        health, _req = _call(h, "GET", "/bothy/healthz", "key-a")
        self.assertIs(health.json()["paused"], False, "a peer paused the host")

    def test_no_key_is_refused(self):
        h = _test_host(share_keys="alice:key-a", admin_key="admin-secret")
        rec, _req = _call(h, "POST", "/bothy/sharing", "", '{"paused": true}')
        self.assertEqual(rec.status, 401, "status %d, want 401" % rec.status)

    def test_reading_the_control_endpoint_controls_nothing(self):
        h = _test_host(share_keys="alice:key-a", admin_key="admin-secret")
        rec, _req = _call(h, "GET", "/bothy/sharing", "admin-secret")
        self.assertNotEqual(rec.status, 200, "GET returned 200: %s" % rec.body())
        health, _req = _call(h, "GET", "/bothy/healthz")
        self.assertIs(health.json()["paused"], False, "a GET changed the sharing state")

    def test_an_unreadable_body_is_refused_not_guessed_at(self):
        h = _test_host(share_keys="alice:key-a", admin_key="admin-secret")
        for body in ("", "not json", "{}", '{"paused": "yes"}', '{"paused": 1}', "[]"):
            with self.subTest(body):
                rec, _req = _call(h, "POST", "/bothy/sharing", "admin-secret", body)
                self.assertEqual(rec.status, 400, "body %r got %d, want 400" % (body, rec.status))

    def test_the_admin_key_pauses_and_resumes(self):
        fake = FakeEngine()
        self.addCleanup(fake.close)
        h = _test_host(engine_url=fake.url, share_key="k", admin_key="admin-secret")

        rec, _req = _call(h, "POST", "/bothy/sharing", "admin-secret", '{"paused": true}')
        self.assertEqual(rec.status, 200, "pause got %d: %s" % (rec.status, rec.body()))
        state = rec.json()
        self.assertIs(state["paused"], True, "the pause was not reported")
        self.assertTrue(state.get("since"), "a pause without a since time leaves the owner guessing")

        served, _req = _call(h, "POST", "/v1/chat/completions", "k", _CHAT_BODY)
        self.assertEqual(served.status, 503, "a request while paused got %d, want 503" % served.status)

        resumed, _req = _call(h, "POST", "/bothy/sharing", "admin-secret", '{"paused": false}')
        self.assertEqual(resumed.status, 200, "resume got %d: %s" % (resumed.status, resumed.body()))
        self.assertIs(resumed.json()["paused"], False, "the resume was not reported")
        self.assertNotIn("since", resumed.json(), "a running host has no pause to date")
        served, _req = _call(h, "POST", "/v1/chat/completions", "k", _CHAT_BODY)
        self.assertEqual(served.status, 200, "the engine was not reachable after resuming: %d" % served.status)


class TestPausingTwiceIsTheSameAsPausingOnce(unittest.TestCase):
    # The control endpoint is a button, and buttons get pressed twice. Pausing
    # again changes nothing, so it must not re-announce: the wake channel is the
    # only place that extra announce would come from.
    def test_pausing_twice_is_the_same_as_pausing_once(self):
        h = _test_host(share_key="k", max_concurrent=4, admin_key="admin-secret")

        def wakes():
            count = 0
            while h._wake.is_set():
                count += 1
                h._wake.clear()
            return count

        _call(h, "POST", "/bothy/sharing", "admin-secret", '{"paused": true}')
        self.assertEqual(wakes(), 1, "pausing has to be visible to clients")
        _call(h, "POST", "/bothy/sharing", "admin-secret", '{"paused": true}')
        self.assertEqual(wakes(), 0, "nothing changed, so there is nothing to tell anyone")
        _call(h, "POST", "/bothy/sharing", "admin-secret", '{"paused": false}')
        self.assertEqual(wakes(), 1, "resuming is a change")
        _call(h, "POST", "/bothy/sharing", "admin-secret", '{"paused": false}')
        self.assertEqual(wakes(), 0, "resuming again changes nothing")


class TestPausingStopsTheHostBeingAdvertised(unittest.TestCase):
    # The registry has no delete, so a paused host stops saying it is there and
    # lets the entry expire, which is what sends clients elsewhere instead of to a
    # host that will refuse them.
    def test_pausing_stops_the_host_being_advertised(self):
        recorder = RecordingRegistry()
        self.addCleanup(recorder.close)
        h = _test_host(
            discovery_url=recorder.url,
            share_key="k",
            max_concurrent=4,
            admin_key="admin-secret",
        )

        h.announce()
        self.assertEqual(recorder.count(), 1, "one announce, one registration")

        _call(h, "POST", "/bothy/sharing", "admin-secret", '{"paused": true}')
        h.announce()
        self.assertEqual(recorder.count(), 1, "a paused host must stop advertising")

        _call(h, "POST", "/bothy/sharing", "admin-secret", '{"paused": false}')
        h.announce()
        self.assertEqual(recorder.count(), 2, "resuming advertises again")


# ---------------------------------------------------------------------------
# The announce loop and what it advertises (serve_test.go)
# ---------------------------------------------------------------------------


class TestTheAnnouncedFreeSlotsExcludeTheReserve(unittest.TestCase):
    # "Four slots with one kept for the owner: the reserve is what makes a host
    # look full to peers before it is actually full."
    def test_the_announced_free_slots_exclude_the_reserve(self):
        recorder = RecordingRegistry()
        self.addCleanup(recorder.close)
        h = _test_host(
            discovery_url=recorder.url,
            share_key="k",
            max_concurrent=4,
            owner_reserve=1,
            public_address="public.example:7777",
        )

        h.announce()
        entries = recorder.entries()
        self.assertEqual(len(entries), 1, "the models the engine serves: %r" % (entries,))
        self.assertEqual(entries[0]["free"], 3, "free peer slots, reserve excluded: %r" % (entries[0],))
        self.assertEqual(entries[0]["model"], "llama3.1:8b", "the model on offer")
        self.assertEqual(entries[0]["digest"], "sha256:1111", "the digest a client verifies")
        self.assertEqual(entries[0]["address"], "public.example:7777", "the address a peer can dial")

    def test_an_uncapped_host_advertises_no_free_count_at_all(self):
        # A host running without a cap has no number to report, so it reports
        # nothing: "uncapped" is not "busy", and sending zero is exactly how an
        # uncapped host ended up sorted behind a full one.
        recorder = RecordingRegistry()
        self.addCleanup(recorder.close)
        h = _test_host(discovery_url=recorder.url, share_key="k", max_concurrent=0)

        h.announce()
        entries = recorder.entries()
        self.assertEqual(len(entries), 1, "the model is still announced: %r" % (entries,))
        self.assertNotIn("free", entries[0], "an uncapped host reported a number it does not have: %r" % (entries[0],))
        self.assertIsNone(registry.Entry.from_json(entries[0]).free, "an absent number is not a claim of being full")


class TestServeAnnouncesOnTheHeartbeatAndStopsOnCancel(unittest.TestCase):
    # Registration is the heartbeat: a host that stops sending it is a host whose
    # entry expires, which is how clients stop dialing a machine that went to
    # sleep. Serve has to run that loop itself, and stop when told to.
    def test_serve_announces_on_the_heartbeat_and_stops_on_cancel(self):
        recorder = RecordingRegistry()
        self.addCleanup(recorder.close)
        port = _free_port()
        h = _test_host(
            listen="127.0.0.1:%d" % port,
            discovery_url=recorder.url,
            share_key="k",
            public_address="public.example:7777",
            max_concurrent=4,
            heartbeat=0.02,
        )

        ctx = threading.Event()
        done: List[Any] = []
        thread = threading.Thread(target=lambda: done.append(h.serve(ctx)))
        thread.start()
        self.addCleanup(ctx.set)

        # Two announcements, not one: one would also be produced by a single
        # registration at startup, which is not a heartbeat.
        self.assertTrue(_wait_until(lambda: recorder.count() >= 2, timeout=5), "the heartbeat never fired twice")
        entries = recorder.entries()
        self.assertEqual(entries[0]["address"], "public.example:7777", "the announced address")
        self.assertEqual(entries[0]["free"], 4, "the free slots under the cap: %r" % (entries[0],))

        # The proxy is actually listening, so this is about a served host and not
        # just a loop in the background.
        status, _body = _get(port, "/bothy/healthz")
        self.assertEqual(status, 200, "the host never answered /bothy/healthz")

        ctx.set()
        thread.join(timeout=8)
        self.assertFalse(thread.is_alive(), "Serve did not return after its context was cancelled")
        self.assertEqual(done, [None], "Serve raises on failure and returns None otherwise")


class TestStopEndsAServingHost(unittest.TestCase):
    # Go's Serve returns when its context is cancelled. The same has to be
    # askable without one, which is what a supervising process or a test reaches
    # for -- and it has to work, or a host started with no context could only be
    # ended by killing the process.
    def test_stop_ends_a_serving_host(self):
        port = _free_port()
        h = _test_host(listen="127.0.0.1:%d" % port, share_key="k")
        done: List[Any] = []
        thread = threading.Thread(target=lambda: done.append(h.serve()))
        thread.start()

        def answered():
            try:
                return _get(port, "/bothy/healthz")[0] == 200
            except OSError:
                return False

        self.assertTrue(_wait_until(answered, timeout=10), "the host never answered, so this proves nothing")
        h.stop()
        thread.join(timeout=8)
        self.assertFalse(thread.is_alive(), "stop did not end the serve loop")
        self.assertEqual(done, [None], "serve raises on failure and returns None otherwise")


class TestAnnounceSurvivesAnEngineThatCannotBeListed(unittest.TestCase):
    # A host starts before its engine is ready -- compose starts services in
    # whatever order it likes -- so an unlistable engine must be a warning, not a
    # crash and not a registration of nothing.
    def test_announce_survives_an_engine_that_cannot_be_listed(self):
        recorder = RecordingRegistry()
        self.addCleanup(recorder.close)
        log, capture = _captured("unlistable")
        h = host.new(
            _test_config(
                engine_kind="ollama",
                engine_url="http://127.0.0.1:%d" % _free_port(),
                discovery_url=recorder.url,
                share_key="k",
            ),
            log,
        )

        h.announce()

        self.assertEqual(recorder.count(), 0, "there was nothing to announce")
        self.assertIsNone(h.current_models(), "an engine that could not be read offers nothing")
        self.assertIn("cannot list engine models", capture.text(), "nothing warned that the engine was unreadable")


class TestAnnounceWarnsAboutAModelWithNoDigestButStillRegistersIt(unittest.TestCase):
    # A model with no digest is the llama.cpp/vLLM case. PROTOCOL.md says clients
    # cannot verify it, so the host has to say so and register it anyway rather
    # than dropping the model silently or inventing a digest.
    def test_announce_warns_about_a_model_with_no_digest_but_still_registers_it(self):
        recorder = RecordingRegistry()
        self.addCleanup(recorder.close)
        log, capture = _captured("no-digest")
        h = host.new(
            _test_config(
                discovery_url=recorder.url,
                share_key="k",
                engine=engine.Options(static=[model.Model(name="llama3.1:8b")]),
            ),
            log,
        )

        h.announce()

        entries = recorder.entries()
        self.assertEqual(len(entries), 1, "the model is registered anyway: %r" % (entries,))
        self.assertEqual(entries[0]["digest"], "", "no digest was invented")
        self.assertIn("no digest", capture.text(), "nothing warned that clients cannot verify this model")


# ---------------------------------------------------------------------------
# Speaking up about the limits (serve_test.go)
# ---------------------------------------------------------------------------


class TestDescribeLimitsSaysWhatIsNotConfigured(unittest.TestCase):
    def test_describe_limits_says_what_is_not_configured(self):
        log, capture = _captured("describe")
        h = _test_host(share_key="", max_concurrent=0, owner_reserve=1)
        h.log = log

        h.describe_limits()

        for want in (
            "no share key set",
            "no concurrency cap",
            "owner-reserve has no effect",
            "no per-peer budget",
            "no admin key",
        ):
            self.assertIn(want, capture.text(), "describeLimits never said %r" % want)


class TestDescribeLimitsNamesWhatIsConfigured(unittest.TestCase):
    def test_describe_limits_names_what_is_configured(self):
        log, capture = _captured("describe-configured")
        h = _test_host(share_keys="alice:key-a,bob:key-b", max_concurrent=4, owner_reserve=1, peer_quota="200/1h", admin_key="admin")
        h.log = log

        h.describe_limits()

        for want in ("per-peer share keys required", "slots kept for you", "per-peer budget"):
            self.assertIn(want, capture.text(), "describeLimits never said %r" % want)
        self.assertNotIn("no per-peer budget", capture.text(), "a configured budget was reported as absent")


class TestDescribeLimitsWarnsWhenStartingPaused(unittest.TestCase):
    # Starting paused is the one state where a host serves nobody while looking
    # perfectly healthy, so it says so rather than being discovered from a peer's
    # 503 later.
    def test_describe_limits_warns_when_starting_paused(self):
        log, capture = _captured("describe-paused")
        h = _test_host(paused=True)
        h.log = log
        h.describe_limits()
        self.assertIn("starting paused", capture.text(), "a host that starts paused never said so")

        log, capture = _captured("describe-running")
        host.new(_test_config(), log).describe_limits()
        self.assertNotIn("starting paused", capture.text(), "a host sharing normally claimed to be paused")


# ---------------------------------------------------------------------------
# Addresses, quotas and startup refusals
# ---------------------------------------------------------------------------


class TestDefaultAddressIsDialable(unittest.TestCase):
    # The advertised address is what peers dial, so a host listening on every
    # interface must not advertise ":7777" -- that is not something anyone can
    # reach.
    def test_default_address_is_dialable(self):
        for listen in (":7777", "127.0.0.1:9999", "0.0.0.0:8080"):
            with self.subTest(listen):
                got = host.default_address(listen)
                name, sep, port = got.rpartition(":")
                self.assertTrue(sep, "default_address(%r) = %r, which is not host:port" % (listen, got))
                self.assertNotEqual(name, "", "default_address(%r) = %r, want a host a peer can resolve" % (listen, got))
                self.assertEqual(port, listen[listen.rfind(":") + 1 :], "default_address(%r) = %r, wrong port" % (listen, got))


class TestQuotaSpecIsCheckedAtStartup(unittest.TestCase):
    def test_quota_spec_is_checked_at_startup(self):
        for spec, want_error in (
            ("", ""),
            ("200/1h", ""),
            ("1/1m", ""),
            ("200", "count/period"),
            ("200/", "not a duration"),
            ("/1h", "not a positive number"),
            ("0/1h", "not a positive number"),
            ("-5/1h", "not a positive number"),
            ("many/1h", "not a positive number"),
            ("200/soon", "not a duration"),
            ("200/0s", "not a duration"),
        ):
            with self.subTest(spec):
                try:
                    h = _test_host(share_key="k", max_concurrent=4, peer_quota=spec)
                except ConfigError as err:
                    if want_error == "":
                        self.fail("quota %r refused: %s" % (spec, err))
                    self.assertIn(want_error, str(err), "the error does not mention %r" % want_error)
                    continue
                self.assertEqual(want_error, "", "quota %r accepted, want a refusal naming %r" % (spec, want_error))
                self.assertEqual(h._meter.quota().enabled(), spec != "", "the quota is not what was asked for")


class TestAReserveThatLeavesNoRoomForPeersIsRefused(unittest.TestCase):
    # A host serving nobody while looking healthy is a misconfiguration, and
    # pausing is the way to say "not right now" -- out loud, and reversibly.
    def test_a_reserve_that_leaves_no_room_for_peers_is_refused(self):
        for name, overrides, want_error in (
            ("the reserve swallows the cap", dict(max_concurrent=2, owner_reserve=2), "no slots for peers"),
            ("the reserve is bigger than the cap", dict(max_concurrent=2, owner_reserve=5), "no slots for peers"),
            ("the reserve is negative", dict(max_concurrent=2, owner_reserve=-1), "negative"),
            # A heartbeat of zero would panic the announce loop: the host must
            # refuse to start instead of dying on its first announce.
            ("the heartbeat would panic the announce loop", dict(max_concurrent=2, heartbeat=-1.0), "heartbeat"),
        ):
            with self.subTest(name):
                with self.assertRaises(ConfigError) as caught:
                    _test_host(share_key="k", **overrides)
                self.assertIn(want_error, str(caught.exception), "the error does not mention %r" % want_error)


# ---------------------------------------------------------------------------
# The command line (serve_test.go)
# ---------------------------------------------------------------------------


class TestRunRefusesConfigurationsItCannotHonour(unittest.TestCase):
    # Run is the command line surface: every one of these is a typo that must be
    # an error at startup rather than a host that runs with a limit nobody
    # configured.
    def test_run_refuses_configurations_it_cannot_honour(self):
        for name, args in (
            ("an unknown engine kind", ["-engine-kind", "nonsense"]),
            ("an engine URL that is not a URL", ["-engine-url", "://bad"]),
            ("a model list with no name", ["-models", "=sha256:abc"]),
            ("a quota without a period", ["-peer-quota", "200"]),
            ("a reserve that swallows the cap", ["-owner-reserve", "9", "-max-concurrent", "2"]),
            ("a negative reserve", ["-owner-reserve", "-1"]),
            ("share keys that are not name:key", ["-share-keys", "alice"]),
            ("a heartbeat of zero", ["-heartbeat", "0s"]),
            ("a negative heartbeat", ["-heartbeat", "-1s"]),
        ):
            with self.subTest(name):
                with self.assertRaises(BothyError):
                    host.run(None, _log("run-refusal"), ["-listen", "127.0.0.1:0"] + args)


class TestRunServesUntilCancelled(unittest.TestCase):
    # The happy path of the command, so the flag wiring is covered rather than
    # just its refusals.
    def test_run_serves_until_cancelled(self):
        port = _free_port()
        ctx = threading.Event()
        done: List[Any] = []
        thread = threading.Thread(
            target=lambda: done.append(
                host.run(
                    ctx,
                    _log("run"),
                    [
                        "-listen",
                        "127.0.0.1:%d" % port,
                        "-engine-kind",
                        "static",
                        "-models",
                        "llama3.1:8b=sha256:1111",
                        "-share-key",
                        "k",
                    ],
                )
            )
        )
        thread.start()
        self.addCleanup(ctx.set)

        def answered():
            try:
                return _get(port, "/bothy/healthz")[0] == 200
            except OSError:
                return False

        self.assertTrue(_wait_until(answered, timeout=10), "the host never answered, so this proves nothing")
        ctx.set()
        thread.join(timeout=8)
        self.assertFalse(thread.is_alive(), "Run did not return after its context was cancelled")
        self.assertEqual(done, [None], "Run returned something other than None")


class TestRunAcceptsGoFlagSpellings(unittest.TestCase):
    # Go's flag package takes the next argument as a flag's value even when it
    # looks like another flag, and it spells booleans `-flag=false`. Both are how
    # the flags are documented, so both have to parse.
    def test_run_accepts_go_flag_spellings(self):
        parser = host._share_parser()
        values = host._value_flags(parser)
        parsed = parser.parse_args(
            host._join_flag_values(
                ["-heartbeat", "-1s", "-stream-usage=false", "-max-request-time", "90s", "-paused"],
                values,
            )
        )
        self.assertEqual(parsed.heartbeat, -1.0, "a negative duration was not read as a value")
        self.assertIs(parsed.stream_usage, False, "-stream-usage=false did not mean false")
        self.assertEqual(parsed.max_request_time, 90.0, "a duration flag was not parsed")
        self.assertIs(parsed.paused, True, "a bare boolean flag did not mean true")

        default = parser.parse_args(host._join_flag_values(["-stream-usage"], values))
        self.assertIs(default.stream_usage, True, "the bare spelling did not mean true")


# ---------------------------------------------------------------------------
# The keys, the peers and the body rewrite (peers_test.go, inject_test.go)
# ---------------------------------------------------------------------------


class TestParsePeers(unittest.TestCase):
    def test_parse_peers(self):
        peers = host.parse_peers("alice:key-one,bob:key-two")
        self.assertEqual(peers.by_key.get("key-one"), "alice", "a key is attributed to its peer")
        self.assertEqual(len(peers.by_key), 2, "parsed %d keys, want 2" % len(peers.by_key))
        self.assertFalse(peers.open(), "a configured host is not open")


class TestParsePeersAllowsColonsInKeys(unittest.TestCase):
    # Keys may contain colons, so the name is only everything before the first one.
    def test_parse_peers_allows_colons_in_keys(self):
        peers = host.parse_peers("alice:sk-abc:def")
        self.assertEqual(peers.by_key.get("sk-abc:def"), "alice", "the name is everything before the first colon")


class TestParsePeersRejectsBadSpecs(unittest.TestCase):
    def test_parse_peers_rejects_bad_specs(self):
        for spec in ("alice", "alice:", ":key", "alice:a,alice:b,alice:b"):
            with self.subTest(spec):
                with self.assertRaises(ConfigError):
                    host.parse_peers(spec)


class TestResolvePeersFallsBackToTheSingleKey(unittest.TestCase):
    def test_resolve_peers_falls_back_to_the_single_key(self):
        peers = host.resolve_peers("", "just-one")
        self.assertFalse(peers.open(), "a single key should still require that key")
        self.assertEqual(peers.by_key.get("just-one"), "default", "a single key is attributed to the default peer")

        named = host.resolve_peers("alice:key-one", "just-one")
        self.assertEqual(named.by_key.get("key-one"), "alice", "named keys win over the single key")
        self.assertIsNone(named.by_key.get("just-one"), "the single key was still accepted")


class TestPeersAcceptsHeaderAndBearer(unittest.TestCase):
    def test_peers_accepts_header_and_bearer(self):
        peers = host.parse_peers("alice:key-one")
        name, ok = peers.resolve(_request("GET", "/v1/models", key="key-one"))
        self.assertTrue(ok and name == "alice", "the named header was not honoured")

        req = _request("GET", "/v1/models", headers=[("Authorization", "Bearer key-one")])
        name, ok = peers.resolve(req)
        self.assertTrue(ok and name == "alice", "a bearer token was not honoured")


class TestPeersRejectsUnknownKeys(unittest.TestCase):
    def test_peers_rejects_unknown_keys(self):
        peers = host.parse_peers("alice:key-one,bob:key-two")
        for key in ("", "wrong", "key-on", "key-oneX", "keytwo"):
            with self.subTest(key):
                _name, ok = peers.resolve(_request("GET", "/v1/models", key=key))
                self.assertFalse(ok, "resolve(%r) was accepted, want refusal" % key)


class TestPeersToleratesHeaderWhitespace(unittest.TestCase):
    # Header values legitimately carry optional whitespace, so trimming it is
    # correct -- and it cannot help an attacker, because whitespace can only make
    # a correct key match, never a wrong one.
    def test_peers_tolerates_header_whitespace(self):
        peers = host.parse_peers("alice:key-one")
        name, ok = peers.resolve(_request("GET", "/v1/models", key="  key-one  "))
        self.assertTrue(ok and name == "alice", "a padded key was refused")


class TestOpenHostMetersByAddress(unittest.TestCase):
    # An open host still needs a peer identity, or its limits would apply to
    # everybody at once and its usage report would be one anonymous row.
    def test_open_host_meters_by_address(self):
        peers = host.resolve_peers("", "")
        self.assertTrue(peers.open(), "a host with no keys is open")
        name, ok = peers.resolve(_request("GET", "/v1/models", remote="203.0.113.7:54321"))
        self.assertTrue(ok, "an open host must still admit the request")
        self.assertEqual(name, "addr:203.0.113.7", "an open host meters by the caller's address")


class TestThePeerIsCarriedOnTheRequest(unittest.TestCase):
    def test_the_peer_is_carried_on_the_request(self):
        req = _request("GET", "/")
        self.assertEqual(host.peer_from(req), "", "a request that was never attributed has no peer")
        self.assertIs(host.with_peer(req, "alice"), req, "with_peer returns the request it named")
        self.assertEqual(host.peer_from(req), "alice", "the peer did not survive")


class TestAddStreamUsage(unittest.TestCase):
    # The decision table is the whole risk here: adding a field to a body Bothy
    # did not write is only safe because it happens in a narrow, enumerable set of
    # cases. So the cases are written down.
    def test_add_stream_usage(self):
        for name, body, want_changed, want_has in (
            (
                "a streamed chat completion gets the ask",
                '{"model":"llama3.1:8b","stream":true,"messages":[]}',
                True,
                '"stream_options":{"include_usage":true}',
            ),
            ("a streamed legacy completion gets it too", '{"model":"llama3.1:8b","stream":true,"prompt":"hi"}', True, '"include_usage":true'),
            ("a caller that already asked is left exactly as it was", '{"model":"m","stream":true,"stream_options":{"include_usage":true}}', False, ""),
            ("a caller that asked not to is not overridden", '{"model":"m","stream":true,"stream_options":{"include_usage":false}}', False, ""),
            ("an unrelated stream_options key is still an opinion", '{"model":"m","stream":true,"stream_options":{"unknown":"x"}}', False, ""),
            ("a whole-response request is not a stream", '{"model":"m","stream":false,"messages":[]}', False, ""),
            ("a request with no stream field at all", '{"model":"m","messages":[]}', False, ""),
            ("a stream field that is not a bool", '{"model":"m","stream":"yes"}', False, ""),
            ("a null stream field", '{"model":"m","stream":null}', False, ""),
            ("not JSON, so not ours to touch", "model=llama3.1&stream=true", False, ""),
            ("JSON, but not an object", '["stream",true]', False, ""),
            ("empty body", "", False, ""),
        ):
            with self.subTest(name):
                got, changed = host.add_stream_usage(body.encode("utf-8"))
                self.assertEqual(changed, want_changed, "changed = %s" % changed)
                if not changed:
                    self.assertEqual(got.decode("utf-8"), body, "an untouched body was altered")
                    continue
                self.assertIn(want_has, got.decode("utf-8"), "the ask is not in the result")
                # What was there before must still be there: this adds a field, and
                # a rewrite that dropped the prompt would be worse than no meter.
                for keep in ('"model":"m"', '"stream":true'):
                    if keep in body:
                        self.assertIn(keep, got.decode("utf-8"), "the rewrite lost %s" % keep)


class TestWantsStreamUsage(unittest.TestCase):
    def test_wants_stream_usage(self):
        for method, path, want in (
            ("POST", "/v1/chat/completions", True),
            ("POST", "/v1/completions", True),
            ("GET", "/v1/chat/completions", False),
            ("POST", "/api/chat", False),  # Ollama's own shape
            ("POST", "/v1/embeddings", False),  # not a streamed route
            ("POST", "/bothy/models", False),  # ours, and not OpenAI's
            ("POST", "/v1/unknown/thing", False),
        ):
            with self.subTest("%s %s" % (method, path)):
                self.assertEqual(
                    host.wants_stream_usage(_request(method, path)),
                    want,
                    "%s %s" % (method, path),
                )


class TestInjectStreamUsageLeavesOtherRequestsAlone(unittest.TestCase):
    # A request that is not going to be rewritten must be forwarded byte for byte.
    def test_inject_stream_usage_leaves_other_requests_alone(self):
        body = '{"model":"m","stream":true,"messages":[]}'
        req = _request("POST", "/api/chat", body=body)
        self.assertFalse(host.inject_stream_usage(req), "a non-OpenAI route was rewritten")
        self.assertEqual(b"".join(req.body_chunks()), body.encode("utf-8"), "the body was altered")


class TestInjectStreamUsageForwardsAnOversizedBody(unittest.TestCase):
    # A body past the buffer limit is forwarded untouched rather than truncated,
    # because a half-read body would be a worse bug than an unmetered reply.
    def test_inject_stream_usage_forwards_an_oversized_body(self):
        big = '{"model":"m","stream":true,"padding":"%s"}' % ("x" * host.MAX_INJECTABLE)
        req = _request("POST", "/v1/chat/completions", body=big)
        self.assertFalse(host.inject_stream_usage(req), "an oversized body was rewritten")
        got = b"".join(req.body_chunks())
        self.assertEqual(len(got), len(big), "read back %d bytes, want %d -- the body was truncated" % (len(got), len(big)))
        self.assertTrue(got.startswith(b'{"model":"m"') and got.endswith(b'"}'), "the oversized body came back mangled")


class TestInjectStreamUsageRewritesAStreamedRequest(unittest.TestCase):
    # And the case it exists for: the body is rewritten, the caller's fields
    # survive, and the length the engine is told matches what it will receive.
    def test_inject_stream_usage_rewrites_a_streamed_request(self):
        req = _request("POST", "/v1/chat/completions", body='{"model":"llama3.1:8b","stream":true}')
        self.assertTrue(host.inject_stream_usage(req), "a streamed chat completion was not rewritten")
        got = b"".join(req.body_chunks())
        self.assertIn(b'"include_usage":true', got, "stream_options was not added")
        self.assertEqual(req.content_length, len(got), "ContentLength does not match the body")
        # The engine has to be told the new length, or it reads a truncated body
        # and answers with a parse error that mentions nothing about metering.
        self.assertEqual(req.headers.get("Content-Length"), str(req.content_length), "the length header was not updated")

    def test_a_chunked_body_is_left_alone(self):
        # Go's net/http de-chunks a request body before a handler sees it; here it
        # is still framed on the socket, so a streamed request that arrives chunked
        # is forwarded untouched and lands in the meter as unmetered.
        req = _request("POST", "/v1/chat/completions", body='{"model":"m","stream":true}', chunked=True)
        self.assertFalse(host.inject_stream_usage(req), "a chunked body was rewritten")
        self.assertEqual(b"".join(req.body_chunks()), b'{"model":"m","stream":true}', "the chunked body was altered")


if __name__ == "__main__":
    unittest.main()
