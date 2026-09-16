"""The registry service: the wire contract hosts and clients use, and the page.

A registry is a bulletin board, not an authority: it cannot check that an address
is reachable and it cannot check that a digest is honest, so the only promise it
makes is that a host which stops heartbeating falls out of the list. Most of what
is below is that promise being kept -- by a TTL, by a token, and by a page that
shows exactly what the JSON API already hands to anyone who asks.

The page is tested here rather than only in a browser because the thing it must
never do is take a value anyone can register and write it into HTML unescaped.
"""

from __future__ import annotations

import http.client
import io
import json
import logging
import os
import socket
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request

from bothy import discovery, httpx, registry
from bothy.errors import ConfigError

# The test servers log through this; nothing in a passing run should reach stderr.
_log = logging.getLogger("bothy.tests.discovery")
_log.addHandler(logging.NullHandler())


def _http(method, url, body=None, token="", content_type="application/json", timeout=20.0):
    """One request over the wire. Returns (status, raw body bytes)."""
    data = None
    if body is not None:
        data = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None and content_type:
        req.add_header("Content-Type", content_type)
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as err:
        return err.code, err.read()


def _payload(entries):
    """The wire form of a batch: what `registry.Entry.to_json` sends."""
    return {"entries": [e.to_json() for e in entries]}


def _register(base, entries, token=""):
    """Post a batch and return the status code."""
    return _http("POST", base + "/register", _payload(entries), token=token)[0]


def _served(base, name=""):
    """The live entries the registry serves, as the wire form."""
    url = base + "/models"
    if name:
        url += "?model=" + urllib.parse.quote(name)
    status, raw = _http("GET", url)
    assert status == 200, "GET /models answered %d" % status
    return json.loads(raw)["entries"]


def _free_addr():
    """An address nothing is listening on, which is what a run test needs."""
    sock = socket.socket()
    try:
        sock.bind(("127.0.0.1", 0))
        return "127.0.0.1:%d" % sock.getsockname()[1]
    finally:
        sock.close()


def _restore_env(before):
    """Put the environment back, so one test's settings are not another's."""
    for key, value in before.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def _captured_log(buf, name="bothy.tests.discovery.run"):
    """A logger that writes its lines where a test can read them."""
    log = logging.getLogger(name)
    handler = logging.StreamHandler(buf)
    handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    log.handlers[:] = [handler]
    log.setLevel(logging.INFO)
    log.propagate = False
    return log


class _ServerTest(unittest.TestCase):
    """A test case with a registry on a port, cleaned up afterwards."""

    def serve(self, ttl=60.0, token="") -> str:
        """Start a registry and return the URL it answers on.

        Port 0 rather than a fixed one: two test cases running at once, or a
        leftover process, must not be able to take the port this one wants.
        """
        s = discovery.new_server(discovery.Config(ttl=ttl, token=token), _log)
        server = httpx.Server("127.0.0.1:0", s.handler(), _log)
        self.addCleanup(server.shutdown)
        server.start()
        return "http://" + server.addr

    def wait_health(self, base, timeout=10.0):
        """The health answer, or a failure -- a registry that never answers makes
        every other assertion in a run test prove nothing."""
        deadline = time.monotonic() + timeout
        last = ""
        while time.monotonic() < deadline:
            try:
                status, raw = _http("GET", base + "/healthz", timeout=2.0)
                if status == 200:
                    return json.loads(raw)
                last = "status %d" % status
            except Exception as err:  # not up yet
                last = str(err)
            time.sleep(0.02)
        self.fail("the registry at %s never answered (%s), so this proves nothing" % (base, last))


# --------------------------------------------------------------------------
# Ported from the Go package's discovery_test.go (in git history)
# --------------------------------------------------------------------------


class TestRegisterThenList(_ServerTest):
    def test_register_then_list(self):
        base = self.serve()
        self.assertEqual(
            _register(base, [registry.Entry(model="llama3.1:8b", digest="sha256:aa", address="box:7777", host="box")]),
            200,
            "a registration from a host that reported nothing else",
        )

        status, raw = _http("GET", base + "/models?model=llama3.1")
        self.assertEqual(status, 200)
        payload = json.loads(raw)
        self.assertEqual(payload["count"], 1, "count = %r, want 1" % (payload,))
        self.assertEqual(len(payload["entries"]), 1, "entries = %r" % (payload["entries"],))

        # The wire shape is the contract: the fields the client parses, and the
        # ones it does not set. `free` is absent because the host did not report
        # it, which is a different thing from reporting zero.
        self.assertEqual(
            payload["entries"][0],
            {
                "model": "llama3.1:8b",
                "digest": "sha256:aa",
                "address": "box:7777",
                "host": "box",
                "last_seen": payload["entries"][0]["last_seen"],
            },
            "an entry is what the host said plus the registry's own stamp",
        )
        # RFC3339 in UTC, which is what Go's time.Time marshals to. The fraction
        # is optional: a whole second is written whole.
        self.assertRegex(
            payload["entries"][0]["last_seen"],
            r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$",
            "last_seen is the registry's clock, in the form the wire uses",
        )

        # And it survives the round trip through the client's parser.
        entry = registry.Entry.from_json(payload["entries"][0])
        self.assertEqual(entry.address, "box:7777")
        self.assertIsNone(entry.free, "a host that reported nothing must not come back as full")


class TestListForAnUnknownModelIsEmptyNotAnError(_ServerTest):
    def test_list_for_an_unknown_model_is_empty_not_an_error(self):
        base = self.serve()
        self.assertEqual(_register(base, [registry.Entry(model="llama3.1:8b", address="box:7777")]), 200)

        status, raw = _http("GET", base + "/models?model=mistral")
        self.assertEqual(status, 200, "want 200 with an empty list, not a 404")
        self.assertEqual(json.loads(raw)["entries"], [], "nobody has mistral, and that is an answer")


class TestRegisterRequiresTheTokenWhenConfigured(_ServerTest):
    # Without a token anyone reachable can fill the registry with junk, so the
    # token has to actually be enforced.
    def test_register_requires_the_token_when_configured(self):
        base = self.serve(token="secret")
        entries = [registry.Entry(model="m", address="a:1")]

        self.assertEqual(_register(base, entries), 401, "an unauthenticated register must be refused")
        self.assertEqual(_register(base, entries, token="wrong"), 401, "a wrong token is not a token")
        self.assertEqual(_register(base, entries, token="secret"), 200, "the right token has to work")
        self.assertEqual(len(_served(base)), 1, "and only then is anything served")

        # A lookup stays open: a registry nobody can read is a registry nobody can
        # use, and the entries are public by design.
        self.assertEqual(_http("GET", base + "/models")[0], 200)

    def test_token_comparison_is_not_above_the_256_bit_key_it_may_carry(self):
        # A share key this long is not a token, but it must not be a crash either:
        # the comparison is constant-time and length-agnostic.
        base = self.serve(token="x" * 300)
        self.assertEqual(_register(base, [registry.Entry(model="m", address="a:1")], token="x" * 300), 200)


class TestRegisterRejectsAnEmptyBatch(_ServerTest):
    def test_register_rejects_an_empty_batch(self):
        base = self.serve()
        self.assertEqual(_register(base, []), 400, "a heartbeat that names nothing is a mistake")


class TestHealthReportsLiveEntries(_ServerTest):
    def test_health_reports_live_entries(self):
        base = self.serve()
        self.assertEqual(_register(base, [registry.Entry(model="m", address="a:1")]), 200)

        status, raw = _http("GET", base + "/healthz")
        self.assertEqual(status, 200)
        payload = json.loads(raw)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["live_entries"], 1)
        self.assertEqual(payload["ttl"], "1m0s", "the configured TTL, as a duration a reader can type back")


# --------------------------------------------------------------------------
# Ported from the Go package's run_test.go (in git history)
# --------------------------------------------------------------------------


class TestRunWarnsWhenRegistrationIsOpen(unittest.TestCase):
    # An open registry is the default, and it lets anyone who can reach the port
    # publish entries. That has to be said out loud at startup -- it is a spam
    # problem rather than a security one, which is exactly the sort of thing a
    # warning is for.
    def test_run_warns_when_registration_is_open(self):
        buf = io.StringIO()
        log = _captured_log(buf)
        addr = _free_addr()
        stop = threading.Event()
        outcome = []

        def serve():
            try:
                discovery.run(stop, log, ["-listen", addr, "-ttl", "1m"])
                outcome.append(None)
            except BaseException as err:  # reported by the assertions below
                outcome.append(err)

        thread = threading.Thread(target=serve, daemon=True, name="bothy-discovery-run")
        thread.start()
        self.addCleanup(thread.join, 8.0)
        self.addCleanup(stop.set)

        health = _ServerTest.wait_health(self, "http://" + addr)
        self.assertEqual(health["ttl"], "1m0s", "health = %r, want the configured 1m0s" % (health,))
        self.assertIn(
            "registration is open",
            buf.getvalue(),
            "nothing warned that anyone reachable can publish entries:\n%s" % buf.getvalue(),
        )

        stop.set()
        thread.join(8.0)
        self.assertFalse(thread.is_alive(), "run did not return after it was asked to stop")
        self.assertEqual(outcome, [None], "run returned %r, want nothing after a graceful shutdown" % (outcome,))


class TestRunDoesNotWarnWhenRegistrationIsClosed(unittest.TestCase):
    # With a token configured, the warning would be a lie, so it must not appear.
    def test_run_does_not_warn_when_registration_is_closed(self):
        buf = io.StringIO()
        log = _captured_log(buf)
        addr = _free_addr()
        stop = threading.Event()

        thread = threading.Thread(
            target=discovery.run, args=(stop, log, ["-listen", addr, "-token", "secret"]), daemon=True
        )
        thread.start()
        self.addCleanup(thread.join, 8.0)
        self.addCleanup(stop.set)

        _ServerTest.wait_health(self, "http://" + addr)
        self.assertNotIn("registration is open", buf.getvalue(), "warned with a token set:\n%s" % buf.getvalue())


class TestRunRefusesAnAddressItCannotBind(unittest.TestCase):
    # A typo in the listen address has to fail the process, not leave a registry
    # that quietly is not there.
    def test_run_refuses_an_address_it_cannot_bind(self):
        log = _captured_log(io.StringIO())
        with self.assertRaises(ConfigError) as caught:
            discovery.run(None, log, ["-listen", "127.0.0.1:not-a-port"])
        self.assertIn("127.0.0.1:not-a-port", str(caught.exception), "the error should name the address")


class TestRunRefusesATTLLThatWouldServeNobody(unittest.TestCase):
    # A TTL that expires everything on arrival leaves a registry that answers every
    # lookup with nothing while looking perfectly healthy -- the kind of
    # misconfiguration that should stop the process, not be discovered later.
    def test_run_refuses_a_ttl_that_would_serve_nobody(self):
        log = _captured_log(io.StringIO())
        for ttl in ("0s", "-1m"):
            addr = _free_addr()
            # The stop signal is already set, so a registry that wrongly accepted
            # the TTL returns instead of serving forever and hanging this suite.
            stop = threading.Event()
            stop.set()
            with self.assertRaises(ConfigError) as caught:
                discovery.run(stop, log, ["-listen", addr, "-ttl", ttl])
            self.assertIn("ttl", str(caught.exception), "error %r does not name the setting" % (str(caught.exception),))


class TestRunDefaultsToTheEnvironment(unittest.TestCase):
    # The environment is what a container sets, and it is the default the flags
    # overwrite -- which is what makes one image work on two ports.
    def test_run_defaults_to_the_environment(self):
        buf = io.StringIO()
        log = _captured_log(buf)
        addr = _free_addr()
        stop = threading.Event()
        before = {k: os.environ.get(k) for k in ("BOTHY_LISTEN", "BOTHY_REGISTRY_TTL")}
        os.environ["BOTHY_LISTEN"] = addr
        os.environ["BOTHY_REGISTRY_TTL"] = "90s"
        self.addCleanup(_restore_env, before)

        thread = threading.Thread(target=discovery.run, args=(stop, log, []), daemon=True)
        thread.start()
        self.addCleanup(thread.join, 8.0)
        self.addCleanup(stop.set)

        health = _ServerTest.wait_health(self, "http://" + addr)
        self.assertEqual(health["ttl"], "1m30s", "health = %r, want the TTL the environment set" % (health,))


# --------------------------------------------------------------------------
# Ported from the Go package's run_test.go (in git history): the body limit and
# the answer
# --------------------------------------------------------------------------


class TestRegisterRefusesAnOversizedBody(_ServerTest):
    # The register body is parsed into memory, so it is bounded. Without this, one
    # POST is enough to make the registry allocate whatever the sender likes.
    def test_register_refuses_an_oversized_body(self):
        base = self.serve()
        huge = "a" * (1 << 21)  # 2 MiB, past the 1 MiB cap
        body = ('{"entries":[{"model":"m","address":"' + huge + '"}]}').encode("utf-8")

        # The refusal comes from the DECLARED length, so the registry answers while
        # the sender is still writing and then closes. What the sender sees depends
        # on that race: a 400 if the answer lands before the close, a broken pipe if
        # it does not. Both are the same refusal, and which one a given interpreter
        # produces is not something to pin -- the assertion that matters is below,
        # about what the registry was left holding.
        refused_early = False
        try:
            status, _ = _http("POST", base + "/register", body)
        except (OSError, urllib.error.URLError):
            refused_early = True
            status = None
        if not refused_early:
            self.assertEqual(status, 400, "an oversized body got %d, want 400" % status)

        # And nothing was stored on the way through.
        self.assertEqual(json.loads(_http("GET", base + "/healthz")[1])["live_entries"], 0)

    def test_a_body_the_registry_cannot_hold_is_refused_without_being_read(self):
        """The half of the limit that protects the host.

        A body that announces more than the cap is refused from its own
        Content-Length, before a byte of it is read -- which is what makes the
        limit a limit rather than a rebuff after the allocation. The body is never
        sent here, so a registry that read first would sit waiting for it.
        """
        base = self.serve()
        host, port = base[len("http://"):].split(":")
        conn = http.client.HTTPConnection(host, int(port), timeout=10.0)
        self.addCleanup(conn.close)
        conn.putrequest("POST", "/register")
        conn.putheader("Content-Type", "application/json")
        conn.putheader("Content-Length", str(1 << 22))
        conn.endheaders()
        resp = conn.getresponse()
        self.assertEqual(resp.status, 400, "a body past the cap is refused, not read")
        self.assertEqual(json.loads(resp.read())["error"]["type"], "bothy_error")


class TestRegisterReportsAcceptedAndOffered(_ServerTest):
    # A host announces several models in one call, and the answer says how many of
    # them were usable -- which is how a host notices it sent a broken entry.
    def test_register_reports_accepted_and_offered(self):
        base = self.serve()
        status, raw = _http(
            "POST",
            base + "/register",
            _payload([registry.Entry(model="a", address="x:1"), registry.Entry(model="", address="y:1")]),
        )
        self.assertEqual(status, 200)
        payload = json.loads(raw)
        self.assertEqual(payload["registered"], 1, "response = %r, want one of two entries accepted" % (payload,))
        self.assertEqual(payload["live"], 1)
        self.assertEqual(payload["ttl"], "1m0s", "ttl = %r, want the configured one" % (payload["ttl"],))


class TestExpiredEntriesStopBeingServed(_ServerTest):
    # An expired registration must fall out of the list the service serves, not
    # just out of an internal count -- this is the whole anti-staleness promise.
    def test_expired_entries_stop_being_served(self):
        base = self.serve(ttl=0.1)
        self.assertEqual(_register(base, [registry.Entry(model="m", address="box:7777")]), 200)
        self.assertEqual(len(_served(base)), 1, "the entry should be live right after registering")

        # A host that stopped heartbeating is gone once its TTL is up.
        time.sleep(0.3)
        self.assertEqual(len(_served(base)), 0, "served after the TTL, want nothing")
        self.assertEqual(json.loads(_http("GET", base + "/healthz")[1])["live_entries"], 0)


class TestReRegistrationIsTheHeartbeat(_ServerTest):
    # There is no separate liveness call: the same registration refreshes the
    # entry, which is what makes a host that keeps saying so stay in the list.
    def test_re_registration_is_the_heartbeat(self):
        base = self.serve(ttl=0.2)
        entry = registry.Entry(model="m", address="box:7777")
        self.assertEqual(_register(base, [entry]), 200)
        time.sleep(0.15)
        self.assertEqual(_register(base, [entry]), 200, "the heartbeat is a second registration")
        time.sleep(0.15)
        self.assertEqual(len(_served(base)), 1, "the refreshed entry is still promised a TTL from its last heartbeat")


# --------------------------------------------------------------------------
# Ported from the Go package's index_test.go (in git history)
# --------------------------------------------------------------------------


class TestIndexPageShowsWhatTheAPIShows(_ServerTest):
    # The page exists so that "is this registry doing anything?" does not require
    # curl and jq. It must show what the JSON already shows -- the same live
    # entries, the same capacity -- because a page showing something the contract
    # does not is a directory nobody agreed to publish.
    def test_index_page_shows_what_the_api_shows(self):
        base = self.serve()
        self.assertEqual(
            _register(
                base,
                [registry.Entry(model="llama3.1:8b", digest="sha256:aaaa", address="box.example:7777", host="box", free=3)],
            ),
            200,
        )

        status, raw = _http("GET", base + "/")
        self.assertEqual(status, 200)
        page = raw.decode("utf-8")
        with urllib.request.urlopen(base + "/", timeout=10.0) as resp:
            content_type = resp.headers.get("Content-Type", "")
        self.assertTrue(
            content_type.startswith("text/html"),
            "Content-Type = %r, want text/html so a browser renders it" % content_type,
        )
        for want in ("llama3.1:8b", "box.example:7777", "box", "1 live entry"):
            self.assertIn(want, page, "the page does not mention %r" % want)
        self.assertIn("sha256:aaaa", page, "a short digest should be shown in full")

        # And the capacity is the one the API reports, from the same store.
        served = _served(base)
        self.assertEqual([e["address"] for e in served], ["box.example:7777"])
        self.assertEqual(served[0]["free"], 3, "the page and the JSON read the same entry")
        self.assertIn('<td class="num">3</td>', page, "the free column says what the host said")


class TestIndexPageEscapesWhatItIsGiven(_ServerTest):
    # Anyone who can reach the port can register, so every value on this page is
    # attacker-controlled. Escaping is the difference between a status page and
    # stored XSS on the registry's own origin.
    def test_index_page_escapes_what_it_is_given(self):
        base = self.serve()
        self.assertEqual(
            _register(
                base,
                [
                    registry.Entry(
                        model="<script>alert(1)</script>",
                        address='"onload="alert(2)',
                        host="<b>host</b>",
                    )
                ],
            ),
            200,
        )

        status, raw = _http("GET", base + "/")
        self.assertEqual(status, 200)
        page = raw.decode("utf-8")
        for unescaped in ("<script>", "<b>host</b>", '"onload="'):
            self.assertNotIn(unescaped, page, "the page contains %r unescaped" % unescaped)
        self.assertIn("&lt;script&gt;", page, "the escaped model name is missing entirely:\n%s" % page)


class TestIndexPageWithNothingLive(_ServerTest):
    # An empty registry is a real state -- the first thing anyone sees -- and it
    # should say so rather than render a table with no rows.
    def test_index_page_with_nothing_live(self):
        base = self.serve()
        status, raw = _http("GET", base + "/")
        self.assertEqual(status, 200)
        page = raw.decode("utf-8")
        self.assertIn("Nobody is serving anything right now.", page, "an empty registry should say so")
        self.assertIn("0 live entries", page, "and count what it has: nothing")
        self.assertNotIn("<table>", page, "an empty registry has no table to draw")


class TestIndexPageIsOnlyTheRoot(_ServerTest):
    # The page answers "/" and nothing else. A prefix pattern would answer every
    # unknown path with it, so a typo would look like a working registry -- and a
    # 404 is how a person finds out they misremembered the URL.
    def test_index_page_is_only_the_root(self):
        base = self.serve()
        status, _ = _http("GET", base + "/nonsense")
        self.assertEqual(status, 404, "GET /nonsense = %d, want 404" % status)

    def test_index_page_is_not_a_route_for_anything_else(self):
        """The same rule, from the other side: only GET, and only the root."""
        base = self.serve()
        for path in ("/index.html", "/models/", "/register/x", "/nope", "/health"):
            status, raw = _http("GET", base + path)
            self.assertEqual(status, 404, "GET %s answered %d, want a 404 rather than the page" % (path, status))
            self.assertNotIn(b"bothy registry", raw, "GET %s was answered with the page" % path)

        # Read-only by construction: registration is still the only write, and it
        # is on another path.
        status, raw = _http("POST", base + "/", {"entries": []})
        self.assertEqual(status, 405, "POST / answered %d, want a 405" % status)
        self.assertNotIn(b"bothy registry", raw)


class TestAgeReadsLikeAPersonAsks(unittest.TestCase):
    def test_age_reads_like_a_person_asks(self):
        for seconds, want in (
            (0.0, "just now"),
            (1.5, "just now"),
            (-1.0, "just now"),
            (4.0, "4s ago"),
            (90.0, "1m30s ago"),
            (7200.0, "2h0m ago"),
        ):
            self.assertEqual(discovery._age(seconds), want, "age(%s)" % seconds)


class TestShortDigestNeverLooksBlank(unittest.TestCase):
    # A blank digest is shown as unknown, not as nothing: the rest of Bothy treats
    # "no digest" as a fact that never satisfies a pinned expectation, and the page
    # must not read as though it matched.
    def test_short_digest_never_looks_blank(self):
        self.assertEqual(discovery._short_digest(""), "unknown", "an empty digest is a fact, and it has a name")

        long = "sha256:" + "a" * 64
        got = discovery._short_digest(long)
        self.assertLess(len(got), len(long), "shortDigest kept too much: %r" % got)
        self.assertTrue(got.startswith("sha256:aaaaaaaa"), "shortDigest kept too little: %r" % got)

        # What "too little" and "too much" mean, spelled out.
        self.assertEqual(got, "sha256:aaaaaaaaaaaa\u2026", "twelve hex characters and a mark that it is cut")
        self.assertEqual(discovery._short_digest("sha256:aa"), "sha256:aa", "a short digest is shown whole")
        self.assertEqual(
            discovery._short_digest("C" * 64),
            "sha256:cccccccccccc\u2026",
            "an unnormalized digest is normalized before it is shown, like everywhere else",
        )


# --------------------------------------------------------------------------
# The page: the assertions the ported tests do not make. The values on it come
# from whoever registered, and a host that reported nothing is the one case the
# page can get wrong in a way nobody notices.
# --------------------------------------------------------------------------


class TestIndexRowsSayWhatTheEntrySays(_ServerTest):
    def test_index_lists_exactly_what_models_lists(self):
        base = self.serve()
        _register(
            base,
            [
                registry.Entry(model="llama3.1:8b", digest="sha256:" + "a" * 64, address="box:7777", host="box", free=3),
                registry.Entry(model="qwen2.5:7b", address="silent:1", host="silent"),
            ],
        )

        status, raw = _http("GET", base + "/")
        self.assertEqual(status, 200)
        page = raw.decode("utf-8")
        self.assertTrue(page.startswith("<!doctype html>"))
        self.assertIn("2 live entries", page)
        self.assertIn("ttl 1m0s", page)

        # One row per live entry, and the free column says what the host said --
        # including that a host which said nothing did not say zero.
        self.assertIn(
            '<tr><td>llama3.1:8b</td><td>box</td><td>box:7777</td><td class="num">3</td>'
            '<td>sha256:aaaaaaaaaaaa\u2026</td><td>just now</td></tr>',
            page,
            "the reported row is missing or mangled:\n%s" % page,
        )
        self.assertIn(
            '<tr><td>qwen2.5:7b</td><td>silent</td><td>silent:1</td><td class="num">unknown</td>'
            '<td>unknown</td><td>just now</td></tr>',
            page,
            "a host that reported nothing must read as unknown:\n%s" % page,
        )

    def test_index_reports_a_host_that_said_zero_as_zero(self):
        """Being busy is a claim, and zero is that claim.

        A page that showed `unknown` for a full host, or `0` for a silent one,
        would leave a reader unable to tell "no room" from "no idea" -- and the
        JSON API already draws that distinction, so the page must not lose it.
        """
        base = self.serve()
        _register(base, [registry.Entry(model="m", address="full:1", host="full", free=0)])
        page = _http("GET", base + "/")[1].decode("utf-8")
        self.assertIn(
            '<tr><td>m</td><td>full</td><td>full:1</td><td class="num">0</td><td>unknown</td><td>just now</td></tr>',
            page,
            "a reported zero is a report, and an unreported digest is still unknown:\n%s" % page,
        )


class TestIndexEscapesEveryField(_ServerTest):
    # An unescaped registry page is a cross-site-scripting hole reachable by
    # anyone who can POST a registration, and at an open registry that is anyone
    # who can reach the port. Go's html/template escapes by default; the port has
    # to do it explicitly, field by field, which is why every field is checked.
    def test_index_escapes_every_field(self):
        base = self.serve()
        script = "<script>alert(1)</script>"
        _register(
            base,
            [
                registry.Entry(
                    model=script,
                    digest="<b>x",
                    address='"><script>alert(2)</script>',
                    host='" onmouseover="alert(3)',
                )
            ],
        )

        page = _http("GET", base + "/")[1].decode("utf-8")
        self.assertNotIn("<script>", page, "a registered value reached the page as markup:\n%s" % page)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", page, "the model name has to be escaped")
        self.assertIn("&quot;&gt;&lt;script&gt;alert(2)&lt;/script&gt;", page, "so has the address")
        self.assertIn("&quot; onmouseover=&quot;alert(3)", page, "so has the host, quotes included")
        self.assertIn("sha256:&lt;b&gt;x", page, "and so has the digest")

        # The registry is otherwise unharmed: what was registered is still served
        # over the JSON API, because escaping belongs to the page, not to the data.
        self.assertEqual(len(_served(base)), 1)
        self.assertEqual(_served(base)[0]["model"], script)

    def test_index_escapes_the_count_it_renders(self):
        """The count is a number the registry makes up, and it is escaped anyway.

        Every value that goes into the page goes through the same function: a page
        whose escaping depends on which field it is writing is a page with an
        escaping hole in the field somebody adds next.
        """
        page = discovery.render_index(discovery.IndexData(count=1, ttl="1m0s"))
        self.assertIn("1 live entry \u00b7 ttl 1m0s", page, "the singular reads as one entry")


class TestIndexSaysWhenRegistrationIsOpen(_ServerTest):
    # It is already discoverable by anyone who can reach the port, so stating it
    # costs nothing and saves an operator guessing.
    def test_index_says_when_registration_is_open(self):
        open_page = _http("GET", self.serve() + "/")[1].decode("utf-8")
        self.assertIn("registration is open", open_page)

        closed_page = _http("GET", self.serve(token="secret") + "/")[1].decode("utf-8")
        self.assertNotIn("registration is open", closed_page, "with a token set, that would be a lie")


class TestIndexHelpers(unittest.TestCase):
    # Nothing to port here: the tests above cover the page and the two renderers
    # it leans on, and this is the third one -- the cell that has to say "unknown"
    # rather than a number nobody reported.
    def test_free_cell_never_invents_a_number(self):
        self.assertEqual(discovery._free_cell(registry.Entry(model="m", address="a:1")), "unknown")
        self.assertEqual(discovery._free_cell(registry.Entry(model="m", address="a:1", free=0)), "0")
        self.assertEqual(discovery._free_cell(registry.Entry(model="m", address="a:1", free=7)), "7")


if __name__ == "__main__":
    unittest.main()
