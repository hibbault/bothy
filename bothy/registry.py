"""The discovery record, the in-memory TTL store behind the discovery service,
and the client that hosts and clients use to talk to it.

Time is seconds since the epoch as a float -- what `time.time()` returns -- the
way the rest of this package treats it, so a TTL and a `last_seen` compare with
plain subtraction and a test can drive the clock forward instead of sleeping.

The wire shape is the one in PROTOCOL.md: a registration is a `POST /register`
carrying `{"entries": [...]}`, and a lookup is a `GET /models` returning the same
field names. That document is the contract, and this module is one side of it.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from http.client import HTTPException
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, Union
from urllib.parse import urlencode

from . import model
from .errors import BothyError
from .httpx import snippet

# How long a client waits for the registry to answer. Go's `NewClient` builds an
# `*http.Client` with this timeout; there is no other knob on the transport worth
# exposing, so this is the whole of it.
DEFAULT_TIMEOUT = 15.0


@dataclass
class Entry:
    """One registration: a host offering a model at an address.

    `address` is opaque to everyone except the client that dials it -- today a
    host:port, later whatever the transport needs. The share key is deliberately
    absent, because the registry is public and keys travel out of band.

    `free` is how many peer request slots the host had when it last said so, or
    `None` when it did not say. Python has no pointer, so `None` is Go's nil
    `*int`, and the distinction it draws is the whole point: a host that is full
    is a reason to look elsewhere, and a host that never said -- one running
    without a cap, or an implementation that does not report this -- is unknown,
    which is no reason at all. Collapsing the two made an uncapped host sort
    behind a busy one.

    `last_seen` is the registry's clock, not the host's: the store stamps it when
    a heartbeat arrives, and PROTOCOL.md calls the field "set by registry, ignored
    on input", so a `last_seen` a host sends on /register is thrown away rather
    than believed. Decoding is the other direction -- a client reading /models
    takes the timestamp the registry wrote, so a Python and a Go client agree
    about how old an entry is.
    """

    model: str = ""
    digest: str = ""
    address: str = ""
    host: str = ""
    free: Optional[int] = None
    last_seen: float = 0.0

    def free_slots(self) -> Tuple[int, bool]:
        """Reports the peer slots a host said it had, and whether it said
        anything. An absent number is not a claim of being full."""
        if self.free is None:
            return 0, False
        return self.free, True

    def _key(self) -> str:
        """The identity of a registration: the model at an address.

        A host that offers the same model at the same address has re-announced
        itself, not announced a second copy, so this is what a re-registration
        replaces.
        """
        return self.model + "\x00" + self.address

    def to_json(self) -> Dict[str, Any]:
        """The wire form, with Go's field names and Go's omissions.

        `host` and `free` are left out when nothing was said, because an absent
        `free` is how "did not report" is expressed and a zero there would be read
        as "full". `last_seen` is always written: it is a fact about the
        registration rather than something the host supplied.
        """
        out: Dict[str, Any] = {"model": self.model, "digest": self.digest, "address": self.address}
        if self.host:
            out["host"] = self.host
        if self.free is not None:
            out["free"] = self.free
        out["last_seen"] = _rfc3339(self.last_seen)
        return out

    @classmethod
    def from_json(cls, payload: Dict[str, Any]) -> "Entry":
        """Reads one entry back off the wire.

        `last_seen` is read when the wire carries it, so an entry decoded from
        /models reports the age the registry knows and a Go client decoding the
        same body reports the same instant. It stays 0.0 when the field is absent
        or null, which is what an entry that was never stamped looks like.

        A `free` that is not a number, or a `last_seen` that is not an RFC3339
        timestamp, raises the way a Go decode into `*int` or `time.Time` fails,
        rather than being quietly read as "did not report" or as an instant that
        was never sent.
        """
        free = payload.get("free")
        if free is not None:
            if isinstance(free, bool) or not isinstance(free, (int, float)) or free != int(free):
                raise ValueError("free is %r, want an integer" % (free,))
            free = int(free)
        seen = payload.get("last_seen")
        return cls(
            model=payload.get("model") or "",
            digest=payload.get("digest") or "",
            address=payload.get("address") or "",
            host=payload.get("host") or "",
            free=free,
            last_seen=0.0 if seen is None else _parse_rfc3339(seen),
        )


def _rfc3339(seconds: float) -> str:
    """A timestamp as the wire writes one: RFC3339, in UTC.

    Whole seconds when the value is whole -- the common case, and the one
    PROTOCOL.md shows -- and microseconds otherwise. A heartbeat stamped by
    `time.time()` is not whole, and truncating it would make an entry look older
    than it is and throw away precision a Go registry writes.
    """
    dt = datetime.fromtimestamp(seconds, timezone.utc)
    if dt.microsecond:
        return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_rfc3339(text: str) -> float:
    """A timestamp read back into seconds since the epoch.

    Go writes `time.Time` as RFC3339 with up to nine fractional digits, so both
    halves of that have to be understood: a trailing `Z` (which `fromisoformat`
    does not take before Python 3.11) and nanoseconds, of which the clock holds
    microseconds. An offset other than UTC is read as the instant it names rather
    than refused, because it says the same thing about when the heartbeat was.

    A timestamp with no zone at all is refused: it names an instant only in the
    writer's head, and Go declines one too.
    """
    s = text.strip() if isinstance(text, str) else ""
    if s.endswith(("Z", "z")):
        s = s[:-1] + "+00:00"
    if "." in s:
        head, _, rest = s.partition(".")
        # Go writes up to nine fractional digits; the clock here holds six.
        frac = rest[: len(rest) - len(rest.lstrip("0123456789"))]
        s = head + "." + frac[:6] + rest[len(frac) :]
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        raise ValueError("last_seen %r is not an RFC3339 timestamp" % (text,)) from None
    if dt.tzinfo is None:
        raise ValueError("last_seen %r has no time zone" % (text,))
    return dt.timestamp()


def _order(e: Entry) -> Tuple[int, int, str, str]:
    """Go's sort comparator, as a key.

    Hosts that reported free slots first (the 0 sorts before the 1, which is what
    `return knewi` did), most free first, then by host and model so two identical
    lookups agree.

    Python's sort is stable where Go's `sort.Slice` is not, so two entries that
    tie on all four keys keep the order they were registered in; that is the one
    case the Go comparator left to chance.
    """
    slots, known = e.free_slots()
    return (0 if known else 1, -slots, e.host, e.model)


class Store:
    """The in-memory registry.

    Registrations double as heartbeats: there is no separate liveness call. A host
    that stops re-registering expires, so clients stop dialing machines that went
    to sleep instead of hanging on them.

    `now` is the clock: either a zero-argument callable returning seconds since
    the epoch (the default, `time.time`) or a fixed float. A callable is what
    makes time testable -- replace the attribute and a test can move forward
    without sleeping, the way the Go store's `now` field is replaced -- and a
    float is a stopped clock, enough to pin a TTL at one instant.

    The mutex is not decoration: the discovery service answers on threads, and two
    of them touch this map at once.
    """

    def __init__(self, ttl: float, now: Union[Callable[[], float], float] = time.time):
        self._ttl = float(ttl)
        self.now: Union[Callable[[], float], float] = now
        self._entries: Dict[str, Entry] = {}
        self._mu = threading.Lock()

    def _clock(self) -> float:
        """The current time in seconds."""
        now = self.now
        return float(now() if callable(now) else now)

    def ttl(self) -> float:
        """Reports how long an entry survives without a heartbeat."""
        return self._ttl

    def register(self, entries: Sequence[Entry]) -> int:
        """Inserts or refreshes entries, stamping each with the current time.

        Returns how many were accepted; entries without a model or address are
        dropped rather than stored as unusable rows. The caller's entries are left
        alone -- the stamp goes on a copy, the way Go's `for _, e := range` copies
        the struct before writing to it.
        """
        with self._mu:
            now = self._clock()
            n = 0
            for e in entries:
                if not e.model or not e.address:
                    continue
                self._entries[e._key()] = replace(e, last_seen=now)
                n += 1
            return n

    def list(self, name: str) -> List[Entry]:
        """Returns live entries ordered so a client can take the first usable one:
        hosts that reported free slots first, most free first, then by host and
        model for a stable order. An empty name returns every live entry. Expired
        entries are dropped as they are seen.

        A host that reported nothing sorts after every host that reported
        something, but it is not excluded: unknown is not the same as busy, it is
        only not a reason to prefer it.

        "Expired" is strictly older than the TTL: an entry is promised to outlive
        its host's last heartbeat by exactly the TTL, so at exactly the TTL it is
        still live.

        What comes back are copies, which is what Go's slice of structs gives a
        caller for free: mutating a listed entry cannot corrupt the store.
        """
        with self._mu:
            now = self._clock()
            out: List[Entry] = []
            for k, e in list(self._entries.items()):
                if now - e.last_seen > self._ttl:
                    del self._entries[k]
                    continue
                if not model.matches(name, e.model):
                    continue
                out.append(replace(e))
        out.sort(key=_order)
        return out

    def len(self) -> int:
        """Reports how many entries are currently live."""
        with self._mu:
            now = self._clock()
            return sum(1 for e in self._entries.values() if now - e.last_seen <= self._ttl)


class RegistryError(BothyError):
    """A call to the registry did not land, and this says which registry and why.

    Three shapes reach a caller: the registry could not be reached at all, it
    answered with something other than 200, or the answer could not be read as the
    documented shape. All of them name the service, and the two that had a body
    quote a snippet of it -- a refused heartbeat that only says "registration
    failed" leaves the operator with nothing to act on.
    """


class Client:
    """Talks to a discovery service.

    `token` is for registration, and also rides along on a lookup; an empty token
    means registration is open, which is a supported configuration rather than a
    broken one, and it must not turn into an empty credential header.

    Go's `*http.Client` field becomes `timeout`, in seconds. `context.Context` has
    no Python counterpart, so the timeout is what ends a call nobody is answering
    -- which, for this client, is everything the context was doing.
    """

    def __init__(self, base_url: str, token: str = "", timeout: float = DEFAULT_TIMEOUT):
        # A base URL written with a trailing slash is the same registry, and a
        # double slash is a 404 on many servers.
        self.base_url = (base_url or "").rstrip("/")
        self.token = token
        self.timeout = timeout

    def register(self, entries: Sequence[Entry]) -> None:
        """Publishes entries. Calling this once per heartbeat is the entire
        liveness protocol."""
        # Compact, the way Go's Marshal writes a request body; the response bodies
        # a person reads are pretty-printed for exactly the opposite reason.
        body = json.dumps({"entries": [e.to_json() for e in entries]}, separators=(",", ":")).encode("utf-8")
        req = urllib.request.Request(self.base_url + "/register", data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        if self.token:
            req.add_header("Authorization", "Bearer " + self.token)
        self._send(req, "register").close()

    def list(self, name: str) -> List[Entry]:
        """Returns live entries, filtered by model name when name is not empty."""
        # No model means "everyone", which has to be a bare /models request: an
        # empty ?model= is a filter for the empty name on some servers.
        endpoint = self.base_url + "/models"
        if name:
            endpoint += "?" + urlencode({"model": name})
        req = urllib.request.Request(endpoint, method="GET")
        if self.token:
            req.add_header("Authorization", "Bearer " + self.token)
        with self._send(req, "models") as resp:
            raw = resp.read()
        try:
            payload = json.loads(raw)
            entries = payload.get("entries")
            if entries is None:
                return []
            if not isinstance(entries, list):
                raise ValueError("entries is %s, want a list" % type(entries).__name__)
            return [Entry.from_json(e) for e in entries]
        except (AttributeError, TypeError, ValueError) as err:
            raise RegistryError("discovery %s: decode /models: %s" % (self.base_url, err)) from None

    def _send(self, req: urllib.request.Request, what: str):
        """One call, every failure turned into a `RegistryError`.

        A non-200 is reported with its status and a snippet of its body, because
        the registry is the only thing that knows why it said no.
        """
        try:
            return urllib.request.urlopen(req, timeout=self.timeout)
        except urllib.error.HTTPError as err:
            body = err.read()
            raise RegistryError(
                "discovery %s: %s: %d %s: %s"
                % (self.base_url, what, err.code, err.reason or "", snippet(body, 300))
            ) from None
        except (OSError, HTTPException) as err:
            # URLError is an OSError, so a refused connection, a name that does not
            # resolve and a timeout all arrive here, as does a malformed answer
            # from something that is not really a registry. None of them is a call
            # that landed.
            raise RegistryError("discovery %s: %s" % (self.base_url, err)) from None
