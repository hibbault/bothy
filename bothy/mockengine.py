"""A stand-in for a real inference engine.

It exists so the whole stack can run in CI and on a laptop with no GPU: it
speaks enough of the OpenAI and Ollama APIs for Bothy and ordinary clients to
work against it, and it reports whatever digest you configure. That last part is
what makes the digest-mismatch path testable without downloading the same 8GB
model twice.

This is part of the product, not a test helper. `bothy mock` runs it, so a host
can be developed and demonstrated with no GPU, and the devnet is documented as
the way to try the whole system on one machine -- registry, mock engine, host and
client, wired end to end. `run()` is that command; `Server.handler()` is the same
surface as a router, which is what a test wants when it needs an engine and does
not want to spend a port on it.

Two behaviours here are deliberate, because each is the worse of the two things
real engines do and a host has to cope with both. `POST /v1/chat/completions`
reports usage on a stream only when the request asked for it with
`stream_options.include_usage`, and `GET /v1/models` reports ids and no digests,
so a client can only learn a digest from the mock's own `/internal/models` --
which is exactly the hole the openai lister fills by merging a configured digest
by name.
"""

from __future__ import annotations

import argparse
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, List, Optional

from . import httpx, model
from .config import dur as _env_dur
from .config import listen_default as _listen
from .config import parse_duration as _parse_duration
from .config import text as _env_text
from .errors import ConfigError

_log = logging.getLogger("bothy.mockengine")

# Obviously-fake digests, so a mock's output can never be mistaken for a real
# weights hash. Full length, so they behave like real ones.
_DIGEST_A = "sha256:1111111111111111111111111111111111111111111111111111111111111111"
_DIGEST_B = "sha256:2222222222222222222222222222222222222222222222222222222222222222"

# The models a bare mock serves. Fixed digests per model name, so a client -- and
# a test of a client -- can assert on them without being told them first.
_DEFAULT_MODELS = "llama3.1:8b=" + _DIGEST_A + ",qwen2.5:7b=" + _DIGEST_B

# How often `run` looks at whether it has been asked to stop. Go selects on a
# context; an Event has to be polled, and this is the granularity of a shutdown
# nobody is waiting on with a stopwatch.
_POLL = 0.05


@dataclass
class Config:
    """Describes one mock engine."""

    # Name identifies this engine in its replies, so two mocks are
    # distinguishable when you are looking at the output.
    name: str = ""
    models: List[model.Model] = field(default_factory=list)
    # delay is an artificial pause between streamed chunks, for making latency
    # visible in a demo. (Go's `time.Duration`; seconds here.)
    delay: float = 0.0


@dataclass
class Message:
    """One turn of a conversation."""

    role: str = ""
    content: Any = ""


@dataclass
class StreamOptions:
    """The part of an OpenAI request that asks for usage on a stream."""

    include_usage: bool = False


@dataclass
class ChatRequest:
    """A chat or completion request, decoded as far as the mock reads one."""

    model: str = ""
    stream: bool = False
    messages: List[Message] = field(default_factory=list)
    prompt: Any = ""
    # An OpenAI-compatible engine reports usage on a stream only when the request
    # asks for it. The mock does the same, deliberately: it is the worse of the
    # two real behaviours, and the one the host has to work around.
    stream_options: Optional[StreamOptions] = None

    @classmethod
    def from_json(cls, payload: Dict[str, Any]) -> "ChatRequest":
        """Read one request.

        Fields of the wrong type are refused here, which is what Go's decoder
        does and the one place this mock validates anything: a request whose
        model is a number is a client bug, and answering it with a plausible
        reply would hide that.
        """
        model_value = payload.get("model")
        if model_value is not None and not isinstance(model_value, str):
            raise ValueError("model must be a string")
        stream_value = payload.get("stream")
        if stream_value is not None and not isinstance(stream_value, bool):
            raise ValueError("stream must be a boolean")
        messages_value = payload.get("messages")
        if messages_value is not None and not isinstance(messages_value, list):
            raise ValueError("messages must be a list")
        options_value = payload.get("stream_options")
        if options_value is not None and not isinstance(options_value, dict):
            raise ValueError("stream_options must be an object")
        options = None
        if isinstance(options_value, dict):
            options = StreamOptions(include_usage=bool(options_value.get("include_usage")))
        return cls(
            model=model_value or "",
            stream=bool(stream_value),
            messages=[_message_from_json(m) for m in messages_value or () if isinstance(m, dict)],
            prompt=payload.get("prompt"),
            stream_options=options,
        )

    def wants_usage(self) -> bool:
        """Whether the request asked for usage on a stream."""
        return self.stream_options is not None and self.stream_options.include_usage


def _message_from_json(payload: Dict[str, Any]) -> Message:
    role = payload.get("role")
    return Message(role=role if isinstance(role, str) else "", content=payload.get("content"))


class Server:
    """The mock engine's HTTP surface."""

    def __init__(self, config: Config, log: Optional[logging.Logger] = None) -> None:
        self.config = config
        self.log = log if log is not None else _log

    def handler(self) -> httpx.Router:
        """The engine's routes, which mirror the real ones closely enough that a
        client cannot tell the difference.
        """
        router = httpx.Router()
        router.handle("GET", "/healthz", self._handle_health)
        router.handle("GET", "/internal/models", self._handle_internal_models)
        router.handle("GET", "/api/tags", self._handle_tags)
        router.handle("GET", "/v1/models", self._handle_openai_models)
        router.handle("POST", "/v1/chat/completions", self._handle_chat)
        router.handle("POST", "/v1/completions", self._handle_completions)
        return router

    def _handle_health(self, req: httpx.Request, resp: httpx.Response) -> None:
        resp.json(200, {"engine": "mock", "name": self.config.name, "ok": True})

    def _handle_internal_models(self, req: httpx.Request, resp: httpx.Response) -> None:
        """Bothy's own digest endpoint.

        A real engine has no such route; this is why the mock is only useful for
        testing -- and why a client that learns digests here can be pointed at a
        mock and a real engine with the same code.
        """
        resp.json(200, {"models": model.models_to_json(self.config.models)})

    def _handle_tags(self, req: httpx.Request, resp: httpx.Response) -> None:
        """Mirrors Ollama's /api/tags."""
        now = _rfc3339(time.time())
        out = [
            {
                "name": m.name,
                "model": m.name,
                "modified_at": now,
                "size": 0,
                "digest": m.digest,
            }
            for m in self.config.models
        ]
        resp.json(200, {"models": out})

    def _handle_openai_models(self, req: httpx.Request, resp: httpx.Response) -> None:
        """Mirrors GET /v1/models.

        Ids only. A real endpoint reports no digest, and that emptiness is the
        whole reason the openai lister merges a configured digest by name: a mock
        that leaked one here would make that path look exercised without being
        exercised.
        """
        out = [{"id": m.name, "object": "model", "created": 0, "owned_by": "bothy-mock"} for m in self.config.models]
        resp.json(200, {"data": out, "object": "list"})

    def _handle_chat(self, req: httpx.Request, resp: httpx.Response) -> None:
        """Serve POST /v1/chat/completions, streaming when asked."""
        request = _decode(req, resp)
        if request is None:
            return
        prompt = last_user_message(request.messages)
        reply = self._reply(request.model, prompt)
        if request.stream:
            self._stream_chat(resp, request.model, prompt, reply, request.wants_usage())
            return
        resp.json(
            200,
            {
                "id": "chatcmpl-mock",
                "object": "chat.completion",
                "created": _unix_now(),
                "model": request.model,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": reply},
                        "finish_reason": "stop",
                    }
                ],
                "usage": _usage(prompt, reply),
            },
        )

    def _handle_completions(self, req: httpx.Request, resp: httpx.Response) -> None:
        """Serve the older POST /v1/completions, because plenty of tools still use
        it.
        """
        request = _decode(req, resp)
        if request is None:
            return
        prompt = _text_of(request.prompt)
        reply = self._reply(request.model, prompt)
        if request.stream:
            self._stream_completion(resp, request.model, prompt, reply, request.wants_usage())
            return
        resp.json(
            200,
            {
                "id": "cmpl-mock",
                "object": "text_completion",
                "created": _unix_now(),
                "model": request.model,
                "choices": [{"index": 0, "text": reply, "finish_reason": "stop"}],
                "usage": _usage(prompt, reply),
            },
        )

    def _reply(self, req_model: str, prompt: str) -> str:
        """The answer to a prompt.

        Deterministic, and it names the engine and digest that produced it --
        which is how you tell, from the client side, whose GPU actually answered.
        A model this engine was not configured with is answered with
        "unknown-model" rather than a digest it does not have.
        """
        digest = ""
        for m in self.config.models:
            if model.same_name(m.name, req_model):
                digest = m.digest
        if digest == "":
            digest = "unknown-model"
        if prompt == "":
            prompt = "(empty prompt)"
        return "[%s] model=%s digest=%s you-said=%s" % (self.config.name, req_model, digest, _quote(prompt))

    def _stream_chat(self, resp: httpx.Response, req_model: str, prompt: str, text: str, include_usage: bool) -> None:
        """Stream a chat completion as server-sent events.

        The frames are produced as they are written, not collected first: a
        response that is buffered until it is complete has undone the reason to
        stream, and this is the path that breaks if it ever is.
        """
        resp.send_stream(
            status=200,
            chunks=self._chat_frames(req_model, prompt, text, include_usage),
            headers=[("Cache-Control", "no-cache")],
            content_type="text/event-stream",
        )

    def _stream_completion(self, resp: httpx.Response, req_model: str, prompt: str, text: str, include_usage: bool) -> None:
        """Stream the older completions shape, which has its own frame type."""
        resp.send_stream(
            status=200,
            chunks=self._completion_frames(req_model, prompt, text, include_usage),
            headers=[("Cache-Control", "no-cache")],
            content_type="text/event-stream",
        )

    def _chat_frames(self, req_model: str, prompt: str, text: str, include_usage: bool) -> Iterator[bytes]:
        for i, word in enumerate(text.split()):
            delta = word if i == 0 else " " + word
            yield _chunk_frame("chat.completion.chunk", req_model, delta, None, None)
            self._pause()
        # The closing frame carries the usage when it was asked for. A host that
        # wants to meter a stream has to send stream_options.include_usage; this is
        # the behaviour it is compensating for.
        reported = _usage(prompt, text) if include_usage else None
        yield _chunk_frame("chat.completion.chunk", req_model, "", "stop", reported)
        yield b"data: [DONE]\n\n"

    def _completion_frames(self, req_model: str, prompt: str, text: str, include_usage: bool) -> Iterator[bytes]:
        for i, word in enumerate(text.split()):
            delta = word if i == 0 else " " + word
            yield _sse(
                {
                    "id": "cmpl-mock",
                    "object": "text_completion",
                    "created": _unix_now(),
                    "model": req_model,
                    "choices": [{"index": 0, "text": delta, "finish_reason": None}],
                }
            )
            self._pause()
        # Same reasoning as the chat stream: a closing frame, carrying usage when
        # the request asked for it.
        closing = {
            "id": "cmpl-mock",
            "object": "text_completion",
            "created": _unix_now(),
            "model": req_model,
            "choices": [{"index": 0, "text": "", "finish_reason": "stop"}],
        }
        if include_usage:
            closing["usage"] = _usage(prompt, text)
        yield _sse(closing)
        yield b"data: [DONE]\n\n"

    def _pause(self) -> None:
        """Sleep between chunks, so `delay` makes latency visible.

        Go's version returns false here when the caller's context is done, and its
        caller stops streaming. That information arrives differently in this port
        and does not have to be asked for: httpx writes each frame as it is
        yielded and stops on a broken pipe, which closes the frame generator --
        so a caller that has gone away is a loop that is never resumed, and the
        sleep is the last thing that happens before the stream ends.
        """
        if self.config.delay > 0:
            time.sleep(self.config.delay)


def run(ctx, log: Optional[logging.Logger], args: List[str]) -> None:
    """Parse flags for the "mock" command and serve until ctx is cancelled.

    Go returns an error here; the same failures are raised, so a caller that wants
    to report one gets it as a sentence rather than as a silent non-serving
    process.

    `ctx` is the stand-in for a Go context: anything with an `is_set()` method, so
    a `threading.Event` is the usual one, and calling `set()` on it is what stops
    the server. None means nobody will ask, and it serves until interrupted.
    """
    parser = argparse.ArgumentParser(prog="bothy mock", description="run a fake inference engine")
    parser.add_argument("-listen", "--listen", default=_listen(":11434"), help="address to listen on")
    parser.add_argument(
        "-name",
        "--name",
        default=_env_text("BOTHY_MOCK_NAME", "mock"),
        help="name this engine reports, so two mocks are tellable apart",
    )
    parser.add_argument(
        "-models",
        "--models",
        default=_env_text("BOTHY_MOCK_MODELS", _DEFAULT_MODELS),
        help="models as name=digest,name=digest",
    )
    parser.add_argument(
        "-delay",
        "--delay",
        type=_parse_duration,
        default=_env_dur("BOTHY_MOCK_DELAY", 0.0),
        help="pause between streamed chunks, to make latency visible",
    )
    opts = parser.parse_args(args)

    models = model.parse_list(opts.models)
    if not models:
        raise ConfigError("no models configured")
    log = log if log is not None else _log
    engine = Server(Config(name=opts.name, models=models, delay=opts.delay), log)
    log.info("mock engine ready name=%s models=%s", opts.name, model.format_list(models))

    # httpx.serve is the shared lifecycle, but it serves until interrupted and the
    # mock has to be stoppable from another thread, so the three lines it is made
    # of are spelled out here around a wait that can also be a stop request.
    server = httpx.Server(opts.listen, engine.handler(), log)
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


def _decode(req: httpx.Request, resp: httpx.Response) -> Optional[ChatRequest]:
    """Read a chat or completions body, or answer 400 and return None.

    The refusal is written here rather than left to the router's exception path
    because the mock is mounted directly in-process by tests and by `bothy swarm`,
    where nothing else would turn it into a status code. It is the documented
    OpenAI shape, so a client shows the reason instead of a blank failure.
    """
    try:
        body = json.loads(req.body_bytes())
        if body is None:
            # Go decodes a JSON null into a zero-valued struct without complaint.
            body = {}
        if not isinstance(body, dict):
            raise ValueError("want a JSON object")
        return ChatRequest.from_json(body)
    except ValueError as err:
        resp.error(400, "invalid JSON body: %s" % err)
        return None


def _chunk_frame(object_: str, req_model: str, delta: str, finish: Any, reported: Optional[Dict[str, Any]]) -> bytes:
    """One SSE frame of a streamed chat reply.

    usage is only attached when it is not None, so the delta frames stay the shape
    clients already expect and the numbers ride on the closing frame alone.
    """
    frame = {
        "id": "chatcmpl-mock",
        "object": object_,
        "created": _unix_now(),
        "model": req_model,
        "choices": [{"index": 0, "delta": {"content": delta}, "finish_reason": finish}],
    }
    if reported is not None:
        frame["usage"] = reported
    return _sse(frame)


def _sse(payload: Dict[str, Any]) -> bytes:
    """One server-sent event.

    Compact and with its keys in order, the way Go's encoder leaves a map, so a
    frame means the same bytes on both sides of the port and a client can compare
    one.
    """
    return b"data: " + json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8") + b"\n\n"


def last_user_message(messages: List[Message]) -> str:
    """Return the final user turn, which is what a real engine would answer.

    An assistant turn with nothing after it is answered instead of nothing, and a
    message with no role counts as a user one, because a client that omits the
    role means "this is the prompt" and not "ignore me".
    """
    for m in reversed(messages):
        if m.role == "user" or m.role == "":
            return _text_of(m.content)
    if messages:
        return _text_of(messages[-1].content)
    return ""


def _text_of(v: Any) -> str:
    """Flatten an OpenAI content field, which may be a plain string or a list of
    typed parts.

    Parts are concatenated exactly, with no separator invented between them: a
    split can land mid-word, so adding a space would corrupt the prompt.
    """
    if isinstance(v, str):
        return v
    if isinstance(v, (list, tuple)):
        out = []
        for part in v:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                out.append(part["text"])
        return "".join(out)
    return ""


def _usage(prompt: str, reply: str) -> Dict[str, Any]:
    """Report a rough word count, which is all a mock needs to look plausible."""
    p = len(prompt.split())
    c = len(reply.split())
    return {"prompt_tokens": p, "completion_tokens": c, "total_tokens": p + c}


def _quote(s: str) -> str:
    """Go's %q, near enough: a double-quoted string, escaped the way JSON escapes
    one, so a prompt with a newline in it does not break up the reply.
    """
    return json.dumps(s, ensure_ascii=False)


def _unix_now() -> int:
    """Go's time.Now().Unix()."""
    return int(time.time())


def _rfc3339(when: float) -> str:
    """A timestamp in Go's time.RFC3339, which is what Ollama's API reports and
    what a client parses it as.
    """
    return datetime.fromtimestamp(when, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
