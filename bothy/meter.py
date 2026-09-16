"""What each peer uses, and the limits that apply to it.

This is the prerequisite for everything else. You cannot fairly share, limit or
charge for capacity you do not count, and a host that cannot say who is using it
cannot answer the only question that matters when someone pins the GPU: who did
that?

Counting happens here rather than in the engine, because the engine is a black
box we proxy to and may be any of Ollama, vLLM or llama.cpp.

Time is seconds since the epoch as a float -- what `time.time()` returns -- and
every duration here (a budget window, a Retry-After) is a number of those
seconds. `begin` and `end` take the time as an argument rather than reading the
clock themselves, so a test can drive time forward instead of sleeping; the
snapshot reads the clock, because the budget window it reports is relative to
now.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from .errors import BothyError

# How much of a stream we are willing to hold while looking for usage, and how
# large a whole body may be before we stop inspecting it and only pass it on.
_MAX_PENDING = 256 << 10
_MAX_BODY = 8 << 20

# How much a single read asks for when reading a body to the end. It only
# decides how many syscalls a big body costs, never what is passed on.
_READ_ALL = 64 << 10


class _NotACount(Exception):
    """A JSON value that Go's encoding/json would refuse to put into an int."""


_MISSING = object()


def _lookup(obj: Dict[str, Any], name: str):
    """Finds a field the way encoding/json matches one: exactly, or by case."""
    if name in obj:
        return obj[name]
    lowered = name.lower()
    for key, value in obj.items():
        if isinstance(key, str) and key.lower() == lowered:
            return value
    return _MISSING


def _count(value: Any) -> int:
    if value is _MISSING or value is None:
        # A missing field, or a null one, leaves Go's int at zero.
        return 0
    if isinstance(value, bool) or not isinstance(value, int):
        raise _NotACount(repr(value))
    return value


@dataclass(frozen=True)
class Usage:
    """What one response cost, as reported by the engine."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


def extract(data: bytes) -> Tuple[Usage, bool]:
    """Reads token usage out of a single JSON object.

    It understands both shapes a host might be proxying: the OpenAI nested
    "usage" object, and Ollama's flat prompt_eval_count / eval_count. An engine
    that reports nothing yields nothing, which callers record honestly as
    unmetered rather than guessing at a number.

    The second half of the pair is "the engine said something", which is not the
    same as "the engine said zero": `{"usage": {}}` reports a usage block and so
    is reported, while a body with no counts at all is not. A body that is not a
    JSON object of the right shape -- malformed, or an array, or a count that is
    not an integer -- is reported as nothing, the way a Go unmarshal into the
    probe struct would fail.
    """
    if not data:
        return Usage(), False
    try:
        probe = json.loads(data)
    except (ValueError, UnicodeDecodeError):
        return Usage(), False
    if not isinstance(probe, dict):
        return Usage(), False

    nested = _lookup(probe, "usage")
    if nested is _MISSING or nested is None:
        # A null usage block is Go's nil pointer, not an empty one.
        nested = None
    elif not isinstance(nested, dict):
        return Usage(), False

    try:
        if nested is not None:
            usage = Usage(
                prompt_tokens=_count(_lookup(nested, "prompt_tokens")),
                completion_tokens=_count(_lookup(nested, "completion_tokens")),
                total_tokens=_count(_lookup(nested, "total_tokens")),
            )
        else:
            prompt_eval = _count(_lookup(probe, "prompt_eval_count"))
            eval_count = _count(_lookup(probe, "eval_count"))
            prompt_tokens = _count(_lookup(probe, "prompt_tokens"))
            completion_tokens = _count(_lookup(probe, "completion_tokens"))
            total_tokens = _count(_lookup(probe, "total_tokens"))
            if prompt_eval != 0 or eval_count != 0:
                # Ollama's native shape.
                usage = Usage(prompt_tokens=prompt_eval, completion_tokens=eval_count)
            elif prompt_tokens != 0 or completion_tokens != 0 or total_tokens != 0:
                usage = Usage(
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    total_tokens=total_tokens,
                )
            else:
                return Usage(), False
    except _NotACount:
        return Usage(), False

    if usage.total_tokens == 0:
        # Ollama reports no total, and an OpenAI-shaped engine may report only
        # parts, so the total has to be derived -- but only when the engine
        # reported no total of its own, or a reported total would be replaced by
        # the sum of parts that were never sent.
        usage = Usage(
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
            total_tokens=usage.prompt_tokens + usage.completion_tokens,
        )
    return usage, True


class Sniffer:
    """Wraps an upstream response body, passing every byte through untouched
    while reading usage out of the stream.

    The two shapes need different treatment. A whole JSON body is parsed once at
    the end. A server-sent-events stream is parsed frame by frame as it goes,
    holding nothing back -- a sniffer that buffered a stream would undo the entire
    point of streaming, so it never delays a byte on the way to the client.
    """

    def __init__(self, body: Any, content_type: Optional[str]) -> None:
        """Wraps body. content_type decides whether it is read as a stream."""
        self._rc = body
        self._stream = (content_type or "").startswith("text/event-stream")
        self._pending = b""
        self._body = b""
        self._overflow = False
        self._done = False
        self._usage = Usage()
        self._reported = False
        self._counted = 0

    def read(self, size: int = -1) -> bytes:
        """Reads up to size bytes, sniffing whatever goes past.

        With no size (or a negative one) it reads to the end of the body, which
        is what a caller that is not streaming the response does.
        """
        if size is None or size < 0:
            out = bytearray()
            while True:
                chunk = self.read(_READ_ALL)
                if not chunk:
                    break
                out += chunk
            return bytes(out)
        try:
            chunk = self._rc.read(size)
        except BaseException:
            # The body ended badly; it still ended, so anything whole enough to
            # parse is parsed before the error reaches the caller.
            self._complete()
            raise
        if chunk:
            self._counted += len(chunk)
            if self._stream:
                self.scan(chunk)
            elif not self._overflow:
                # A body too large to inspect still passes through byte for byte.
                if len(self._body) + len(chunk) > _MAX_BODY:
                    self._overflow = True
                    self._body = b""
                else:
                    self._body += chunk
        elif size != 0:
            # A zero-length read is not the end of the body, only a request that
            # wanted nothing back.
            self._complete()
        return chunk

    def scan(self, chunk: bytes) -> None:
        """Pulls complete data lines out of the stream, leaving the bytes untouched."""
        self._pending += chunk
        while True:
            i = self._pending.find(b"\n")
            if i < 0:
                break
            self.line(self._pending[:i])
            self._pending = self._pending[i + 1 :]
        if len(self._pending) > _MAX_PENDING:
            self._pending = b""  # malformed stream; stop trying to parse it

    def line(self, raw: bytes) -> None:
        """Reads one line of a stream, if it is a frame carrying usage."""
        trimmed = raw.strip()
        if not trimmed.startswith(b"data:"):
            return
        data = trimmed[len(b"data:") :].strip()
        if not data or data == b"[DONE]":
            return
        usage, ok = extract(data)
        if ok:
            self.merge(usage)

    def merge(self, usage: Usage) -> None:
        """Keeps the latest non-zero numbers: engines send zeroes early and the
        running totals in the final frame."""
        current = self._usage
        self._usage = Usage(
            prompt_tokens=usage.prompt_tokens or current.prompt_tokens,
            completion_tokens=usage.completion_tokens or current.completion_tokens,
            total_tokens=usage.total_tokens or current.total_tokens,
        )
        self._reported = True

    def usage(self) -> Tuple[Usage, bool]:
        """Reports what the response cost. Reported is false when the engine said
        nothing, so callers can distinguish "small" from "unknown"."""
        return self._usage, self._reported

    def bytes(self) -> int:
        """Reports how many response bytes passed through."""
        return self._counted

    def close(self):
        return self._rc.close()

    def _complete(self) -> None:
        """Parses a whole JSON body once the body is over."""
        if self._stream or self._done:
            return
        self._done = True
        if not self._overflow:
            usage, ok = extract(self._body)
            if ok:
                self._usage, self._reported = usage, True
        self._body = b""


# Reasons a request can be refused.
reason_concurrency = "concurrency"
# reason_peer_concurrency is the host having room but this peer not being allowed
# more of it: unlike the first, it is the peer's own doing, and it resolves when
# one of their requests finishes rather than on a timer.
reason_peer_concurrency = "peer_concurrency"
reason_rate = "rate"
reason_quota = "quota"


def _quote(name: str) -> str:
    """A peer name as Go's %q would write it, for a message a person reads."""
    return json.dumps(str(name), ensure_ascii=False)


def _round_seconds(seconds: float) -> int:
    """Rounds to whole seconds the way time.Duration.Round does: half away from zero."""
    if seconds < 0:
        return -int(-seconds + 0.5)
    return int(seconds + 0.5)


def format_duration(seconds: float) -> str:
    """Renders seconds the way Go renders a time.Duration, e.g. "1h0m0s".

    Only whole seconds reach here -- every caller rounds first -- so the
    sub-second forms Go also has ("1.5s", "300ms") are not produced.
    """
    total = _round_seconds(seconds)
    sign = "-" if total < 0 else ""
    total = abs(total)
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    out = "%ds" % secs
    if minutes or hours:
        out = "%dm%s" % (minutes, out)
    if hours:
        out = "%dh%s" % (hours, out)
    return sign + out


class LimitError(BothyError):
    """Says why a request was refused. Concurrency is about the host; rate and
    quota are about the peer.

    `retry_after` and `window` are seconds. `quota` and `window` are set for
    `reason_quota`, so that a refusal can say what the budget was rather than
    only that it is gone.
    """

    def __init__(
        self,
        peer: str = "",
        reason: str = "",
        retry_after: float = 0.0,
        quota: int = 0,
        window: float = 0.0,
    ) -> None:
        self.peer = peer
        self.reason = reason
        self.retry_after = float(retry_after)
        self.quota = quota
        self.window = float(window)
        super().__init__(str(self))

    def __str__(self) -> str:
        if self.reason == reason_concurrency:
            return "the host is serving as many peer requests as it allows right now; retry shortly"
        if self.reason == reason_peer_concurrency:
            return (
                "peer %s is already using its share of the host's slots, and one of its own requests "
                "has to finish first; retry in %s"
                % (_quote(self.peer), format_duration(self.retry_after))
            )
        if self.reason == reason_rate:
            return "peer %s exceeded its request rate; retry in %s" % (
                _quote(self.peer),
                format_duration(self.retry_after),
            )
        if self.reason == reason_quota:
            return "peer %s has used its budget of %d requests per %s; retry in %s" % (
                _quote(self.peer),
                self.quota,
                format_duration(self.window),
                format_duration(self.retry_after),
            )
        return "request refused by the host limiter"


@dataclass(frozen=True)
class Quota:
    """A request budget over a window: a peer may make `requests` requests, and
    then waits for the window to turn over.

    It is a budget rather than a rate, which is the difference between slowing
    somebody down and stopping them. A rate limit of 30 a minute permits 43,200
    requests a day, for ever; a quota of 200 an hour permits 200, and then stops.

    It counts requests and not tokens, which is a limitation rather than a
    preference. Tokens are known only after a response has been produced, so a
    token budget can be enforced only retrospectively -- and an engine that
    reports no usage, which is allowed, would evade it entirely. Requests are
    counted before the work starts, so a request budget always binds. The tokens
    are still there in the usage report for the owner to judge by.
    """

    requests: int = 0
    window: float = 0.0

    def enabled(self) -> bool:
        """Reports whether a quota is actually configured."""
        return self.requests > 0 and self.window > 0


@dataclass
class Counter:
    """What one peer has used since the process started.

    The wire names are not always the field names: `unmetered` is
    `unmetered_responses` in the usage report, because the report is read by
    somebody asking why a total looks low. `last_seen` is an RFC3339 timestamp on
    the wire and seconds since the epoch here, and it stays 0.0 -- Go's zero
    time -- for a peer that has been refused but never finished a request.
    """

    requests: int = 0
    limited: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    response_bytes: int = 0
    unmetered: int = 0
    last_seen: float = 0.0


@dataclass(frozen=True)
class PeerUsage:
    """One row of the usage report.

    It carries a `Counter`'s fields rather than holding one, because that is how
    the wire form reads: the report has one flat object per peer.
    """

    peer: str = ""
    in_flight: int = 0
    requests: int = 0
    limited: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    response_bytes: int = 0
    unmetered: int = 0
    last_seen: float = 0.0
    # quota_used and quota_reset appear only when a quota is configured: how much
    # of the current window this peer has spent, and when it turns over. Without
    # them an owner watching a peer stop has no way to tell a budget from a
    # crash.
    quota_used: int = 0
    quota_reset: str = ""


@dataclass(frozen=True)
class Options:
    """Configures the limiter."""

    # max_concurrent caps requests in flight across the whole host. A GPU
    # serialises work anyway, so this is the limit that actually protects it.
    # Zero means no cap.
    max_concurrent: int = 0
    # owner_reserve is how many slots are kept for the machine's owner out of
    # max_concurrent. Peers are capped at max_concurrent - owner_reserve, so the
    # person paying for the electricity always has headroom and never queues
    # behind strangers.
    #
    # This is a guarantee of headroom, not a reading of what the owner is doing.
    # Their own traffic never passes through here -- they talk to their engine
    # directly -- and no portable engine API reports whether it is busy, so there
    # is nothing to detect. A reservation is the honest shape available.
    owner_reserve: int = 0
    # peer_quota caps one peer's requests over a window. Zero means no budget.
    peer_quota: Quota = Quota()
    # peer_max_concurrent caps how many of the host's slots one peer may hold at
    # once. Without it, max_concurrent is first-come-first-served and a single
    # client with a dozen parallel requests occupies the whole GPU while
    # everybody else is told the host is full -- which is the difference between
    # a shared machine and a taken one. Zero means no separate cap.
    peer_max_concurrent: int = 0
    # requests_per_minute caps one peer's request rate, with a burst of the same
    # size. Zero means no cap.
    requests_per_minute: int = 0


@dataclass
class _PeerState:
    """What the meter knows about one peer."""

    usage: Counter = field(default_factory=Counter)
    in_flight: int = 0
    tokens: float = 0.0
    # refill is when the bucket was last topped up, and None until the peer's
    # first request -- Go's zero time.Time.
    refill: Optional[float] = None
    # window_start is when this peer's current budget window began, and
    # window_used is what it has spent in it. The window starts at the peer's own
    # first admitted request rather than on a shared clock, so budgets do not all
    # turn over at once and "200 an hour" means an hour from when you started.
    # None means no window has been opened, which is not the same as one that has
    # spent nothing: a peer who was only ever refused has no window at all.
    window_start: Optional[float] = None
    window_used: int = 0


class Meter:
    """Tracks per-peer usage and enforces the host's limits."""

    def __init__(self, options: Optional[Options] = None) -> None:
        self._options = options or Options()
        self._mu = threading.Lock()
        self._peers: Dict[str, _PeerState] = {}
        self._in_flight_count = 0

    def begin(self, peer: str, now: float) -> None:
        """Reserves capacity for one request. Every successful begin must be
        paired with exactly one end."""
        with self._mu:
            state = self._peer(peer)
            # Decide before consuming. A request refused by one limit must not
            # spend another limit's allowance, or a peer turned away by a full
            # host would also lose a request of its budget for work that was
            # never done.
            try:
                self._check_limits(peer, state, now)
            except LimitError:
                state.usage.limited += 1
                raise
            self._consume(state, now)
            self._in_flight_count += 1
            state.in_flight += 1

    def _check_limits(self, peer: str, state: _PeerState, now: float) -> None:
        """Decides whether a request may start, without spending anything.

        The order decides which refusal a peer is told about when more than one
        applies. The host's own capacity comes first because it is not the peer's
        doing, then this peer's own slot share, then its budget, which is the
        more final of the answers, then its rate.
        """
        if self._options.max_concurrent > 0 and self._in_flight_count >= self._peer_slots():
            raise LimitError(peer=peer, reason=reason_concurrency)
        if (
            self._options.peer_max_concurrent > 0
            and state.in_flight >= self._options.peer_max_concurrent
        ):
            # The wait is one of the peer's own requests finishing, so there is no
            # real estimate to give. A second is a floor rather than a prediction:
            # saying nothing at all would read as "do not come back".
            raise LimitError(peer=peer, reason=reason_peer_concurrency, retry_after=1.0)
        quota = self._options.peer_quota
        if quota.enabled():
            if not self._window_open(state, now) and state.window_used >= quota.requests:
                raise LimitError(
                    peer=peer,
                    reason=reason_quota,
                    retry_after=state.window_start + quota.window - now,
                    quota=quota.requests,
                    window=quota.window,
                )
        if not self._has_token(state, now):
            raise LimitError(
                peer=peer, reason=reason_rate, retry_after=self._retry_after_at(state, now)
            )

    def _window_open(self, state: _PeerState, now: float) -> bool:
        """Reports whether the peer's budget window has turned over, in which
        case its spend resets."""
        if state.window_start is None:
            return True
        return now - state.window_start >= self._options.peer_quota.window

    def end(
        self,
        peer: str,
        usage: Usage,
        reported: bool,
        response_bytes: int,
        now: float,
    ) -> None:
        """Releases the slot begin reserved and records what the request cost."""
        with self._mu:
            state = self._peer(peer)
            state.usage.requests += 1
            if reported:
                state.usage.prompt_tokens += usage.prompt_tokens
                state.usage.completion_tokens += usage.completion_tokens
            else:
                state.usage.unmetered += 1
            state.usage.response_bytes += response_bytes
            state.usage.last_seen = now
            if state.in_flight > 0:
                state.in_flight -= 1
            if self._in_flight_count > 0:
                self._in_flight_count -= 1

    def free_slots(self) -> int:
        """Reports how many requests this host could take from peers right now.
        This is what gets advertised, so clients route to whoever is least busy.

        It reports *peer* slots, so a reserved slot is not counted and does not
        get advertised. That is what makes the reservation propagate without a
        client needing to understand it: a host with one slot free for its owner
        looks full to everybody else.

        Zero means full here, and only here: the caller decides whether it is
        worth reporting, and a host running without a cap reports nothing at all
        rather than reporting this zero, because "uncapped" is not "busy".
        """
        with self._mu:
            if self._options.max_concurrent <= 0:
                return 0
            return max(self._peer_slots() - self._in_flight_count, 0)

    def quota(self) -> Quota:
        """Reports the per-peer budget this meter enforces, if any."""
        with self._mu:
            return self._options.peer_quota

    def peer_slots(self) -> int:
        """Reports how many requests peers may have in flight at once, which is
        the cap less the slots kept for the owner. Zero means uncapped."""
        with self._mu:
            if self._options.max_concurrent <= 0:
                return 0
            return self._peer_slots()

    def _peer_slots(self) -> int:
        """The concurrency available to peers. Callers hold the lock.

        A reserve that swallows the whole cap yields zero rather than a negative,
        and the caller's `max_concurrent > 0` test means "no slots" rather than
        "no limit": a misconfiguration must fail closed.
        """
        return max(self._options.max_concurrent - self._options.owner_reserve, 0)

    def in_flight(self) -> int:
        """Reports how many requests are being served right now."""
        with self._mu:
            return self._in_flight_count

    def snapshot(self) -> List[PeerUsage]:
        """Returns one row per peer, heaviest user first."""
        with self._mu:
            quota = self._options.peer_quota
            now = time.time()
            out: List[PeerUsage] = []
            for name, state in self._peers.items():
                quota_used = 0
                quota_reset = ""
                if quota.enabled() and state.window_start is not None:
                    # A window that has already turned over is reported as
                    # spent-nothing rather than as whatever it held when it last
                    # ran.
                    reset = state.window_start + quota.window
                    if reset > now:
                        quota_used = state.window_used
                        quota_reset = _rfc3339(reset)
                out.append(
                    PeerUsage(
                        peer=name,
                        in_flight=state.in_flight,
                        requests=state.usage.requests,
                        limited=state.usage.limited,
                        prompt_tokens=state.usage.prompt_tokens,
                        completion_tokens=state.usage.completion_tokens,
                        response_bytes=state.usage.response_bytes,
                        unmetered=state.usage.unmetered,
                        last_seen=state.usage.last_seen,
                        quota_used=quota_used,
                        quota_reset=quota_reset,
                    )
                )
            out.sort(key=lambda row: (-(row.prompt_tokens + row.completion_tokens), row.peer))
            return out

    def _peer(self, name: str) -> _PeerState:
        state = self._peers.get(name)
        if state is None:
            state = _PeerState()
            self._peers[name] = state
        return state

    def _consume(self, state: _PeerState, now: float) -> None:
        """Spends what check_limits allowed: one rate token and one request of
        the peer's budget."""
        if self._options.requests_per_minute > 0:
            state.tokens = self._available(state, now) - 1
            state.refill = now
        quota = self._options.peer_quota
        if quota.enabled():
            if self._window_open(state, now):
                state.window_start = now
                state.window_used = 0
            state.window_used += 1

    def _available(self, state: _PeerState, now: float) -> float:
        """How many tokens the peer's bucket would hold at now, without spending
        any. A token bucket lets a peer spend a minute's worth of requests as a
        burst, then refills continuously."""
        rate = self._options.requests_per_minute
        if rate <= 0:
            return 1.0
        if state.refill is None:
            return float(rate)
        tokens = state.tokens + (now - state.refill) / 60.0 * rate
        if tokens > rate:
            tokens = float(rate)
        return tokens

    def _has_token(self, state: _PeerState, now: float) -> bool:
        return self._options.requests_per_minute <= 0 or self._available(state, now) >= 1

    def _retry_after_at(self, state: _PeerState, now: float) -> float:
        """Estimates how long until the peer's bucket holds one request again."""
        rate = self._options.requests_per_minute
        if rate <= 0:
            return 0.0
        tokens = self._available(state, now)
        if tokens >= 1:
            return 0.0
        return (1 - tokens) / rate * 60.0


def _rfc3339(seconds: float) -> str:
    """A timestamp as the usage report writes one: RFC3339, in UTC, to the second."""
    return datetime.fromtimestamp(seconds, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
