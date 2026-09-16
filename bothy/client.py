"""The "connect" side: a local, OpenAI-compatible endpoint that is really
somebody else's GPU.

It listens on 11223, a port of Bothy's own, and deliberately not on 11434. Bothy
sits *beside* a local engine rather than impersonating it: a machine that already
runs Ollama keeps it, and a tool that wants a borrowed model is pointed at
Bothy's port instead of being told a lie about where the model is. Serving is a
different port again (7777), so one machine can do both at once — serve its own
model and use somebody else's in the same session.

Go's `context.Context` has no counterpart here, so nothing in this module takes
one. What the context carried was a cancellation, and the only place that was
needed is `serve`, which takes the same stand-in the rest of this package uses:
anything with an `is_set()` method, usually a `threading.Event`, or None when
nobody is going to ask. The other thing a context carried was the request
deadline `fetch_models` passed to its client, and that is `Client.timeout` —
per call timeouts belong to the client object rather than to the call.
"""

from __future__ import annotations

import argparse
import http.client
import json
import logging
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from email.message import Message
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

from . import config, httpx, model, registry
from .errors import BothyError, ConfigError

# How long the client waits for a host to answer a question it asked itself -- a
# model list at startup, or a fresh list of hosts. Go builds one `http.Client` for
# this with a 20 second timeout, shared by every call in the package; here it is
# the client's own `timeout` attribute, because a timeout belongs to the object
# that makes the call rather than to the call.
DEFAULT_TIMEOUT = 20.0

# How long the client is willing to spend *dialling* a host. Go's
# `http.DefaultTransport` bounds the dial and nothing else, which is the shape
# that matters: a host that streams for ten minutes is not a host that has hung.
DIAL_TIMEOUT = 30.0

# The proxy side, from routing.go.
#
# The client used to resolve one host and stay there, which made "route to
# whoever is least busy" true only at connect time: a 429 is a *successful*
# response, so it was relayed to the caller and the next host was never asked. A
# host answering "full" was therefore the end of the request rather than the start
# of a search.
#
# So the choice of host moved from the Director — which runs once — into the
# transport, which runs per attempt and can try the next one. What counts as a
# reason to move on is a refusal: the host saying "not now" rather than the engine
# answering.

# max_host_attempts is how many hosts one request may be tried against before the
# caller is told. Three, not every host: the point is to find room in a small
# fleet, and a request that surveys twenty hosts is a slow way to say "everything
# is busy".
MAX_HOST_ATTEMPTS = 3

# refused_longest caps how long a host is skipped after it refuses. An hour is a
# real answer — a spent budget, a full host — but taking it as gospel would leave
# a one-host client looking broken for that hour, so the client asks again once
# the cap passes and takes the second refusal.
REFUSED_LONGEST = 5 * 60.0

# assumed_wait is how long a host is skipped when it refuses without saying. A
# host that does not set Retry-After still meant "not now".
ASSUMED_WAIT = 30.0

# max_replay_body is how much of a request body is held in memory so a refused
# request can be tried against another host. Past it the request is streamed,
# which means it is sent once: a prompt carrying images is legitimately megabytes,
# and buffering all of them to make a retry possible would cost more than the
# retry is worth. Such a request still gets the host's refusal, Retry-After and
# all.
MAX_REPLAY_BODY = 1 << 20

# err_nothing_reachable is a failure rather than a wait, and the client's error
# handler turns it into a 502. Go holds this as a sentinel error; nothing here
# compares against it, so it is the sentence itself.
NOTHING_REACHABLE = "no host could be reached"

_log = logging.getLogger("bothy.client")


def _message(pairs: Sequence[Tuple[str, str]] = ()) -> Message:
    """A case-insensitive header bag, the shape `Request.headers` and
    `Response.headers` really are, and the shape `http.client` wants back."""
    m = Message()
    for key, value in pairs:
        m.add_header(key, str(value))
    return m


def _quote(s: str) -> str:
    """Go's %q, near enough for an error message: a double-quoted string, escaped
    the way JSON escapes one, so an address with a quote in it stays legible."""
    return json.dumps(s)


def _rounded(seconds: float) -> float:
    """Go's `Duration.Round(time.Second)`, as seconds.

    Rounded to the nearest whole second, halves away from zero, which is what
    `(d + m/2) / m * m` does there and what Python's banker's rounding does not.
    """
    return float(int(seconds + 0.5)) if seconds >= 0 else -float(int(-seconds + 0.5))


class MismatchError(BothyError):
    """Reports that a host's advertised digest is not the one that was required.
    Retrying will not fix it, so it is treated as fatal.
    """

    def __init__(self, model: str = "", expected: str = "", actual: str = "") -> None:
        super().__init__(self._render(model, expected, actual))
        self.model = model
        self.expected = expected
        self.actual = actual

    @staticmethod
    def _render(name: str, expected: str, actual: str) -> str:
        if actual.strip() == "":
            return "cannot verify %s: host advertised no digest, but %s was expected" % (
                name,
                model.normalize_digest(expected),
            )
        return "digest mismatch for %s: expected %s, host offers %s" % (
            name,
            model.normalize_digest(expected),
            model.normalize_digest(actual),
        )

    def __str__(self) -> str:
        # Rendered from the fields rather than frozen at construction, so an
        # error built by hand -- a test, or a caller re-raising one -- reads the
        # same as one `verify` raised.
        return self._render(self.model, self.expected, self.actual)


def verify(expected: str, actual: str, name: str) -> None:
    """Decide whether the digest a host advertises satisfies what was asked for.

    An empty expectation accepts anything, which is the default because most
    people just want a working model. The moment a digest is named, a host
    offering different weights is refused rather than silently used.

    Go returns an `error`; the refusal is raised here, as a `MismatchError`,
    which is the type the client catches to tell a fatal pin from a transient
    failure.
    """
    if expected.strip() == "":
        return
    if not model.equal_digest(expected, actual):
        raise MismatchError(model=name, expected=expected, actual=actual)


@dataclass
class Config:
    """Describes one client."""

    listen: str = ""
    # host_address points straight at a host, skipping discovery.
    host_address: str = ""
    discovery_url: str = ""
    model: str = ""
    share_key: str = ""
    expected_digest: str = ""
    # local_api_key, when set, is required on the local endpoint. Empty leaves it
    # open, which is normal because it only listens on loopback.
    local_api_key: str = ""


class Client:
    """Resolves hosts and forwards local requests to whichever one will take
    them, moving on when a host refuses. See the routing section below.
    """

    def __init__(self, cfg: Config, log: Optional[logging.Logger] = None) -> None:
        self.cfg = cfg
        self.log = log if log is not None else _log
        self.disc = registry.Client(cfg.discovery_url, "") if cfg.discovery_url else None
        self.timeout = DEFAULT_TIMEOUT

        # Re-entrant because the status page reads the fleet while it is holding
        # the lock, exactly as the Go code does. Go gets away with two RLocks on
        # an RWMutex only by luck -- a writer arriving between them can deadlock
        # it -- so the re-entrancy is here on purpose rather than as a copy of
        # that accident.
        self.mu = threading.RLock()
        self.entries: List[registry.Entry] = []  # the hosts to try, in order
        self.next = 0  # where the rotation last stopped
        self.avoid: Dict[str, unavailable] = {}  # host -> why, and until when, it is skipped
        self.unusable = ""  # the last entry whose address could not be dialled
        self.unusable_seen = False  # whether such an entry was seen at all
        self.entry = registry.Entry()  # the host this client last talked to
        self.target: Optional[urllib.parse.SplitResult] = None

    def serve(self, ctx=None) -> None:
        """Resolve hosts and forward local requests to whichever will take them,
        until ctx is cancelled.

        A digest mismatch is a configuration error, so it fails the service
        instead of answering requests that can never succeed — and it is only an
        error when *no* host matches, since one host serving different weights is
        now just a host to avoid. A fleet that is merely busy is fine to start
        against: refusals move a request along, and a host that is not up yet is
        re-resolved on the first request, because compose starts services in
        whatever order it likes.

        It is a method rather than only the body of `run` because one process can
        be both halves now — see app.

        `ctx` is the stand-in for a Go context: anything with an `is_set()` method
        (a `threading.Event` is the usual one), or None to serve until
        interrupted. Everything else the Go context carried is gone, because
        nothing here needed it.
        """
        try:
            self.ensure_candidates()
        except MismatchError:
            raise
        except BothyError as err:
            self.log.warning("no host reachable yet; will connect on the first request err=%s", err)
        # Pick a host now rather than at the first request, so /bothy/status can
        # say where requests would go instead of looking disconnected while it is
        # not.
        entry, target, ok = self.choose(time.time())
        if ok:
            self.log.info(
                "connected host=%s model=%s digest=%s digest_checked=%s",
                target.netloc,
                entry.model,
                or_unknown(entry.digest),
                self.cfg.expected_digest != "",
            )

        # httpx.serve is the shared lifecycle, but it serves until interrupted and
        # this has to be stoppable from another thread, so the three lines it is
        # made of are spelled out here around a wait that can also be a stop.
        server = httpx.Server(self.cfg.listen, self.handler(), self.log)
        self.log.info("listening addr=%s", server.addr)
        server.start()
        try:
            if ctx is None:
                server.wait()
            else:
                while not ctx.is_set():
                    time.sleep(0.05)
        except KeyboardInterrupt:
            # A person asking to stop is not a failure.
            pass
        finally:
            server.shutdown()

    def handler(self) -> httpx.Handler:
        """The client's routes. The local status route is the only thing Bothy
        answers itself; everything else goes to the host.
        """
        router = httpx.Router()
        router.handle("GET", "/bothy/status", self.handle_status)
        router.default(httpx.require_token(self.cfg.local_api_key, self.forward))
        return httpx.log_requests(self.log, router)

    def forward(self, req: httpx.Request, resp: httpx.Response) -> None:
        """Make sure there is a host to try, then proxy. Which host, and whether
        to try another one, is the transport's decision.
        """
        try:
            self.ensure_candidates()
        except BothyError as err:
            resp.error(502, str(err))
            return
        try:
            reply = _Routing(self).round_trip(req)
        except (BothyError, OSError, http.client.HTTPException) as err:
            self.refuse(resp, err)
            return
        try:
            self.relay(req, resp, reply)
        finally:
            reply.close()

    def relay(self, req: httpx.Request, resp: httpx.Response, reply: "_Upstream") -> None:
        """Write a host's answer to the caller, as it arrives.

        Nothing is buffered on the way out for the same reason nothing is
        buffered on the way in: a streamed completion that is collected before it
        is forwarded is a streamed completion nobody wanted.
        """
        headers: List[Tuple[str, str]] = []
        content_type = None
        for name, value in httpx.end_to_end(reply.headers):
            if name.lower() == "content-type":
                content_type = value
                continue
            headers.append((name, value))
        resp.send_stream(
            status=reply.status,
            chunks=reply.chunks(),
            headers=headers,
            content_type=content_type,
            content_length=content_length(reply.headers),
        )

    def refuse(self, resp: httpx.Response, err: Exception) -> None:
        """Answer a request that never reached a host.

        Nobody taking the request is not the same failure as the network being
        broken, and the difference matters to a caller: one means "come back
        later", the other means "something is wrong". So a fleet that refused
        everything is a 429 (or a 503 when it did not say when to come back), and
        an unreachable or unusable fleet is a 502.
        """
        no_host = is_no_host(err)
        if no_host is not None and no_host.known:
            if no_host.retry_after > 0:
                wait = str(int(no_host.retry_after + 0.5))
                resp.error(429, str(no_host), headers=[("Retry-After", wait)])
                return
            resp.error(503, str(no_host))
            return
        self.log.warning("the request never reached a host err=%s", err)
        resp.error(502, str(err))

    def ensure_candidates(self) -> None:
        """Resolve the hosts to try, once, and keep them until something suggests
        the list is stale.
        """
        with self.mu:
            cached = len(self.entries) > 0
        if cached:
            return
        self.set_entries(self.resolve())

    def resolve(self) -> List[registry.Entry]:
        """List the hosts this client may use, in the order to try them, and
        applies the one rule that can rule out a host before it is ever asked: the
        digest, when one is required.
        """
        entries = self.lookup()
        if self.cfg.expected_digest.strip() == "":
            return entries

        matched: List[registry.Entry] = []
        mismatch: Optional[MismatchError] = None
        for entry in entries:
            try:
                verify(self.cfg.expected_digest, entry.digest, entry.model)
            except MismatchError as err:
                mismatch = err
                self.log.warning(
                    "skipping a host whose weights are not the ones required host=%s digest=%s",
                    entry.address,
                    or_unknown(entry.digest),
                )
                continue
            matched.append(entry)
        if not matched:
            # Nothing matched, so unlike one bad host in a fleet this is not
            # something the next attempt could fix, and it is reported as fatal.
            if mismatch is not None:
                raise mismatch
            raise BothyError('no host offering "%s" matches the required digest' % self.cfg.model)
        return matched

    def lookup(self) -> List[registry.Entry]:
        """Where the list comes from: one host named directly, or discovery."""
        addr = (self.cfg.host_address or "").strip()
        if addr != "":
            base = with_scheme(addr)
            try:
                target = urllib.parse.urlsplit(base)
                target.port  # a port that is not a number is not an address
            except ValueError as err:
                raise BothyError('host %s: %s' % (_quote(addr), err)) from None
            if not target.netloc:
                raise BothyError('host %s has no host' % _quote(addr))
            # The scheme is kept, deliberately: the transport dials this address,
            # and storing the host alone dropped it. A host configured as
            # `https://box:7777` was therefore asked for its model list over TLS
            # and then proxied to in cleartext, which is the worst of both.
            entry = registry.Entry(address=base, model=self.cfg.model)
            # A directly-addressed host can be asked what it serves, which is how
            # a direct connection still ends up with a verifiable digest.
            try:
                models = self.fetch_models(base)
            except BothyError as err:
                self.log.warning(
                    "cannot read the host's model list, so the digest cannot be checked host=%s err=%s",
                    addr,
                    err,
                )
                return [entry]
            found, ok = pick(models, self.cfg.model)
            if ok:
                entry.model, entry.digest = found.name, found.digest
            return [entry]
        if self.disc is None:
            raise ConfigError("nothing to connect to: set -host or -discovery-url")
        entries = self.disc.list(self.cfg.model)
        if not entries:
            if self.cfg.model == "":
                raise BothyError("no hosts are registered right now")
            raise BothyError('no host is offering "%s" right now' % self.cfg.model)
        # The registry orders entries for exactly this purpose — most free slots
        # first — and the client keeps that order, because it is the routing.
        return entries

    def fetch_models(self, base: str) -> List[model.Model]:
        """Ask a host what it serves, so digests can be checked even when
        discovery was not involved.
        """
        endpoint = base.rstrip("/") + "/bothy/models"
        req = urllib.request.Request(endpoint, method="GET")
        if self.cfg.share_key:
            req.add_header(httpx.KEY_HEADER, self.cfg.share_key)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as err:
            # The refusal is a response too, and Go closes it on the way out.
            err.close()
            raise BothyError("/bothy/models: %d %s" % (err.code, err.reason)) from None
        except (urllib.error.URLError, OSError, http.client.HTTPException) as err:
            raise BothyError(str(err)) from None
        try:
            payload = json.loads(raw)
            return model.models_from_json(payload.get("models"))
        except (AttributeError, TypeError, ValueError) as err:
            raise BothyError("decode /bothy/models: %s" % err) from None

    def handle_status(self, req: httpx.Request, resp: httpx.Response) -> None:
        with self.mu:
            status: Dict[str, Any] = {
                "listening": self.cfg.listen,
                "discovery": self.cfg.discovery_url,
                "requested_model": self.cfg.model,
                "expected_digest": self.cfg.expected_digest,
                "connected": self.target is not None,
            }
            if self.target is not None:
                status["host"] = self.target.netloc
                status["model"] = self.entry.model
                status["digest"] = self.entry.digest
                # No required digest means any host satisfies the policy, so the
                # connection counts as verified. (equal_digest deliberately
                # reports two empty digests as unequal — "unknown" must never
                # read as "verified" — but that is about comparing digests, not
                # about whether this connection met its requirement.)
                status["digest_verified"] = self.cfg.expected_digest.strip() == "" or model.equal_digest(
                    self.cfg.expected_digest, self.entry.digest
                )
            status["hosts"] = self.host_report()
        resp.json(200, status)

    def host_report(self) -> List[Dict[str, Any]]:
        """Describe the fleet this client knows about, for /bothy/status.

        It exists because "why is every request going to one host?" is otherwise
        unanswerable: the answer is usually a host that refused, or one that could
        not be reached, and both are invisible from outside.
        """
        with self.mu:
            now = time.time()
            out: List[Dict[str, Any]] = []
            for entry in self.entries:
                row: Dict[str, Any] = {"address": entry.address, "model": entry.model}
                state = self.avoid.get(entry.address)
                if state is None or not now < state.until:
                    out.append(row)
                    continue
                row["skipped_for"] = config.format_duration(_rounded(state.until - now))
                row["reason"] = "refused" if state.refusal else "unreachable"
                out.append(row)
            return out

    # --- routing ------------------------------------------------------------

    def choose(self, now: float) -> Tuple[Optional[registry.Entry], Optional[urllib.parse.SplitResult], bool]:
        """Return the next host to try, skipping any that have asked to be left
        alone, and record it as the host this client is talking to.

        It carries on from where the last request stopped rather than starting at
        the top of the list, so a fleet takes turns instead of every client in it
        aiming at the same host.

        `now` is seconds since the epoch, the way the rest of this package counts
        time and what `time.time()` returns; it is a parameter rather than a call
        to the clock so a test can move the clock instead of sleeping. Go returns
        the zero `Entry` alongside a false; here that pair is `(None, None,
        False)`.
        """
        with self.mu:
            for i in range(len(self.entries)):
                entry = self.entries[(self.next + i) % len(self.entries)]
                state = self.avoid.get(entry.address)
                if state is not None and now < state.until:
                    continue
                try:
                    target = urllib.parse.urlsplit(with_scheme(entry.address))
                    target.port  # a port that is not a number is not an address
                    usable = target.netloc != ""
                except ValueError:
                    usable = False
                if not usable:
                    self.unusable, self.unusable_seen = entry.address, True
                    self.log.warning("skipping an entry with no address this client can dial address=%s", entry.address)
                    continue
                self.next = (self.next + i + 1) % len(self.entries)
                self.entry, self.target = entry, target
                return entry, target, True
        return None, None, False

    def serving(self, entry: registry.Entry, target: urllib.parse.SplitResult) -> None:
        """Record that a host took the request, which clears anything it said
        earlier about being busy.
        """
        with self.mu:
            self.avoid.pop(entry.address, None)
            self.entry, self.target = entry, target

    def skip(self, entry: registry.Entry, now: float, wait: float, refusal: bool) -> None:
        """Record that a host is not to be tried again for a while."""
        if wait <= 0:
            wait = ASSUMED_WAIT
        if wait > REFUSED_LONGEST:
            wait = REFUSED_LONGEST
        with self.mu:
            self.avoid[entry.address] = unavailable(until=now + wait, refusal=refusal)

    def set_entries(self, entries: List[registry.Entry]) -> None:
        """Replace the candidate list, keeping what hosts have said about being
        busy: a fresh list does not mean an hour-old budget has been refilled.
        """
        with self.mu:
            self.entries, self.next = list(entries), 0

    def no_host_error(self, now: float) -> Exception:
        """Explain why nobody took the request.

        Three answers, and they are not interchangeable: a fleet that is busy
        means "come back", a fleet that cannot be reached means "this is broken",
        and an entry nobody can dial means "this address is wrong".

        Go returns an `error`; the same object is returned here rather than
        raised, so the caller decides — which is what makes the reasoning above
        testable without a socket.
        """
        with self.mu:
            if not self.entries:
                return NoHostError()
            soonest: Optional[float] = None
            refusal_host = ""
            unreachable = False
            for entry in self.entries:
                state = self.avoid.get(entry.address)
                if state is None:
                    continue
                if not state.refusal:
                    unreachable = True
                    continue
                if soonest is None or state.until < soonest:
                    soonest, refusal_host = state.until, entry.address
            if soonest is not None:
                # A host that answered is the more useful answer when there is
                # one, because it comes with a time.
                return NoHostError(host=refusal_host, retry_after=soonest - now, known=True)
            if unreachable:
                return BothyError(NOTHING_REACHABLE)
            if self.unusable_seen:
                return NoHostError(undialable=True, unusable=self.unusable)
            return NoHostError(known=True)


def new(cfg: Config, log: Optional[logging.Logger] = None) -> Client:
    """Build a client. Go's `New`."""
    return Client(cfg, log)


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
            return config.format_duration(value)
        return str(value)
    text = str(value)
    if text == "":
        return ""
    return json.dumps(text, ensure_ascii=False)


class _GoHelp(argparse.HelpFormatter):
    """Help that prints a default the way Go's flag package does: "(default
    "127.0.0.1:11223")".

    argparse's own ArgumentDefaultsHelpFormatter writes "(default: ...)" and only
    for actions it can render; the defaults here are already resolved from the
    environment and any config file by the time the parser is built, so showing
    them is the documented way to see where a client would actually connect.
    """

    def _get_help_string(self, action: argparse.Action) -> str:
        text = action.help or ""
        if "%(default)" in text or action.default is None:
            return text
        return text + " (default %s)" % _default_text(action.default)


class _Parser(argparse.ArgumentParser):
    """An ArgumentParser that exits like Go's flag package.

    Go's ExitOnError exits 2 for a bad flag and 0 for -h, and writes both the
    error and the help to stderr. argparse's `print_help` and `print_usage` name
    stdout before they reach `_print_message`, so both are overridden here: help
    on stdout would be mixed into whatever the caller was reading the command's
    output for, and a person piping it through a pager would never see it.
    """

    def _print_message(self, message: Optional[str], file=None) -> None:  # type: ignore[override]
        if message:
            (file or sys.stderr).write(message)

    def print_usage(self, file=None) -> None:  # type: ignore[override]
        self._print_message(self.format_usage(), file or sys.stderr)

    def print_help(self, file=None) -> None:  # type: ignore[override]
        self._print_message(self.format_help(), file or sys.stderr)

    def error(self, message: str) -> None:
        self.print_usage(sys.stderr)
        self.exit(2, "%s: error: %s\n" % (self.prog, message))


def run(ctx=None, log: Optional[logging.Logger] = None, args: Optional[List[str]] = None) -> None:
    """Parse flags for the "connect" command and serve until ctx is cancelled.

    Go returns an error here; the same failures are raised, so a caller that
    wants to report one gets it as a sentence rather than as a silent
    non-serving process.

    `ctx` is the stand-in for a Go context, as in `Client.serve`: anything with
    an `is_set()` method, or None. `args` is None for a real command line, which
    is what makes the three tests that drive this stay out of sys.argv.
    """
    # `-h` writes to stderr and exits 0, which is what Go's flag package does and
    # what every other command here does.
    parser = _Parser(
        prog="bothy connect",
        description="use somebody else's GPU from a local port",
        add_help=False,
        allow_abbrev=False,
        formatter_class=_GoHelp,
    )
    parser.add_argument("-h", "-help", "--help", action="help", help="show this help and exit")
    parser.add_argument(
        "-listen",
        "--listen",
        default=config.listen_default("127.0.0.1:11223"),
        help="local address for the OpenAI-compatible endpoint",
    )
    parser.add_argument(
        "-host",
        "--host",
        dest="host_address",
        default=config.text("BOTHY_HOST", ""),
        help="host to connect to directly, e.g. box.example:7777",
    )
    parser.add_argument(
        "-discovery-url",
        "--discovery-url",
        dest="discovery_url",
        default=config.text("BOTHY_DISCOVERY_URL", ""),
        help="registry to look the host up in",
    )
    parser.add_argument("-model", "--model", default=config.text("BOTHY_MODEL", ""), help="model to use, e.g. llama3.1:8b")
    parser.add_argument("-share-key", "--share-key", dest="share_key", default=config.text("BOTHY_SHARE_KEY", ""), help="key the host expects")
    parser.add_argument(
        "-expected-digest",
        "--expected-digest",
        dest="expected_digest",
        default=config.text("BOTHY_EXPECTED_DIGEST", ""),
        help="required weights digest; anything else is refused",
    )
    parser.add_argument(
        "-local-api-key",
        "--local-api-key",
        dest="local_api_key",
        default=config.text("BOTHY_LOCAL_API_KEY", ""),
        help="key required on the local endpoint (optional)",
    )
    opts = parser.parse_args(args)

    cfg = Config(
        listen=opts.listen,
        host_address=opts.host_address,
        discovery_url=opts.discovery_url,
        model=opts.model,
        share_key=opts.share_key,
        expected_digest=opts.expected_digest,
        local_api_key=opts.local_api_key,
    )
    new(cfg, log).serve(ctx)


# --- the transport ----------------------------------------------------------
#
# The two types and the four helpers below have no Go counterpart in name but do
# in role: a `*http.Response` still on the wire, and the `io.ReadCloser` the
# transport reads a body through. Everything else on this side of the file is
# routing.go.


class _Upstream:
    """One answer from a host, still on the wire.

    Go's `*http.Response`: the status and headers are there as soon as the host
    has spoken, and the body is read as it arrives. What is read is `read1` rather
    than `read`, and that is the whole of the streaming guarantee: Go's body reader
    hands back whatever has arrived, while `read(n)` waits until it has n bytes,
    so a streamed completion would sit in a buffer here until enough of it existed
    to fill one. `read1` is the reader that answers with what there is.
    """

    __slots__ = ("status", "headers", "_raw", "_conn")

    def __init__(self, raw: http.client.HTTPResponse, conn: http.client.HTTPConnection) -> None:
        self.status = raw.status
        self.headers = _message(raw.getheaders())
        self._raw = raw
        self._conn = conn

    def chunks(self) -> Iterator[bytes]:
        """The body, in the pieces it arrives in."""
        while True:
            chunk = self._raw.read1(httpx.CHUNK)
            if not chunk:
                return
            yield chunk

    def close(self) -> None:
        """Let go of the connection, whether or not the body was read."""
        try:
            self._raw.close()
        finally:
            self._conn.close()


class NoHostError(BothyError):
    """Reports that no host would take the request, and when the soonest one
    expects to be free. It is one of the four 429s moved to the client: the caller
    is told to come back rather than told the fleet does not exist.
    """

    def __init__(
        self,
        host: str = "",
        retry_after: float = 0.0,
        known: bool = False,
        undialable: bool = False,
        unusable: str = "",
    ) -> None:
        super().__init__()
        self.host = host
        self.retry_after = retry_after
        # known is False when no host was even asked, which is a different
        # answer: nobody offers this model, rather than everybody is busy.
        self.known = known
        # undialable says an entry was seen whose address could not be used —
        # empty, or not a URL. unusable carries that address when there was one,
        # because an empty address is itself a case and "" is not a sentinel that
        # can be checked.
        self.undialable = undialable
        self.unusable = unusable

    def __str__(self) -> str:
        if self.undialable and self.unusable != "":
            return "no host could be dialled: %s is not an address this client can use" % _quote(self.unusable)
        if self.undialable:
            return "no host could be dialled: a registry entry has no address at all"
        if not self.known:
            return "no host is offering this model right now"
        if self.retry_after <= 0:
            return "every host refused the request, and none said when to try again"
        return "every host refused the request; %s asked to be tried again in %s" % (
            self.host,
            config.format_duration(_rounded(self.retry_after)),
        )


@dataclass
class unavailable:
    """Why a host is being skipped: either it answered "not now", or it could not
    be reached at all. The difference decides what the caller is told when nobody
    is left — a wait, or a failure.
    """

    until: float = 0.0
    refusal: bool = False


class _Routing:
    """The transport behind the client's proxy. It decides which host each attempt
    goes to, swaps the caller's credentials for the share key, and moves on when a
    host refuses — all before a single byte reaches the caller, which is what
    makes a retry possible at all.

    Go has this as a `RoundTripper` handed to `httputil.ReverseProxy`, because
    net/http owns the copying of the answer back to the caller. There is no
    ReverseProxy in the standard library here, so `round_trip` ends where Go's
    does — with an answer still on the wire — and `Client.relay` writes it out.
    """

    def __init__(self, c: Client) -> None:
        self.c = c

    def round_trip(self, req: httpx.Request) -> _Upstream:
        c = self.c
        body = req.body_chunks() if (req.content_length > 0 or req.chunked) else None
        held, rest, replayable = read_replayable(body)
        has_body = body is not None

        attempts = MAX_HOST_ATTEMPTS
        if not replayable:
            # The body cannot be sent twice, so there is no second attempt to make.
            attempts = 1

        best: Optional[_Upstream] = None
        unreachable: Optional[Exception] = None
        for i in range(attempts):
            now = time.time()
            entry, target, ok = c.choose(now)
            if not ok and i == 0:
                # Every host has asked to be left alone, which may simply mean the
                # list is old enough to be out of date. One refresh per request.
                entries = self._refresh()
                if entries:
                    c.set_entries(entries)
                    entry, target, ok = c.choose(time.time())
            if not ok:
                if best is not None:
                    return best
                if unreachable is not None:
                    raise unreachable
                raise c.no_host_error(time.time())

            conn = None
            try:
                conn = _dial(target)
                conn.request(
                    req.method,
                    _path_of(req, target),
                    body=_attempt_body(held, rest, replayable, has_body),
                    headers=_outgoing_headers(req, c.cfg.share_key, held, replayable, has_body),
                    encode_chunked=not replayable and req.chunked,
                )
                raw = conn.getresponse()
            except (OSError, http.client.HTTPException) as err:
                if conn is not None:
                    conn.close()
                # A host that cannot be reached is not a host saying "not now", so
                # it is skipped for a while but never reported to the caller as a
                # wait.
                c.skip(entry, time.time(), ASSUMED_WAIT, False)
                c.log.warning("host unreachable, trying the next one host=%s err=%s", target.netloc, err)
                unreachable = err
                if not replayable:
                    raise
                continue

            reply = _Upstream(raw, conn)
            wait, refusing = refusal(reply)
            if refusing:
                c.skip(entry, time.time(), wait, True)
                c.log.info(
                    "host refused, trying the next one host=%s status=%d retry_after=%s",
                    target.netloc,
                    reply.status,
                    config.format_duration(_rounded(wait)),
                )
                if best is None or sooner(reply, best):
                    if best is not None:
                        best.close()
                    best = reply
                else:
                    reply.close()
                if not replayable:
                    return best
                continue

            c.serving(entry, target)
            if best is not None:
                best.close()
            # Which host answered is the one thing a router must be able to say
            # out loud, because "everything is going to one host" is otherwise
            # invisible.
            c.log.info("host took the request host=%s", target.netloc)
            return reply

        if best is not None:
            # Every host refused, so the caller gets the refusal that asked for
            # the shortest wait rather than whichever happened to be asked last.
            return best
        if unreachable is not None:
            # Nothing was reachable, which is a failure rather than a wait.
            raise unreachable
        raise c.no_host_error(time.time())

    def _refresh(self) -> List[registry.Entry]:
        """One fresh lookup, or nothing when it fails.

        A refresh that fails is not worth reporting: the request that triggered
        it is already going to be answered with the fleet it had, and the reason
        it failed is the same reason that request will fail.
        """
        try:
            return self.c.resolve()
        except BothyError:
            return []


def refusal(resp: _Upstream) -> Tuple[float, bool]:
    """Report whether a response is a host saying "not now" rather than an engine
    answering, and how long it asked to be left alone.

    429 is the host's own limits: full, too fast, or out of budget. 503 is the
    owner having paused. 502 is the host up with its engine unreachable, which is
    no more use to a caller than either. All three are reasons to ask somebody
    else instead of handing back an answer that was never produced.
    """
    if resp.status in (429, 503, 502):
        return retry_after(resp.headers), True
    return 0.0, False


def retry_after(headers) -> float:
    """Read the header, because a host that says when to come back is worth
    believing."""
    try:
        secs = int((headers.get("Retry-After") or "").strip())
    except (TypeError, ValueError):
        return ASSUMED_WAIT
    if secs < 0:
        return ASSUMED_WAIT
    return float(secs)


def sooner(a: _Upstream, b: _Upstream) -> bool:
    """Report whether a is the refusal a caller would rather be given: the one
    that asks for the shortest wait."""
    wa, wb = retry_after(a.headers), retry_after(b.headers)
    if wa != wb:
        return wa < wb
    return a.status < b.status


def read_replayable(body: Optional[Iterator[bytes]]) -> Tuple[bytes, Optional[Iterator[bytes]], bool]:
    """Hold a small body in memory so the request can be sent more than once, and
    otherwise hand back a reader that yields everything, including what was read
    while finding out.

    Go reads an `io.ReadCloser` and hands back an `io.MultiReader` of what it read
    in front of what is left. The body here is an iterator of chunks off the
    socket and `rest` is an iterator again, so the same thing is true: nothing
    that was read to find out is lost, and a body past the threshold is never
    held whole. `None` is a request with no body at all, which is replayable and
    has nothing to hold.

    The threshold is the test: a body that fits is held, and one that does not is
    streamed once. How much is read past the threshold is the chunk that crossed
    it, which is `httpx.CHUNK` at worst.
    """
    if body is None:
        return b"", None, True
    stream = iter(body)
    head = bytearray()
    for chunk in stream:
        head.extend(chunk)
        if len(head) > MAX_REPLAY_BODY:
            break
    if len(head) <= MAX_REPLAY_BODY:
        return bytes(head), None, True
    return b"", _chain(bytes(head), stream), False


def _chain(head: bytes, stream: Iterator[bytes]) -> Iterator[bytes]:
    """What was read, then what is left: Go's `io.MultiReader`."""
    yield head
    for chunk in stream:
        yield chunk


def content_length(headers) -> Optional[int]:
    """A declared content length, or nothing when the host did not declare one —
    which is how a streamed answer is told apart from a measured one."""
    raw = headers.get("Content-Length")
    if raw is None:
        return None
    try:
        return int(raw.strip())
    except (TypeError, ValueError):
        return None


def is_no_host(err: Exception) -> Optional[NoHostError]:
    """Report whether an error came from every host refusing, which the client
    answers with a 429 rather than a 502: one is "come back later", the other is
    "something is broken"."""
    return err if isinstance(err, NoHostError) else None


def _dial(target: urllib.parse.SplitResult) -> http.client.HTTPConnection:
    """A connection to one host, with the dial bounded and nothing else.

    Go's transport bounds the dial and then lets an answer take as long as it
    takes; a socket timeout that covered reading as well would cut off the very
    stream this client exists to pass through. So the connect is bounded here and
    the socket is handed back with no deadline on it.

    One connection per attempt, rather than Go's pooled transport: an attempt is
    to a host this client may not come back to, and a socket whose body was
    abandoned half-read cannot safely be handed to the next request. The cost is a
    handshake per request, paid to a peer on the same network as the GPU.
    """
    klass = http.client.HTTPSConnection if target.scheme == "https" else http.client.HTTPConnection
    conn = klass(target.hostname, target.port, timeout=DIAL_TIMEOUT)
    conn.connect()
    conn.sock.settimeout(None)
    return conn


def _path_of(req: httpx.Request, target: urllib.parse.SplitResult) -> str:
    """The path to ask this host for: the host's own prefix, then the caller's.

    A registry entry is an address with no path in it today, so this is the
    caller's path; joining them is what keeps the client the same shape as the
    proxy it replaces.
    """
    path = (target.path or "").rstrip("/") + req.path
    if not path:
        path = "/"
    query = urllib.parse.urlencode(req.query, doseq=True)
    return path + ("?" + query if query else "")


def _outgoing_headers(req: httpx.Request, share_key: str, held: bytes, replayable: bool, has_body: bool) -> Message:
    """The headers a host sees: the caller's, minus its credentials, plus the
    share key, and with a length that matches the body actually being sent.
    """
    # The caller's key never travels; the host gets the one it expects. Host is
    # dropped so the connection names the host being dialled, and Content-Length
    # so the body below is the one that is measured.
    pairs = httpx.end_to_end(req.headers, drop=("host", "content-length", "authorization", httpx.KEY_HEADER))
    out = _message(pairs)
    if share_key:
        out.add_header(httpx.KEY_HEADER, share_key)
    length = _attempt_length(req, held, replayable, has_body)
    if length is not None:
        out.add_header("Content-Length", str(length))
    return out


def _attempt_body(held: bytes, rest: Optional[Iterator[bytes]], replayable: bool, has_body: bool):
    """The body for one attempt: the held copy when there is one, and the stream
    of everything when there is not."""
    if replayable:
        return held if has_body else None
    return rest


def _attempt_length(req: httpx.Request, held: bytes, replayable: bool, has_body: bool) -> Optional[int]:
    """How long this attempt's body is, as far as it can be told.

    A held body is measured; a streamed one keeps the length the caller declared,
    and a chunked one has none to declare, so the request goes out chunked.
    """
    if replayable:
        return len(held) if has_body else None
    return req.content_length if req.content_length > 0 else None


def pick(models: List[model.Model], name: str) -> Tuple[Optional[model.Model], bool]:
    """Return the requested model, or the only one on offer when nothing was
    requested."""
    if not models:
        return None, False
    if name.strip() == "":
        return models[0], True
    for m in models:
        if model.matches(name, m.name):
            return m, True
    return None, False


def with_scheme(addr: str) -> str:
    if "://" in addr:
        return addr
    return "http://" + addr


def or_unknown(d: str) -> str:
    if (d or "").strip() == "":
        return "unknown"
    return d
