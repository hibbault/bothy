"""A fake engine, tested as if it were a real one.

Nearly every test here goes through a socket rather than calling a handler
directly, because the mock's job is to be indistinguishable from an engine at the
other end of a connection: a buffered stream, a wrong content type or a refusal
that is not the documented shape are all failures that only show up over the wire.

Two of these tests are about the mock deliberately behaving like the *worse* kind
of real engine -- reporting usage on a stream only when asked, and listing ids
with no digests. They are here to pin a behaviour a host has to work around; if
they ever pass for the wrong reason, the stand-in has stopped standing in for the
case that matters.
"""

from __future__ import annotations

import io
import http.client
import json
import logging
import socket
import threading
import time
import unittest
from datetime import datetime
from typing import Any, Dict, NamedTuple, Optional

from bothy import httpx, mockengine, model
from bothy.errors import BothyError, ConfigError

DIGEST_A = mockengine._DIGEST_A
LLAMA = "llama3.1:8b"


def _quiet() -> logging.Logger:
    """A logger that discards everything, the counterpart of slog's io.Discard."""
    log = logging.getLogger("bothy.mockengine.test")
    if not log.handlers:
        log.addHandler(logging.NullHandler())
    log.propagate = False
    return log


class _Reply(NamedTuple):
    """One answer from the engine, already read."""

    status: int
    headers: Any
    raw: bytes

    def json(self) -> Any:
        try:
            return json.loads(self.raw)
        except ValueError as err:
            raise AssertionError("%r is not JSON: %s" % (self.raw, err)) from None


def _free_addr() -> str:
    """An address nothing is listening on, the way a closed listener leaves one."""
    probe = socket.socket()
    try:
        probe.bind(("127.0.0.1", 0))
        return "127.0.0.1:%d" % probe.getsockname()[1]
    finally:
        probe.close()


class _EngineCase(unittest.TestCase):
    """One mock engine, serving on a real port.

    The counterpart of Go's httptest.NewServer: same engine, same routes, one
    connection away, so the assertions are about bytes rather than about calls.
    """

    delay = 0.0
    name = "mock-a"

    def build_engine(self) -> mockengine.Server:
        """The engine under test. A subclass substitutes an instrumented one."""
        return mockengine.Server(self.config(), _quiet())

    def config(self) -> mockengine.Config:
        return mockengine.Config(
            name=self.name,
            models=[model.Model(name=LLAMA, digest=DIGEST_A)],
            delay=self.delay,
        )

    def setUp(self) -> None:
        self.engine = self.build_engine()
        self.server = httpx.Server("127.0.0.1:0", self.engine.handler(), _quiet())
        self.server.start()
        self.addCleanup(self.server.shutdown)

    def request(self, method: str, path: str, body: Optional[bytes] = None) -> _Reply:
        conn = http.client.HTTPConnection("127.0.0.1", self.server.port(), timeout=10)
        try:
            headers = {"Content-Type": "application/json"} if body is not None else {}
            conn.request(method, path, body=body, headers=headers)
            resp = conn.getresponse()
            return _Reply(resp.status, resp.headers, resp.read())
        finally:
            conn.close()

    def get(self, path: str) -> _Reply:
        return self.request("GET", path)

    def post(self, path: str, payload: Any) -> _Reply:
        if isinstance(payload, bytes):
            body = payload
        else:
            body = json.dumps(payload).encode("utf-8")
        return self.request("POST", path, body)

    def chat(self, payload: Dict[str, Any]) -> _Reply:
        return self.post("/v1/chat/completions", payload)

    def stream_chat(self, payload: Dict[str, Any]) -> bytes:
        return self.chat(payload).raw


def _streamed_usage(raw: bytes) -> Optional[Dict[str, Any]]:
    """The usage object out of a streamed response, if any frame carried one.

    Only the last such frame matters; earlier ones have none. Frames that are not
    JSON are skipped, because [DONE] is a frame too.
    """
    reported = None
    for frame in raw.split(b"\n\n"):
        line = frame.strip()
        if not line.startswith(b"data:"):
            continue
        data = line[len(b"data:") :].strip()
        if data == b"[DONE]":
            continue
        try:
            parsed = json.loads(data)
        except ValueError:
            continue
        if isinstance(parsed, dict) and isinstance(parsed.get("usage"), dict):
            reported = parsed["usage"]
    return reported


class TestInternalModelsReportsDigests(_EngineCase):
    # This is the route Bothy reads to learn digests; without it the mock cannot
    # stand in for a real engine.
    def test_internal_models_reports_digests(self) -> None:
        reply = self.get("/internal/models")
        self.assertEqual(reply.status, 200)
        payload = reply.json()
        self.assertEqual(len(payload["models"]), 1)
        self.assertEqual(payload["models"][0]["digest"], DIGEST_A)


class TestTheDigestForANamedModelIsStable(_EngineCase):
    # A host's announce loop asserts on this digest without having been told it,
    # so it has to be the same answer every time it is asked.
    def test_the_digest_for_a_named_model_is_stable_across_calls(self) -> None:
        first = self.get("/internal/models").json()["models"]
        second = self.get("/internal/models").json()["models"]
        self.assertEqual(first, second)
        self.assertEqual(first[0], {"name": LLAMA, "digest": DIGEST_A})

        # The reply names the same digest as the model list, which is how a client
        # tells whose GPU answered.
        content = self.chat({"model": LLAMA, "messages": [{"role": "user", "content": "hi"}]}).json()["choices"][0][
            "message"
        ]["content"]
        self.assertIn(DIGEST_A, content)

    def test_the_default_models_are_the_ones_configured_from_the_environment(self) -> None:
        # The devnet sets BOTHY_MOCK_MODELS to exactly this, so a bare `bothy mock`
        # and the documented one-machine setup must agree about the digests.
        defaults = {m.name: m.digest for m in model.parse_list(mockengine._DEFAULT_MODELS)}
        self.assertEqual(defaults[LLAMA], DIGEST_A)
        self.assertEqual(defaults["qwen2.5:7b"], mockengine._DIGEST_B)


class TestChatCompletionNamesTheEngineAndDigest(_EngineCase):
    def test_reply_names_the_engine_the_digest_and_the_prompt(self) -> None:
        reply = self.chat({"model": LLAMA, "messages": [{"role": "user", "content": "hello there"}]})
        self.assertEqual(reply.status, 200)
        out = reply.json()
        self.assertEqual(out["model"], LLAMA)
        self.assertEqual(out["object"], "chat.completion")
        self.assertEqual(len(out["choices"]), 1)
        content = out["choices"][0]["message"]["content"]
        self.assertIn("mock-a", content)
        self.assertIn(DIGEST_A, content)
        self.assertIn("hello there", content)
        self.assertEqual(out["choices"][0]["finish_reason"], "stop")


class TestChatCompletionStreamsServerSentEvents(_EngineCase):
    # Streaming is the path that breaks if the response is buffered, so assert the
    # frames actually arrive.
    def test_streams_server_sent_events(self) -> None:
        reply = self.chat({"model": LLAMA, "stream": True, "messages": [{"role": "user", "content": "one two three"}]})
        self.assertTrue(reply.headers.get("Content-Type").startswith("text/event-stream"), reply.headers.get("Content-Type"))
        text = reply.raw.decode("utf-8")
        self.assertIn("chat.completion.chunk", text)
        self.assertIn("data: [DONE]", text)
        # Three words, plus a finish chunk, plus [DONE].
        self.assertGreaterEqual(text.count("data: "), 4, text)


class TestStreamedReplyReportsUsageWhenAsked(_EngineCase):
    # A streamed reply is only countable if the engine puts usage in the stream, and
    # an OpenAI-compatible engine only does that when the request asks. The mock
    # behaves the same way on purpose: it is the worse of the two real behaviours,
    # and the one the host has to work around.
    def test_usage_arrives_when_it_is_asked_for(self) -> None:
        raw = self.stream_chat(
            {
                "model": LLAMA,
                "stream": True,
                "stream_options": {"include_usage": True},
                "messages": [{"role": "user", "content": "one two three"}],
            }
        )
        reported = _streamed_usage(raw)
        self.assertIsNotNone(reported, "no streamed frame carried usage, so a stream can only be unmetered: %r" % raw)
        self.assertEqual(reported["prompt_tokens"], 3)
        self.assertGreater(reported["completion_tokens"], 0)
        self.assertEqual(reported["total_tokens"], reported["prompt_tokens"] + reported["completion_tokens"])


class TestStreamedReplyReportsNothingWhenNotAsked(_EngineCase):
    # The other half of the same contract: without the ask, a compliant engine
    # reports nothing. If this ever passes with usage present, the mock has stopped
    # being a useful stand-in for the case that matters.
    def test_usage_is_absent_when_it_was_not_asked_for(self) -> None:
        raw = self.stream_chat({"model": LLAMA, "stream": True, "messages": [{"role": "user", "content": "one two three"}]})
        self.assertIsNone(_streamed_usage(raw), "usage was reported without being asked for: %r" % raw)
        self.assertIn("data: [DONE]", raw.decode("utf-8"), "the stream did not terminate properly: %r" % raw)


class TestContentPartsAreFlattened(_EngineCase):
    # Tooling still sends the older completions shape, and content can be a list of
    # typed parts rather than a string.
    def test_parts_are_concatenated_without_inventing_a_separator(self) -> None:
        reply = self.chat(
            {
                "model": LLAMA,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "part one"},
                            {"type": "text", "text": "part two"},
                        ],
                    }
                ],
            }
        )
        # Parts are concatenated exactly, with no separator inserted: a split can
        # land mid-word, so inventing a space would corrupt the prompt.
        self.assertIn("part onepart two", reply.raw.decode("utf-8"))


class TestCompletionsEndpointWorks(_EngineCase):
    def test_prompt_is_echoed(self) -> None:
        reply = self.post("/v1/completions", {"model": LLAMA, "prompt": "say hi"})
        self.assertEqual(reply.status, 200)
        self.assertIn("say hi", reply.raw.decode("utf-8"))
        self.assertEqual(reply.json()["object"], "text_completion")


class TestHealthNamesTheEngine(_EngineCase):
    def test_health_names_the_engine(self) -> None:
        reply = self.get("/healthz")
        self.assertEqual(reply.status, 200)
        body = reply.json()
        self.assertEqual((body["ok"], body["engine"], body["name"]), (True, "mock", "mock-a"))


class TestTagsMirrorOllama(_EngineCase):
    # Ollama's own API, because `bothy share -engine-kind ollama` is meant to work
    # against the mock exactly as it does against a real Ollama.
    def test_tags_carry_the_name_and_digest_ollama_reports(self) -> None:
        reply = self.get("/api/tags")
        self.assertEqual(reply.status, 200)
        models = reply.json()["models"]
        self.assertEqual(len(models), 1)
        first = models[0]
        self.assertEqual((first["name"], first["model"], first["digest"]), (LLAMA, LLAMA, DIGEST_A))
        # RFC3339, which is what a client parses it as. This raises if it is not.
        when = datetime.strptime(first["modified_at"], "%Y-%m-%dT%H:%M:%SZ")
        self.assertGreater(when.year, 2000, first["modified_at"])

    def test_openai_models_lists_ids_only(self) -> None:
        # A real /v1/models reports ids and no digests, and that emptiness is the
        # whole reason the openai lister merges a configured digest by name.
        reply = self.get("/v1/models")
        self.assertEqual(reply.status, 200)
        body = reply.json()
        self.assertEqual(body["object"], "list")
        self.assertEqual(len(body["data"]), 1)
        card = body["data"][0]
        self.assertEqual((card["id"], card["object"], card["owned_by"]), (LLAMA, "model", "bothy-mock"))
        self.assertNotIn("digest", card)


class TestCompletionsStreamsFramesAndUsageWhenAsked(_EngineCase):
    # The older completions endpoint is what plenty of tooling still sends.
    def _stream(self, include_usage: bool) -> bytes:
        payload: Dict[str, Any] = {"model": LLAMA, "stream": True, "prompt": "one two"}
        if include_usage:
            payload["stream_options"] = {"include_usage": True}
        reply = self.post("/v1/completions", payload)
        self.assertTrue(reply.headers.get("Content-Type").startswith("text/event-stream"), reply.headers.get("Content-Type"))
        text = reply.raw.decode("utf-8")
        self.assertIn("text_completion", text)
        self.assertIn("data: [DONE]", text, "the stream did not terminate: %r" % text)
        return reply.raw

    def test_usage_arrives_when_it_is_asked_for(self) -> None:
        reported = _streamed_usage(self._stream(True))
        self.assertIsNotNone(reported)
        self.assertEqual(reported["prompt_tokens"], 2)
        self.assertGreater(reported["completion_tokens"], 0)
        self.assertEqual(reported["total_tokens"], reported["prompt_tokens"] + reported["completion_tokens"])

    def test_usage_is_absent_when_it_was_not_asked_for(self) -> None:
        self.assertIsNone(_streamed_usage(self._stream(False)))


class TestInvalidJSONIsRefusedInTheDocumentedShape(_EngineCase):
    # A refusal has to be OpenAI-shaped, or a client shows a blank failure instead of
    # the reason.
    def test_both_endpoints_refuse_with_a_message(self) -> None:
        for path in ("/v1/chat/completions", "/v1/completions"):
            with self.subTest(path=path):
                reply = self.post(path, b"not json")
                self.assertEqual(reply.status, 400, path)
                message = reply.json()["error"]["message"]
                self.assertNotEqual(message, "", path)


class TestTheReplyNamesTheEngineAndAdmitsAnUnknownModel(_EngineCase):
    # The reply names the engine and the digest that produced it, which is how a
    # client can tell whose GPU answered -- and it must not pretend to know a model
    # it was not configured with.
    def test_an_unknown_model_is_not_given_a_digest_it_does_not_have(self) -> None:
        raw = self.chat({"model": "not-installed:1b", "messages": [{"role": "user", "content": ""}]}).raw
        text = raw.decode("utf-8")
        self.assertIn("unknown-model", text)
        self.assertNotIn(DIGEST_A, text)
        self.assertIn("(empty prompt)", text)


class _RecordingWriter:
    """A response body that records what was written, and can break like a socket.

    `break_after` is the caller going away: every write from then on behaves the
    way writing to a socket somebody has closed does.
    """

    def __init__(self, break_after: Optional[int] = None) -> None:
        self.chunks = []
        self.break_after = break_after

    def write(self, data: bytes) -> None:
        if self.break_after is not None and len(self.chunks) >= self.break_after:
            raise BrokenPipeError("the caller went away")
        self.chunks.append(bytes(data))

    def flush(self) -> None:
        pass

    def joined(self) -> bytes:
        return b"".join(self.chunks)


class _FakeConnection:
    """Just enough of a connection for a handler to answer without a socket."""

    def __init__(self, writer: _RecordingWriter) -> None:
        self.wfile = writer
        self.status = 0
        self.headers = []

    def send_response_only(self, status: int) -> None:
        self.status = status

    def send_header(self, key: str, value: str) -> None:
        self.headers.append((key, value))

    def end_headers(self) -> None:
        pass

    def header(self, name: str) -> str:
        for key, value in self.headers:
            if key.lower() == name.lower():
                return value
        return ""


def _request(method: str, path: str, payload: Optional[Dict[str, Any]]) -> httpx.Request:
    body = b"" if payload is None else json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json", "Content-Length": str(len(body))}
    return httpx.Request(method, path, {}, headers, io.BytesIO(body))


class TestStreamingStopsWhenTheCallerGoesAway(_EngineCase):
    delay = 0.05

    def test_a_broken_pipe_ends_the_stream_before_the_terminator(self) -> None:
        # The caller leaves after three frames. The handler must stop there: it must
        # not keep producing a reply nobody is reading, and it must not claim a
        # clean finish to a stream that was cut off.
        request = _request(
            "POST",
            "/v1/chat/completions",
            {"model": LLAMA, "stream": True, "messages": [{"role": "user", "content": "one two three four five six"}]},
        )
        conn = _FakeConnection(_RecordingWriter(break_after=3))
        self.engine._handle_chat(request, httpx.Response(request, conn))

        self.assertEqual(conn.status, 200)
        self.assertEqual(conn.header("Content-Type"), "text/event-stream")
        written = conn.wfile.joined()
        self.assertIn(b"chat.completion.chunk", written)
        self.assertNotIn(b"data: [DONE]", written, "the stream claimed to finish after the caller left")
        expected_frames = len(self.engine._reply(LLAMA, "one two three four five six").split()) + 2
        self.assertLess(len(conn.wfile.chunks), expected_frames)

    def test_a_closed_socket_ends_the_stream_before_the_terminator(self) -> None:
        # The same thing end to end, which is where the pause between chunks would
        # hold a slot on a real host for as long as the engine felt like it.
        engine = _CountingEngine(self.config(), _quiet())
        server = httpx.Server("127.0.0.1:0", engine.handler(), _quiet())
        server.start()
        self.addCleanup(server.shutdown)

        body = json.dumps(
            {
                "model": LLAMA,
                "stream": True,
                "messages": [{"role": "user", "content": "a b c d e f g h i j k l"}],
            }
        ).encode("utf-8")
        sock = socket.create_connection(("127.0.0.1", server.port()), timeout=10)
        try:
            sock.sendall(
                b"POST /v1/chat/completions HTTP/1.1\r\n"
                b"Host: bothy\r\n"
                b"Content-Type: application/json\r\n"
                b"Content-Length: " + str(len(body)).encode("ascii") + b"\r\n\r\n" + body
            )
            self.assertTrue(sock.recv(64), "no chunk arrived before the caller left")
        finally:
            sock.close()

        words = len(engine._reply(LLAMA, "a b c d e f g h i j k l").split())
        # Long enough that an engine which ignored the disconnect would have run to
        # the end; the last pause happens after the last word frame.
        time.sleep(self.delay * words + 0.5)
        self.assertLess(engine.pauses, words, "the handler kept streaming after the caller went away")


class _CountingEngine(mockengine.Server):
    """An engine that counts the pauses between chunks, so a test can see whether
    a stream was still being produced.
    """

    def __init__(self, config: mockengine.Config, log: Optional[logging.Logger] = None) -> None:
        super().__init__(config, log)
        self.pauses = 0

    def _pause(self) -> None:
        self.pauses += 1
        super()._pause()


class TestRunRefusesConfigurationsItCannotServe(unittest.TestCase):
    def test_a_malformed_model_list(self) -> None:
        with self.assertRaises(ConfigError):
            mockengine.run(None, _quiet(), ["-models", "=sha256:abc"])

    def test_no_models_at_all(self) -> None:
        with self.assertRaises(BothyError) as caught:
            mockengine.run(None, _quiet(), ["-models", ""])
        self.assertIn("no models", str(caught.exception))

    def test_an_address_it_cannot_bind(self) -> None:
        with self.assertRaises(ConfigError):
            mockengine.run(None, _quiet(), ["-listen", "127.0.0.1:not-a-port"])


class TestRunServesUntilCancelled(unittest.TestCase):
    # The command lives behind flags and environment defaults, so the wiring itself
    # gets a test: the name from -name must be what the engine reports, and the
    # process must stop when it is asked to.
    def test_run_serves_until_cancelled(self) -> None:
        addr = _free_addr()
        ctx = threading.Event()
        outcome: Dict[str, Any] = {}

        def serve() -> None:
            try:
                mockengine.run(ctx, _quiet(), ["-listen", addr, "-name", "from-flags", "-models", LLAMA + "=" + DIGEST_A])
            except BaseException as err:  # reported by the assertions below, not swallowed
                outcome["error"] = err
            outcome["returned"] = True

        thread = threading.Thread(target=serve, name="mock-run", daemon=True)
        thread.start()
        self.addCleanup(ctx.set)

        body = None
        for _ in range(100):
            try:
                conn = http.client.HTTPConnection("127.0.0.1", int(addr.rsplit(":", 1)[1]), timeout=2)
                try:
                    conn.request("GET", "/healthz")
                    resp = conn.getresponse()
                    payload = json.loads(resp.read())
                    if resp.status == 200:
                        body = payload
                        break
                finally:
                    conn.close()
            except OSError:
                pass
            time.sleep(0.02)
        self.assertIsNotNone(body, "the mock engine never answered, so this proves nothing")
        self.assertEqual(body["name"], "from-flags")

        ctx.set()
        thread.join(8)
        self.assertFalse(thread.is_alive(), "run did not return after its context was cancelled")
        self.assertNotIn("error", outcome, "run raised %r after a graceful shutdown" % outcome.get("error"))
