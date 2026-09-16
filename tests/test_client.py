"""The connect side: a local, OpenAI-compatible endpoint that is really somebody
else's GPU.

Four Go test files land here, because they were four views of one thing. The
client's own tests cover the digest rule, the status page and the two startup
behaviours that matter (a pinned digest nobody satisfies is fatal; a fleet that is
not up yet is not). The routing tests are about the property the whole fleet
exists for -- a refusal moving the request to the next host -- and the failover
tests are about a client that outlives the host it picked. The resolve tests are
about where the list of hosts comes from and what may rule one out before it is
ever asked.

Everything is driven through the client's real handler, and the hosts are real
servers on real ports, because the thing under test is a proxy: a test that
reached inside the transport would still pass while the wire was wrong.
"""

from __future__ import annotations

import http.client
import io
import json
import logging
import threading
import time
import unittest
import urllib.parse
from email.message import Message
from typing import Any, Dict, List, Optional, Tuple

from bothy import client, httpx, model, registry
from bothy.errors import BothyError, ConfigError

DIGEST_A = "sha256:aaaa"
DIGEST_B = "sha256:bbbb"

MODEL = "llama3.1:8b"

# Nothing in a passing run should reach stderr.
_log = logging.getLogger("bothy.tests.client")
_log.addHandler(logging.NullHandler())


def _quiet() -> logging.Logger:
    return _log


def _captured() -> Tuple[logging.Logger, io.StringIO]:
    """A logger that keeps what it was told, for the tests about what the client
    says when it cannot do something."""
    buf = io.StringIO()
    log = logging.getLogger("bothy.tests.client.capture")
    handler = logging.StreamHandler(buf)
    log.addHandler(handler)
    log.propagate = False
    log.setLevel(logging.INFO)
    return log, buf


def _message(*pairs: Tuple[str, str]) -> Message:
    """A case-insensitive header bag, the shape `Request.headers` really is."""
    m = Message()
    for key, value in pairs:
        m.add_header(key, value)
    return m


class _Sink(io.BytesIO):
    """A body that remembers how often it was flushed, so a streamed answer that
    sat in a buffer can be told from one that was written out."""

    def __init__(self) -> None:
        super().__init__()
        self.flushes = 0

    def flush(self):
        self.flushes += 1
        super().flush()


class _Recorder:
    """The stdlib handler surface that `Response` writes through.

    Stands in for `BaseHTTPRequestHandler` so the client's handler can be driven
    without a socket, the way the Go tests use `httptest.NewRecorder`.
    """

    def __init__(self) -> None:
        self.status = 0
        self.headers: List[Tuple[str, str]] = []
        self.wfile = _Sink()
        self.close_connection = False

    def send_response_only(self, code, message=None):
        self.status = code

    def send_header(self, keyword, value):
        self.headers.append((keyword, str(value)))

    def end_headers(self):
        pass

    def header(self, name: str) -> Optional[str]:
        for key, value in self.headers:
            if key.lower() == name.lower():
                return value
        return None

    def body(self) -> bytes:
        return self.wfile.getvalue()


def _ask(
    c: client.Client,
    body: str = "",
    method: str = "POST",
    path: str = "/v1/chat/completions",
    headers: Tuple[Tuple[str, str], ...] = (),
) -> _Recorder:
    """Send one request through the client's local endpoint, which is the whole
    path a real tool takes: handler, forward, proxy, transport."""
    raw = body.encode("utf-8")
    h = _message(*headers)
    if raw:
        h["Content-Length"] = str(len(raw))
    req = httpx.Request(method, path, {}, h, io.BytesIO(raw), "127.0.0.1", "HTTP/1.1")
    rec = _Recorder()
    c.handler()(req, httpx.Response(req, rec))
    return rec


def _call(addr: str, path: str = "/v1/chat/completions", method: str = "GET", body: bytes = b"", timeout: float = 5.0):
    """One request to a real client over the wire, from a real socket."""
    conn = http.client.HTTPConnection(*httpx.split_addr(addr), timeout=timeout)
    try:
        headers = {"Content-Length": str(len(body))} if body else {}
        conn.request(method, path, body=body or None, headers=headers)
        resp = conn.getresponse()
        return resp.status, resp.read(), resp
    finally:
        conn.close()


class _FakeHost:
    """A host that records what it was asked, so a test can tell not only who
    answered but who was never asked.
    """

    def __init__(self, reply) -> None:
        self._reply = reply
        self._mu = threading.Lock()
        self.requests = 0
        self.keys: List[str] = []
        self.auths: List[str] = []
        self.paths: List[str] = []
        self.bodies: List[str] = []
        self._server = httpx.Server("127.0.0.1:0", self._dispatch, _log)
        self._server.start()

    @property
    def url(self) -> str:
        return "http://" + self._server.addr

    def addr(self) -> str:
        return self._server.addr

    def _dispatch(self, req: httpx.Request, resp: httpx.Response) -> None:
        body = req.body_bytes()
        with self._mu:
            self.requests += 1
            self.keys.append(req.headers.get(httpx.KEY_HEADER) or "")
            self.auths.append(req.headers.get("Authorization") or "")
            self.paths.append(req.path)
            self.bodies.append(body.decode("utf-8", "replace"))
        self._reply(req, resp)

    def count(self) -> int:
        with self._mu:
            return self.requests

    def saw(self):
        with self._mu:
            return list(self.keys), list(self.auths), list(self.paths), list(self.bodies)

    def close(self) -> None:
        self._server.shutdown()


def _serving(marker: str):
    """Answer like an engine that produced something."""

    def reply(_req, resp: httpx.Response) -> None:
        resp.json(200, {"marker": marker})

    return reply


def _refusing(status: int, retry_after: str = ""):
    """Answer like a host whose limits said no, with the header the client is
    supposed to believe."""

    def reply(_req, resp: httpx.Response) -> None:
        headers = [("Retry-After", retry_after)] if retry_after else []
        resp.send_bytes(status, b"", headers=headers)

    return reply


class _Registry:
    """A registry that publishes the entries it was given, in the order it was
    given them, and counts how often it was asked."""

    def __init__(self, entries: List[Dict[str, Any]]) -> None:
        self.entries = entries
        self.asked = 0
        self._server = httpx.Server("127.0.0.1:0", self._dispatch, _log)
        self._server.start()

    @property
    def url(self) -> str:
        return "http://" + self._server.addr

    def _dispatch(self, _req, resp: httpx.Response) -> None:
        self.asked += 1
        resp.json(200, {"entries": self.entries})

    def close(self) -> None:
        self._server.shutdown()


class _ClientCase(unittest.TestCase):
    """A test case that can put a registry and fake hosts on real ports."""

    def host(self, reply) -> _FakeHost:
        h = _FakeHost(reply)
        self.addCleanup(h.close)
        return h

    def registry(self, entries: List[Dict[str, Any]]) -> _Registry:
        r = _Registry(entries)
        self.addCleanup(r.close)
        return r

    def registry_of(self, *addresses: str) -> str:
        """Publish the given addresses as hosts for one model."""
        r = self.registry([{"model": "m", "digest": DIGEST_A, "address": a} for a in addresses])
        return r.url

    def models_host(self, models: List[model.Model]) -> str:
        """A host that answers /bothy/models, which is the route a client pointed
        straight at an address reads to learn what it can verify."""

        def reply(req, resp: httpx.Response) -> None:
            if req.path != "/bothy/models":
                resp.error(404, "no such route: %s" % req.path)
                return
            resp.json(200, {"models": model.models_to_json(models)})

        return "http://" + self.host(reply).addr()


# --- the digest rule --------------------------------------------------------


class TestVerify(unittest.TestCase):
    def test_verify(self):
        for name, expected, actual, want_error in [
            ("no expectation accepts anything", "", DIGEST_B, False),
            ("no expectation accepts an unknown digest", "", "", False),
            ("matching digest", DIGEST_A, DIGEST_A, False),
            ("matching digest ignores case and prefix", "AAAA", DIGEST_A, False),
            ("different weights are refused", DIGEST_A, DIGEST_B, True),
            ("unknown digest cannot satisfy a requirement", DIGEST_A, "", True),
        ]:
            with self.subTest(name):
                if want_error:
                    with self.assertRaises(client.MismatchError):
                        client.verify(expected, actual, MODEL)
                else:
                    client.verify(expected, actual, MODEL)


class TestVerifyReturnsMismatchForFatalHandling(unittest.TestCase):
    # The client treats a mismatch as fatal, so it has to be a distinguishable
    # type rather than an anonymous error string.
    def test_a_mismatch_is_its_own_type(self):
        err = None
        try:
            client.verify(DIGEST_A, DIGEST_B, MODEL)
        except client.MismatchError as caught:
            err = caught
        self.assertIsNotNone(err, "want a MismatchError so the refusal is legible at startup")
        self.assertEqual(err.expected, DIGEST_A)
        self.assertEqual(err.actual, DIGEST_B)


class TestPickPrefersRequestedModel(unittest.TestCase):
    def test_pick(self):
        models = [
            model.Model(name="qwen2.5:7b", digest=DIGEST_B),
            model.Model(name="llama3.1", digest=DIGEST_A),
        ]
        found, ok = client.pick(models, "llama3.1:latest")
        self.assertTrue(ok, "pick should match llama3.1 as :latest")
        self.assertEqual(found.digest, DIGEST_A)

        found, ok = client.pick(models, "")
        self.assertTrue(ok)
        self.assertEqual(found.name, "qwen2.5:7b", "pick with no name should take the first model")

        self.assertFalse(client.pick(models, "mistral")[1], "pick should not invent a model that is not offered")
        self.assertFalse(client.pick([], "llama3.1")[1], "pick should fail on an empty list")


class TestWithScheme(unittest.TestCase):
    def test_with_scheme(self):
        self.assertEqual(client.with_scheme("box:7777"), "http://box:7777")
        self.assertEqual(client.with_scheme("https://box:7777"), "https://box:7777", "with_scheme rewrote an explicit scheme")


class TestOrUnknown(unittest.TestCase):
    def test_or_unknown(self):
        self.assertEqual(client.or_unknown("  "), "unknown")
        self.assertEqual(client.or_unknown(DIGEST_A), DIGEST_A, "want it unchanged")


class TestMismatchErrorSaysWhatIsWrong(unittest.TestCase):
    # Both mismatch sentences are read by someone deciding whether their setup is
    # wrong, so each has to name the model and say the useful thing.
    def test_the_sentence_names_the_model_and_both_digests(self):
        try:
            client.verify(DIGEST_A, DIGEST_B, MODEL)
        except client.MismatchError as err:
            msg = str(err)
        else:
            self.fail("a mismatch was accepted")
        for want in (MODEL, DIGEST_A, DIGEST_B, "mismatch"):
            self.assertIn(want, msg)

        try:
            client.verify(DIGEST_A, "", MODEL)
        except client.MismatchError as err:
            unknown = str(err)
        else:
            self.fail("a host with no digest satisfied a requirement")
        for want in (MODEL, DIGEST_A, "no digest"):
            self.assertIn(want, unknown)
        # The two cases have to be tellable apart: "different weights" is a host
        # problem, "no digest at all" is usually an older engine.
        self.assertNotEqual(unknown, msg)


# --- the status page --------------------------------------------------------


class TestStatusDigestVerified(unittest.TestCase):
    # The status page is the first thing a new user curls after `connect`. When no
    # digest was required, the connection satisfies the policy, so it must not
    # report digest_verified:false on a healthy setup.
    def test_status_digest_verified(self):
        for name, expected, actual, want in [
            ("nothing required reads as verified", "", DIGEST_A, True),
            ("nothing required and nothing known reads as verified", "", "", True),
            ("matching digest reads as verified", DIGEST_A, DIGEST_A, True),
            ("different weights read as unverified", DIGEST_A, DIGEST_B, False),
        ]:
            with self.subTest(name):
                c = client.new(client.Config(expected_digest=expected), _quiet())
                c.target = urllib.parse.urlsplit("http://box:7777")
                c.entry = registry.Entry(model=MODEL, digest=actual)

                rec = _ask(c, method="GET", path="/bothy/status")
                status = json.loads(rec.body())
                self.assertEqual(status["digest_verified"], want)


class TestStatusBeforeAnythingIsResolved(unittest.TestCase):
    def test_status_with_nothing_connected(self):
        c = client.new(
            client.Config(listen="127.0.0.1:11223", discovery_url="http://reg:8080", model=MODEL, expected_digest=DIGEST_A),
            _quiet(),
        )
        rec = _ask(c, method="GET", path="/bothy/status")
        status = json.loads(rec.body())

        self.assertFalse(status["connected"], "connected = %r, want false" % (status["connected"],))
        for key in ("listening", "discovery", "requested_model", "expected_digest"):
            self.assertIn(key, status)
        # Nothing is connected, so there is no host to describe -- and definitely
        # no digest_verified:true.
        for key in ("host", "digest", "digest_verified"):
            self.assertNotIn(key, status, "status claims %r while nothing is connected" % (key,))


class TestTheStatusPageExplainsWhoIsBeingSkipped(_ClientCase):
    # "Why is every request going to one host?" is otherwise unanswerable, so the
    # answer -- a host that refused, and for how long -- has to be on the page.
    def test_skipped_hosts_are_reported_with_a_reason(self):
        patient = self.host(_refusing(429, "600"))
        hurried = self.host(_refusing(429, "3"))
        c = client.new(client.Config(discovery_url=self.registry_of(patient.addr(), hurried.addr()), model="m"), _quiet())
        _ask(c, '{"model":"m"}')

        rows = {row["address"]: row for row in c.host_report()}
        self.assertEqual(set(rows), {patient.addr(), hurried.addr()})
        self.assertEqual(rows[patient.addr()]["reason"], "refused")
        self.assertEqual(rows[hurried.addr()]["reason"], "refused")
        # An hour was asked for; the client caps it, because taking it as gospel
        # would leave a one-host client looking broken for that hour.
        self.assertEqual(rows[patient.addr()]["skipped_for"], "5m0s")
        self.assertEqual(rows[hurried.addr()]["skipped_for"], "3s")

    def test_an_unreachable_host_is_named_as_such(self):
        dead = self.host(_serving("never reached"))
        addr = dead.addr()
        dead.close()
        c = client.new(client.Config(discovery_url=self.registry_of(addr), model="m"), _quiet())
        _ask(c, '{"model":"m"}')

        row = c.host_report()[0]
        self.assertEqual(row["address"], addr)
        self.assertEqual(row["reason"], "unreachable", "want the failure told apart from a refusal")


# --- startup ----------------------------------------------------------------


class TestRunRefusesAPinnedDigestTheHostCannotSatisfy(_ClientCase):
    # A pinned digest the host cannot satisfy is a configuration error, not a
    # transient one: no amount of waiting will fix it. A client that started
    # anyway would serve an endpoint whose every request fails, with the reason
    # arriving after the user has already pointed an editor at it.
    def test_run_fails_the_process(self):
        srv = self.models_host([model.Model(name=MODEL, digest=DIGEST_B)])

        with self.assertRaises(client.MismatchError) as caught:
            client.run(None, _quiet(), ["-host", srv, "-model", MODEL, "-expected-digest", DIGEST_A, "-listen", "127.0.0.1:0"])
        self.assertIn(MODEL, str(caught.exception), "the refusal has to name the model it could not satisfy")


class TestRunServesWhenNothingIsReachableYet(unittest.TestCase):
    # The other half of that rule: nothing being reachable yet must not stop the
    # client from starting. Compose brings services up in whatever order it likes,
    # so a client that exited because its host had not booted would be the most
    # annoying possible failure.
    def test_service_starts_before_its_fleet_does(self):
        for name, args, why in [
            ("the host is not up yet", ["-host", "127.0.0.1:1"], "a direct host that cannot be read yet is not fatal"),
            ("the registry is not up yet", ["-discovery-url", "http://127.0.0.1:1"], "a fleet that has not started is not fatal"),
        ]:
            with self.subTest(name):
                ctx = threading.Event()
                outcome: Dict[str, Any] = {}

                def serve():
                    try:
                        client.run(ctx, _quiet(), args + ["-model", MODEL, "-listen", "127.0.0.1:0"])
                    except BaseException as err:  # reported by the assertions below, not swallowed
                        outcome["error"] = err
                    outcome["returned"] = True

                thread = threading.Thread(target=serve, daemon=True)
                thread.start()
                time.sleep(0.3)
                self.assertFalse(outcome.get("returned"), "run returned %r instead of serving: %s" % (outcome.get("error"), why))

                ctx.set()
                thread.join(8)
                self.assertFalse(thread.is_alive(), "run did not shut down within 8s of being asked to")
                self.assertIsNone(outcome.get("error"), "run returned %r after a graceful cancel" % (outcome.get("error"),))


# --- routing: who is asked, and who is asked next ---------------------------


class TestARefusalMovesTheRequestAlong(_ClientCase):
    # The gap this closes: a 429 is a *successful* response, so it used to be
    # relayed to the caller and the next host was never asked. A host saying
    # "full" was the end of the request rather than the start of a search.
    def test_the_next_host_answers(self):
        full = self.host(_refusing(429, "5"))
        free = self.host(_serving("second"))

        c = client.new(client.Config(discovery_url=self.registry_of(full.addr(), free.addr()), model="m"), _quiet())
        rec = _ask(c, '{"model":"m"}')

        self.assertEqual(rec.status, 200, "want the second host to answer: %s" % rec.body().decode())
        self.assertIn(b"second", rec.body(), "the answer did not come from the host that had room")
        self.assertEqual(full.count(), 1, "the full host was asked %d times, want exactly 1" % full.count())
        self.assertEqual(free.count(), 1, "the host with room was asked %d times, want 1" % free.count())


class TestAHostWhoseEngineIsUnreachableIsSkipped(_ClientCase):
    # A host that is up but whose engine is not is no more use than a full one.
    def test_a_502_from_a_host_moves_the_request_along(self):
        broken = self.host(_refusing(502))
        working = self.host(_serving("alive"))

        c = client.new(client.Config(discovery_url=self.registry_of(broken.addr(), working.addr()), model="m"), _quiet())
        rec = _ask(c, '{"model":"m"}')

        self.assertEqual(rec.status, 200, rec.body().decode())
        self.assertIn(b"alive", rec.body(), "want the working host to answer")
        self.assertEqual(working.count(), 1)


class TestEveryHostRefusingGivesTheCallerTheShortestWait(_ClientCase):
    def test_the_shortest_wait_wins(self):
        patient = self.host(_refusing(429, "600"))
        hurried = self.host(_refusing(429, "3"))

        c = client.new(client.Config(discovery_url=self.registry_of(patient.addr(), hurried.addr()), model="m"), _quiet())
        rec = _ask(c, '{"model":"m"}')

        self.assertEqual(rec.status, 429, "want the refusal relayed as a 429")
        self.assertEqual(rec.header("Retry-After"), "3", "want the shortest wait on offer")
        self.assertEqual(patient.count() + hurried.count(), 2, "want both hosts tried once")

        # The next request has nothing left to ask, and the client answers in its
        # own words: the wait it names is the shortest one any host asked for, and
        # the sentence says whose it is.
        second = _ask(c, '{"model":"m"}')
        self.assertEqual(second.status, 429)
        self.assertEqual(second.header("Retry-After"), "3", "the shortest wait is the one the caller is sent away with")
        body = second.body().decode()
        self.assertIn(hurried.addr(), body, "a refusal with no subject is not something a person can act on")
        self.assertIn("3s", body)


class TestARefusedHostIsLeftAloneUntilItAsked(_ClientCase):
    # A host that said "retry in an hour" should not be asked again immediately,
    # or the client is as annoying as the caller would have been.
    def test_a_refused_host_is_not_asked_again(self):
        full = self.host(_refusing(429, "60"))
        free = self.host(_serving("second"))

        c = client.new(client.Config(discovery_url=self.registry_of(full.addr(), free.addr()), model="m"), _quiet())
        for i in range(3):
            rec = _ask(c, '{"model":"m"}')
            self.assertEqual(rec.status, 200, "request %d: status = %d" % (i + 1, rec.status))

        self.assertEqual(full.count(), 1, "the refused host was asked %d times, want 1 -- it asked to be left alone" % full.count())
        self.assertEqual(free.count(), 3, "the host with room was asked %d times, want every request" % free.count())


class TestOneRefusingHostIsReportedToTheCaller(_ClientCase):
    # With a single host, a refusal is the answer rather than a reason to hide it
    # -- and the caller is then left alone until the host asked to be tried
    # again, rather than the client hammering a host that already said no.
    def test_the_only_host_refusal_is_relayed_then_become_a_wait(self):
        only = self.host(_refusing(503))

        c = client.new(client.Config(discovery_url=self.registry_of(only.addr()), model="m"), _quiet())
        first = _ask(c, '{"model":"m"}')
        self.assertEqual(first.status, 503, "want the host's own refusal relayed")

        second = _ask(c, '{"model":"m"}')
        self.assertEqual(second.status, 429, "want a 429 telling the caller to come back")
        self.assertTrue(second.header("Retry-After"), "a refusal with a wait on it needs a Retry-After, or it reads as never")
        self.assertEqual(only.count(), 1, "the only host was asked %d times for 2 requests; it said not now" % only.count())


class TestTheShareKeyReplacesTheCallersCredential(_ClientCase):
    # The caller's own credential must not travel to a stranger's engine -- and
    # which host it travels to is now decided per attempt, so it is tested here
    # rather than in a director that runs once.
    def test_the_host_sees_the_share_key_only(self):
        host = self.host(_serving("ok"))

        c = client.new(client.Config(discovery_url=self.registry_of(host.addr()), model="m", share_key="peer-key"), _quiet())
        rec = _ask(
            c,
            '{"model":"m"}',
            headers=((httpx.KEY_HEADER, "local-api-key"), ("Authorization", "Bearer local-api-key")),
        )
        self.assertEqual(rec.status, 200, rec.body().decode())

        keys, auths, paths, bodies = host.saw()
        self.assertEqual(keys, ["peer-key"], "the host saw keys %r, want the share key" % (keys,))
        self.assertEqual(auths, [""], "the host saw Authorization %r, want the caller's credential dropped" % (auths,))
        self.assertEqual(paths, ["/v1/chat/completions"], "the host saw paths %r, want the path the caller asked for" % (paths,))
        self.assertEqual(len(bodies), 1)
        self.assertIn('"model":"m"', bodies[0], "the host saw body %r, want the caller's request" % (bodies[0],))


class TestABodyTooBigToReplayIsSentOnce(_ClientCase):
    # A body too big to hold in memory is sent once: buffering megabytes to make a
    # retry possible would cost more than the retry is worth. The caller still
    # gets the refusal rather than a hang.
    def test_a_large_body_is_streamed_and_not_retried(self):
        full = self.host(_refusing(429, "10"))
        free = self.host(_serving("second"))

        c = client.new(client.Config(discovery_url=self.registry_of(full.addr(), free.addr()), model="m"), _quiet())
        big = '{"model":"m","prompt":"' + "x" * (client.MAX_REPLAY_BODY + 1) + '"}'
        rec = _ask(c, big)

        self.assertEqual(rec.status, 429, "want the refusal")
        self.assertEqual(free.count(), 0, "the second host was asked %d times; a body this large is not replayed" % free.count())


class TestReadReplayableYieldsWhatItRead(unittest.TestCase):
    # The contract the big-body path rests on: finding out that a body is too
    # large must not lose the part that was read to find out.
    def test_a_small_body_is_held_and_replayable(self):
        held, rest, replayable = client.read_replayable(iter([b"abc", b"def"]))
        self.assertEqual(held, b"abcdef")
        self.assertIsNone(rest)
        self.assertTrue(replayable)

    def test_a_large_body_is_yielded_whole(self):
        chunks = [b"x" * client.MAX_REPLAY_BODY, b"y" * 8]
        held, rest, replayable = client.read_replayable(iter(chunks))
        self.assertEqual(held, b"", "a body this large is not held")
        self.assertFalse(replayable)
        self.assertEqual(b"".join(rest), b"".join(chunks), "everything read while finding out has to come back out")

    def test_no_body_is_replayable_and_empty(self):
        self.assertEqual(client.read_replayable(None), (b"", None, True))


class TestNoHostsAtAllIsNotARefusal(_ClientCase):
    # Nothing to route to is not the same failure as everybody refusing, and the
    # difference is visible to the caller.
    def test_nobody_offering_the_model_is_a_502(self):
        c = client.new(client.Config(discovery_url=self.registry([]).url, model="m"), _quiet())
        rec = _ask(c, '{"model":"m"}')

        self.assertEqual(rec.status, 502, "nobody offering a model is not a busy fleet")
        self.assertIn(b"no host is offering", rec.body())


class TestTheCandidateListRotates(_ClientCase):
    # The list carries on from where the last request stopped rather than starting
    # at the top, so a fleet takes turns instead of every client aiming at the
    # same host.
    def test_requests_take_turns(self):
        first = self.host(_serving("A"))
        second = self.host(_serving("B"))

        c = client.new(client.Config(discovery_url=self.registry_of(first.addr(), second.addr()), model="m"), _quiet())
        answers = [_ask(c, '{"model":"m"}').body() for _ in range(3)]

        self.assertIn(b'"A"', answers[0])
        self.assertIn(b'"B"', answers[1], "the second request should go to the host the first one did not use")
        self.assertIn(b'"A"', answers[2], "and then round again")
        self.assertEqual((first.count(), second.count()), (2, 1))


class TestASkippedHostComesBackWhenItsWaitIsOver(_ClientCase):
    def test_the_skip_window_expires(self):
        host = self.host(_serving("ok"))
        c = client.new(client.Config(discovery_url=self.registry_of(host.addr()), model="m"), _quiet())
        c.ensure_candidates()

        entry, _target, ok = c.choose(1000.0)
        self.assertTrue(ok)
        c.skip(entry, 1000.0, 60.0, True)

        self.assertFalse(c.choose(1000.0)[2], "a host that asked for 60s is not asked again immediately")
        self.assertFalse(c.choose(1059.9)[2], "nor any time inside the window it named")
        self.assertTrue(c.choose(1060.0)[2], "at the moment it named, it is asked again")


class TestAnUnreachableFleetIsAFailureRatherThanAWait(_ClientCase):
    # Waiting for a fleet that is not up means waiting forever, so nothing
    # reachable is a 502 rather than a Retry-After the caller would believe.
    def test_an_unreachable_fleet_is_a_502(self):
        dead_a = self.host(_serving("a"))
        dead_b = self.host(_serving("b"))
        addr_a, addr_b = dead_a.addr(), dead_b.addr()
        dead_a.close()
        dead_b.close()

        c = client.new(client.Config(discovery_url=self.registry_of(addr_a, addr_b), model="m"), _quiet())
        first = _ask(c, '{"model":"m"}')
        self.assertEqual(first.status, 502, first.body().decode())

        second = _ask(c, '{"model":"m"}')
        self.assertEqual(second.status, 502, second.body().decode())
        self.assertIn(b"no host could be reached", second.body(), "the fleet, not the hosts, is what failed")
        self.assertIsNone(second.header("Retry-After"), "a broken fleet is not something to wait for")


# --- resolve -----------------------------------------------------------------


class TestFetchModelsReadsTheHostsListWithTheShareKey(_ClientCase):
    def test_the_share_key_is_presented(self):
        host = self.host(lambda _req, resp: resp.json(200, {"models": [{"name": MODEL, "digest": DIGEST_A}]}))

        c = client.new(client.Config(share_key="peer-key"), _quiet())
        models = c.fetch_models(host.url)

        self.assertEqual(models, [model.Model(name=MODEL, digest=DIGEST_A)])
        keys, _, paths, _ = host.saw()
        self.assertEqual(keys, ["peer-key"], "want the share key the host expects")
        self.assertEqual(paths, ["/bothy/models"])


class TestFetchModelsReportsWhatWentWrong(_ClientCase):
    def test_a_refusal(self):
        def reply(_req, resp):
            resp.error(401, "nope")

        c = client.new(client.Config(), _quiet())
        with self.assertRaises(BothyError) as caught:
            c.fetch_models(self.host(reply).url)
        self.assertIn("/bothy/models", str(caught.exception), "the error should say which route failed")

    def test_a_body_that_is_not_the_documented_shape(self):
        c = client.new(client.Config(), _quiet())
        with self.assertRaises(BothyError):
            c.fetch_models(self.host(lambda _req, resp: resp.send_bytes(200, b'{"models":"nope"}', content_type="application/json")).url)

    def test_a_host_that_is_not_there(self):
        host = self.host(_serving("x"))
        url = host.url
        host.close()
        c = client.new(client.Config(), _quiet())
        with self.assertRaises(BothyError):
            c.fetch_models(url)


class TestResolveAgainstADirectHostLearnsTheDigest(_ClientCase):
    # A direct address is the two-friend setup, and it still ends up with a
    # verifiable digest -- without discovery being involved at all.
    def test_the_offered_model_and_digest_are_recorded(self):
        srv = self.models_host([model.Model(name="qwen2.5:7b", digest=DIGEST_B), model.Model(name=MODEL, digest=DIGEST_A)])

        entries = client.new(client.Config(host_address=srv, model=MODEL), _quiet()).resolve()
        self.assertEqual(len(entries), 1, "want the one host given directly: %r" % (entries,))
        self.assertEqual(entries[0].model, MODEL)
        self.assertEqual(entries[0].digest, DIGEST_A)
        self.assertTrue(entries[0].address, "the entry has no address to dial")


class TestResolveAgainstADirectHostWithNoModelTakesTheFirstOffered(_ClientCase):
    def test_the_only_model_is_taken(self):
        srv = self.models_host([model.Model(name="qwen2.5:7b", digest=DIGEST_B)])
        entries = client.new(client.Config(host_address=srv), _quiet()).resolve()
        self.assertEqual(len(entries), 1)
        self.assertEqual((entries[0].model, entries[0].digest), ("qwen2.5:7b", DIGEST_B))


class TestResolveKeepsGoingWhenTheHostsModelListCannotBeRead(_ClientCase):
    # A host that is up but silent about its models is still usable, because the
    # person pointed at it on purpose. What must not happen is a made-up digest:
    # the entry keeps the requested name and no digest, and the log says why.
    def test_the_entry_keeps_the_requested_name_and_no_digest(self):
        host = self.host(_serving("x"))
        addr = host.addr()
        host.close()

        log, buf = _captured()
        entries = client.new(client.Config(host_address=addr, model=MODEL), log).resolve()

        self.assertEqual(len(entries), 1, "want the host given directly")
        self.assertEqual(entries[0].model, MODEL, "want the requested model")
        self.assertEqual(entries[0].digest, "", "a digest nobody reported cannot be invented")
        self.assertIn("cannot read the host's model list", buf.getvalue())


class TestResolveReportsUsableFailures(_ClientCase):
    def test_nothing_to_connect_to(self):
        with self.assertRaises(BothyError) as caught:
            client.new(client.Config(), _quiet()).resolve()
        msg = str(caught.exception)
        self.assertIn("-host", msg)
        self.assertIn("-discovery-url", msg)

    def test_an_address_that_is_not_a_url(self):
        with self.assertRaises(BothyError) as caught:
            client.new(client.Config(host_address="http://[::1"), _quiet()).resolve()
        self.assertIn("http://[::1", str(caught.exception), "the error should quote the address")


class TestResolveReportsAnEmptyRegistry(_ClientCase):
    def test_an_empty_registry(self):
        url = self.registry([]).url
        with self.assertRaises(BothyError) as caught:
            client.new(client.Config(discovery_url=url), _quiet()).resolve()
        self.assertIn("no hosts are registered", str(caught.exception))

        with self.assertRaises(BothyError) as caught:
            client.new(client.Config(discovery_url=url, model=MODEL), _quiet()).resolve()
        self.assertIn(MODEL, str(caught.exception), "the error should name the model nobody has")


class TestResolvePropagatesARegistryFailure(_ClientCase):
    def test_a_failing_registry_is_not_an_empty_one(self):
        def reply(_req, resp):
            resp.error(503, "down")

        url = self.host(reply).url
        with self.assertRaises(BothyError):
            client.new(client.Config(discovery_url=url, model="m"), _quiet()).resolve()


class TestCandidatesAreResolvedOnceAndKept(_ClientCase):
    # The client holds the hosts it resolved -- that is what keeps a request from
    # re-looking-up on every call -- and asks again only when the list has been
    # thrown away, which is what the transport does when nothing is usable.
    def test_one_lookup_is_shared_by_every_request(self):
        r = self.registry([{"model": MODEL, "digest": DIGEST_A, "address": "box:7777"}])
        c = client.new(client.Config(discovery_url=r.url, model=MODEL), _quiet())

        for _ in range(3):
            c.ensure_candidates()
            entry, target, ok = c.choose(time.time())
            self.assertTrue(ok, "no host to choose")
            self.assertEqual(target.netloc, "box:7777")
            self.assertEqual(entry.model, MODEL)

        self.assertEqual(r.asked, 1, "the registry was asked %d times, want 1 while the list is held")

        # A second lookup is what a refresh costs, and it is only done when the
        # hosts in hand have all refused.
        c.resolve()
        self.assertEqual(r.asked, 2, "the registry was asked %d times, want 2 after a refresh" % r.asked)


class TestDigestPinSkipsHostsAndIsFatalOnlyWhenNobodyMatches(_ClientCase):
    # A pin that no host satisfies is fatal: it means nobody has the weights that
    # were asked for. A pin that *some* host satisfies is not -- the others are
    # simply skipped, which is what a fleet is for.
    def test_nobody_matches(self):
        entries = [{"model": MODEL, "digest": DIGEST_B, "address": "box:7777"}]
        c = client.new(
            client.Config(discovery_url=self.registry(entries).url, model=MODEL, expected_digest=DIGEST_A), _quiet()
        )
        with self.assertRaises(client.MismatchError):
            c.resolve()

    def test_one_of_them_matches(self):
        entries = [
            {"model": MODEL, "digest": DIGEST_B, "address": "wrong:7777"},
            {"model": MODEL, "digest": DIGEST_A, "address": "right:7777"},
        ]
        c = client.new(
            client.Config(discovery_url=self.registry(entries).url, model=MODEL, expected_digest=DIGEST_A), _quiet()
        )
        got = c.resolve()
        self.assertEqual(len(got), 1, "want only the host with the weights that were pinned: %r" % (got,))
        self.assertEqual(got[0].address, "right:7777")

    def test_an_unset_expectation_accepts_any_host(self):
        entries = [
            {"model": MODEL, "digest": DIGEST_B, "address": "one:7777"},
            {"model": MODEL, "digest": "", "address": "two:7777"},
        ]
        got = client.new(client.Config(discovery_url=self.registry(entries).url, model=MODEL), _quiet()).resolve()
        self.assertEqual(len(got), 2, "an empty expectation accepts anything: %r" % (got,))

    def test_a_host_with_no_digest_does_not_satisfy_a_requirement(self):
        # "unknown" must not read as "verified": a host that advertises nothing is
        # not a host that advertised the right thing.
        entries = [{"model": MODEL, "digest": "", "address": "one:7777"}]
        c = client.new(
            client.Config(discovery_url=self.registry(entries).url, model=MODEL, expected_digest=DIGEST_A), _quiet()
        )
        with self.assertRaises(client.MismatchError):
            c.resolve()


class TestAnUndialableEntryIsSkippedAndNamed(_ClientCase):
    # A registry is shared, and anyone can publish into it, so an entry whose
    # address cannot be dialled is skipped rather than fatal: one bad entry must
    # not take a client down. When it is the only entry, the failure names it
    # rather than pretending the fleet was busy.
    def test_an_undialable_entry(self):
        for name, address in [("no address at all", ""), ("an address that is not a URL", "http://[::1")]:
            with self.subTest(name):
                entries = [{"model": "m", "digest": "", "address": address}]
                c = client.new(client.Config(discovery_url=self.registry(entries).url), _quiet())
                c.ensure_candidates()

                self.assertFalse(c.choose(time.time())[2], "an entry with address %r was chosen to dial" % address)
                msg = str(c.no_host_error(time.time()))
                self.assertIn("no host could be dialled", msg)
                self.assertNotIn("busy", msg)
                self.assertNotIn("refused", msg, "the address is at fault, not the hosts")
                if address:
                    self.assertIn(address, msg, "the error should quote the address")


class TestResolveUsesTheRegistryOrder(_ClientCase):
    def test_the_order_is_kept_and_nothing_is_dialled(self):
        first = self.host(_serving("first"))
        second = self.host(_serving("second"))
        entries = [
            {"model": "m", "address": first.addr(), "digest": DIGEST_A, "free": 9},
            {"model": "m", "address": second.addr(), "digest": DIGEST_A, "free": 1},
        ]
        c = client.new(client.Config(discovery_url=self.registry(entries).url, model="m"), _quiet())
        got = c.resolve()

        self.assertEqual(len(got), 2, "want both hosts in the order the registry gave them: %r" % (got,))
        self.assertIn(first.addr(), got[0].address, "want the entry the registry put in front")
        self.assertEqual(first.count() + second.count(), 0, "resolving dialled the hosts; it should only list them")


# --- failover ---------------------------------------------------------------


class TestClientReResolvesAfterTheHostDisappears(_ClientCase):
    # PROTOCOL.md promises that "re-resolution happens automatically after an
    # upstream failure". That is the thing that keeps a client alive when the host
    # it picked goes to sleep mid-session -- a normal event on a network of home
    # machines.
    def test_a_dead_host_is_left_behind(self):
        first = self.host(_serving("host A"))
        second = self.host(_serving("host B"))

        # A registry that names A until A stops heartbeating, then names B, which
        # is what the real one does once an entry expires.
        def reply(_req, resp):
            address = first.addr() if asked["n"] == 1 else second.addr()
            asked["n"] += 1
            resp.json(200, {"entries": [{"model": MODEL, "digest": "sha256:1111", "address": address, "host": "somewhere", "free": 4}]})

        asked = {"n": 1}
        reg = self.host(reply)
        c = client.new(client.Config(discovery_url=reg.url, model=MODEL, share_key="k"), _quiet())
        srv = httpx.Server("127.0.0.1:0", c.handler(), _quiet())
        srv.start()
        self.addCleanup(srv.shutdown)

        code, body, _ = _call(srv.addr)
        self.assertEqual(code, 200, body.decode())
        self.assertIn(b"host A", body, "want the first request to reach the host that was resolved")

        # The host goes to sleep.
        first.close()

        # This one is allowed to fail: the client is still holding the address it
        # resolved. A clear failure beats a hang, which is why it is asserted.
        code, body, _ = _call(srv.addr)
        self.assertEqual(code, 502, "a request to a dead host = %d %s, want 502" % (code, body.decode()))

        # And this is the promise. Without re-resolution the client would be
        # wedged on a dead address for the rest of its life.
        code, body, _ = _call(srv.addr)
        self.assertEqual(code, 200, "the client did not move on after the failure: %d %s" % (code, body.decode()))
        self.assertIn(b"host B", body)
        self.assertGreaterEqual(asked["n"], 2, "the registry was asked %d times, want a fresh lookup after the failure" % asked["n"])


class TestARequestWithNowhereToRouteFailsCleanly(_ClientCase):
    # A client is allowed to start with nothing to route to -- a fleet that is not
    # up yet comes up later, and the first request re-resolves. But a *request*
    # that arrives while there is still nowhere to go has to fail at once and say
    # why: anything else turns an editor's next keystroke into a hang that ends in
    # the editor's own timeout, with nothing anywhere saying what was missing.
    def test_the_failure_names_what_is_missing(self):
        for name, cfg, must_say in [
            ("no host and no registry", client.Config(model=MODEL), "-host"),
            ("a registry where nobody is offering this model", None, MODEL),
        ]:
            with self.subTest(name):
                if cfg is None:
                    cfg = client.Config(discovery_url=self.registry([]).url, model=MODEL)
                srv = httpx.Server("127.0.0.1:0", client.new(cfg, _quiet()).handler(), _quiet())
                srv.start()
                self.addCleanup(srv.shutdown)

                body = json.dumps({"model": MODEL, "messages": []}).encode()
                code, raw, _ = _call(srv.addr, method="POST", body=body)
                self.assertEqual(code, 502, "want 502 when there is nothing to route to: %s" % raw.decode())
                self.assertIn(must_say, raw.decode(), "the caller cannot tell what is missing")


class TestClientWithADirectAddressFailsCleanly(_ClientCase):
    # A client pointed straight at an address has no registry to re-resolve
    # against, so the failure has to stay a clear one rather than becoming a panic
    # or a hang.
    def test_a_dead_direct_host_is_a_502(self):
        host = self.host(_serving("never reached"))
        addr = host.addr()
        host.close()

        c = client.new(client.Config(host_address=addr, model=MODEL, share_key="k"), _quiet())
        srv = httpx.Server("127.0.0.1:0", c.handler(), _quiet())
        srv.start()
        self.addCleanup(srv.shutdown)

        code, body, _ = _call(srv.addr)
        self.assertEqual(code, 502, "want 502 for an unreachable host: %s" % body.decode())


# --- the answer itself ------------------------------------------------------


class TestAStreamedAnswerReachesTheCallerAsItIsProduced(_ClientCase):
    # A proxy that collects a stream to forward it has undone the reason to
    # stream, so the first frame has to reach the caller while the host is still
    # producing the second.
    def test_a_stream_is_not_buffered(self):
        produced: List[float] = []

        def reply(_req, resp: httpx.Response) -> None:
            def frames():
                produced.append(time.monotonic())
                yield b"one\n"
                # Long enough that a proxy which waited for the end of the stream
                # would be unmistakable rather than a race.
                time.sleep(1.0)
                produced.append(time.monotonic())
                yield b"two\n"

            resp.send_stream(200, frames(), content_type="text/plain")

        host = self.host(reply)
        c = client.new(client.Config(discovery_url=self.registry_of(host.addr()), model="m"), _quiet())
        srv = httpx.Server("127.0.0.1:0", c.handler(), _quiet())
        srv.start()
        self.addCleanup(srv.shutdown)

        conn = http.client.HTTPConnection(*httpx.split_addr(srv.addr), timeout=10)
        try:
            body = b'{"model":"m"}'
            conn.request("POST", "/v1/chat/completions", body=body, headers={"Content-Length": str(len(body))})
            resp = conn.getresponse()
            first = resp.read(1)
            at = time.monotonic()
            rest = resp.read()
        finally:
            conn.close()

        self.assertEqual(first, b"o", "want the first byte of the first frame")
        self.assertEqual(len(produced), 2, "the host should have produced its second frame by the end")
        self.assertLess(at, produced[1], "the caller waited for the whole stream before seeing any of it")
        self.assertIn(b"two", rest)


if __name__ == "__main__":
    unittest.main()
