"""The discovery record, the store behind the discovery service, and the client
that talks to it.

A registration is a promise with an expiry date: a host says "I have this model at
this address, and I said so just now". These tests are what makes the promise mean
something -- that a host which stops saying so disappears, that a host which said
nothing is not mistaken for a host that is full, and that what a client puts on
the wire is what a registry parses.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import unittest

from bothy import httpx, registry
from bothy.errors import BothyError

# The test servers log through this; nothing in a passing run should reach stderr.
_log = logging.getLogger("bothy.tests.registry")
_log.addHandler(logging.NullHandler())


def _ok(_req, resp):
    """A registry that accepts everything."""
    resp.send_bytes(200)


class _Record:
    """One request as the registry saw it."""

    def __init__(self, req: httpx.Request, body: bytes):
        self.method = req.method
        self.path = req.path
        self.query = req.query
        self.headers = req.headers
        self.body = body


class _RecordServer:
    """A registry that keeps every request a client sent it.

    A client test that only checked error values would not notice a dropped token
    header at all, so what the client actually puts on the wire is recorded here
    and asserted on.
    """

    def __init__(self, handler):
        self._handler = handler
        self._mu = threading.Lock()
        self._seen = []
        self._server = httpx.Server("127.0.0.1:0", self._dispatch, _log)
        self._server.start()
        self.url = "http://" + self._server.addr

    def _dispatch(self, req, resp):
        body = req.body_bytes()
        with self._mu:
            self._seen.append(_Record(req, body))
        self._handler(req, resp)

    def recorded(self):
        with self._mu:
            return list(self._seen)

    def count(self):
        with self._mu:
            return len(self._seen)

    def close(self):
        self._server.shutdown()


class _ServerTestCase(unittest.TestCase):
    """A test case with a registry on a port, cleaned up afterwards."""

    def record_server(self, handler) -> _RecordServer:
        rs = _RecordServer(handler)
        self.addCleanup(rs.close)
        return rs


class TestListFiltersByModel(unittest.TestCase):
    # A request with no tag has to reach a host offering a tagged model, or "who
    # has llama3.1?" would miss llama3.1:8b. An empty request asks for everyone.
    def test_list_filters_by_model(self):
        s = registry.Store(60)
        s.register(
            [
                registry.Entry(model="llama3.1:8b", address="a:1", digest="sha256:1"),
                registry.Entry(model="qwen2.5:7b", address="b:2", digest="sha256:2"),
            ]
        )

        got = s.list("llama3.1")
        self.assertEqual(len(got), 1, "List(llama3.1) = %r, want only a:1" % (got,))
        self.assertEqual(got[0].address, "a:1")
        self.assertEqual(len(s.list("")), 2, 'List("") should return every live entry')


class TestEntriesExpireWithoutHeartbeat(unittest.TestCase):
    # A host that stops heartbeating must fall out of the list, or clients keep
    # dialing machines that went to sleep.
    def test_entries_expire_without_heartbeat(self):
        s = registry.Store(30)
        clock = [1000.0]
        s.now = lambda: clock[0]
        s.register([registry.Entry(model="m", address="a:1")])

        clock[0] += 10
        self.assertEqual(len(s.list("")), 1, "after 10s the entry is still live")

        # Exactly the TTL is still within the promise; only past it is not.
        clock[0] += 20
        self.assertEqual(len(s.list("")), 1, "at exactly the TTL the entry is still live")

        clock[0] += 5  # 35s, past the 30s TTL
        self.assertEqual(len(s.list("")), 0, "after 35s the entry is gone")
        self.assertEqual(s.len(), 0, "Len() = %d, want 0 once expired" % s.len())

    # A fixed clock is the other accepted form: enough to test a TTL at one
    # instant, where a callable is what makes time pass.
    def test_store_accepts_a_fixed_clock(self):
        s = registry.Store(30, now=1000.0)
        s.register([registry.Entry(model="m", address="a:1")])
        self.assertEqual(len(s.list("")), 1)
        self.assertEqual(s.len(), 1)


class TestReRegisterRefreshesInsteadOfDuplicating(unittest.TestCase):
    def test_re_register_refreshes_instead_of_duplicating(self):
        s = registry.Store(60)
        clock = [1000.0]
        s.now = lambda: clock[0]
        s.register([registry.Entry(model="m", address="a:1", digest="sha256:old")])

        clock[0] += 30
        s.register([registry.Entry(model="m", address="a:1", digest="sha256:new")])

        self.assertEqual(s.len(), 1, "a heartbeat must not duplicate the entry")
        self.assertEqual(s.list("")[0].digest, "sha256:new", "the refreshed value should win")


class TestUnusableEntriesAreDropped(unittest.TestCase):
    def test_unusable_entries_are_dropped(self):
        s = registry.Store(60)
        n = s.register(
            [
                registry.Entry(model="no-address"),
                registry.Entry(address="no-model:1"),
                registry.Entry(model="ok", address="a:2"),
            ]
        )
        self.assertEqual(n, 1, "Register accepted %d entries, want 1" % n)


class TestListPrefersHostWithRoom(unittest.TestCase):
    def test_list_prefers_host_with_room(self):
        s = registry.Store(60)
        s.register(
            [
                registry.Entry(model="m", address="busy:1", host="busy", free=0),
                registry.Entry(model="m", address="free:1", host="free", free=4),
            ]
        )
        self.assertEqual(s.list("m")[0].address, "free:1", "the host with free slots should sort first")


class TestListSortsHostsThatReportedBeforeHostsThatDidNot(unittest.TestCase):
    # A host that reported nothing is not a host that is full. Before this they
    # were the same number, which put an uncapped host behind a busy one -- and a
    # client walking the list in order would then ask the busy one first, every
    # time.
    def test_list_sorts_hosts_that_reported_before_hosts_that_did_not(self):
        s = registry.Store(60)
        s.register(
            [
                registry.Entry(model="m", address="silent:1", host="silent"),
                registry.Entry(model="m", address="full:1", host="full", free=0),
                registry.Entry(model="m", address="roomy:1", host="roomy", free=5),
            ]
        )

        got = s.list("m")
        self.assertEqual(len(got), 3, "List = %r, want all three hosts: saying nothing is not a refusal" % (got,))
        self.assertEqual([e.address for e in got], ["roomy:1", "full:1", "silent:1"])


class TestListIsStableForEqualSlots(unittest.TestCase):
    # Sorting is what lets a client take the first entry and be done: capacity
    # descending, then a stable order for ties so two identical lookups agree.
    def test_list_is_stable_for_equal_slots(self):
        s = registry.Store(60)
        s.register(
            [
                registry.Entry(model="m", address="c:1", host="c"),
                registry.Entry(model="m", address="a:1", host="a"),
                registry.Entry(model="m", address="b:1", host="b"),
            ]
        )

        first = s.list("m")
        self.assertEqual(len(first), 3)
        self.assertEqual([e.host for e in first], ["a", "b", "c"], "host order for equal slots")
        second = s.list("m")
        self.assertEqual(
            [e.address for e in first],
            [e.address for e in second],
            "two identical List calls disagreed: %r vs %r" % (first, second),
        )


class TestRegisterKeepsTheUsableEntriesOfABatch(unittest.TestCase):
    # A host offering several models announces them in one call, and a bad entry
    # in the batch must not take the good ones down with it.
    def test_register_keeps_the_usable_entries_of_a_batch(self):
        s = registry.Store(60)
        n = s.register(
            [
                registry.Entry(model="a", address="x:1"),
                registry.Entry(model="", address="y:1"),
                registry.Entry(model="b", address=""),
                registry.Entry(model="c", address="z:1"),
            ]
        )
        self.assertEqual(n, 2, "Register accepted %d of 4 entries, want 2" % n)
        self.assertEqual(len(s.list("")), 2, "List returned %d live entries, want 2" % len(s.list("")))


class TestStoreReportsItsTTL(unittest.TestCase):
    # The registry's TTL is what clients are promised: an entry outlives its
    # host's last heartbeat by exactly this long, and the client reports it so the
    # promise is visible rather than implied.
    def test_store_reports_its_ttl(self):
        self.assertEqual(registry.Store(90).ttl(), 90.0)


class TestRegisterPostsEntriesWithTheToken(_ServerTestCase):
    # Register is the entire liveness protocol -- a host's heartbeat is this call
    # repeated -- so the shape it sends has to be exactly what the registry parses.
    def test_register_posts_entries_with_the_token(self):
        rs = self.record_server(_ok)

        registry.Client(rs.url, "reg-token").register(
            [
                registry.Entry(
                    model="llama3.1:8b", digest="sha256:aa", address="box:7777", host="box", free=3
                )
            ]
        )

        reqs = rs.recorded()
        self.assertEqual(len(reqs), 1, "the registry saw %d requests, want 1" % len(reqs))
        req = reqs[0]
        self.assertEqual((req.method, req.path), ("POST", "/register"), "want POST /register")
        self.assertEqual(req.headers.get("Authorization"), "Bearer reg-token", "want the register token")
        self.assertEqual(req.headers.get("Content-Type"), "application/json")

        payload = json.loads(req.body)
        self.assertEqual(list(payload), ["entries"], "the body is not {\"entries\":[...]}: %r" % (payload,))
        self.assertEqual(len(payload["entries"]), 1)
        raw = payload["entries"][0]
        # The field names are the contract: a Go registry unmarshals these.
        self.assertEqual(
            sorted(raw), ["address", "digest", "free", "host", "last_seen", "model"], "wrong field names on the wire"
        )
        got = registry.Entry.from_json(raw)
        self.assertEqual(
            (got.model, got.address, got.digest, got.free_slots()),
            ("llama3.1:8b", "box:7777", "sha256:aa", (3, True)),
            "want the fields as given: %r" % (got,),
        )


class TestRegisterOmitsWhatWasNotSaid(_ServerTestCase):
    # "free" absent and "free": 0 are different answers, so a host that said
    # nothing must not put a zero on the wire.
    def test_register_omits_what_was_not_said(self):
        rs = self.record_server(_ok)
        registry.Client(rs.url).register([registry.Entry(model="m", address="a:1")])

        raw = json.loads(rs.recorded()[0].body)["entries"][0]
        self.assertEqual(sorted(raw), ["address", "digest", "last_seen", "model"], "want nothing said on the wire")
        self.assertEqual(registry.Entry.from_json(raw).free_slots(), (0, False), "an absent free is not a zero")


class TestRegisterWithoutATokenSendsNoCredential(_ServerTestCase):
    # An open registry is a supported configuration, and it must not send an empty
    # Authorization header -- some proxies reject a malformed credential outright.
    def test_register_without_a_token_sends_no_credential(self):
        rs = self.record_server(_ok)
        registry.Client(rs.url, "").register([registry.Entry(model="m", address="a:1")])
        self.assertNotIn(
            "Authorization", rs.recorded()[0].headers, "want none when no token is configured"
        )


class TestRegisterReportsARefusalFromTheRegistry(_ServerTestCase):
    # A refused registration is a heartbeat that did not land, so the error has to
    # say which registry and why -- otherwise the host logs "registration failed"
    # and the operator is left with nothing.
    def test_register_reports_a_refusal_from_the_registry(self):
        def refuse(_req, resp):
            resp.send_bytes(401, '{"error":{"message":"bad token"}}')

        rs = self.record_server(refuse)

        with self.assertRaises(registry.RegistryError) as caught:
            registry.Client(rs.url, "wrong").register([registry.Entry(model="m", address="a:1")])
        message = str(caught.exception)
        self.assertIsInstance(caught.exception, BothyError)
        for want in (rs.url, "401", "bad token"):
            self.assertIn(want, message, "error %r does not mention %r" % (message, want))


class TestClientErrorsNameTheRegistryWhenItIsUnreachable(_ServerTestCase):
    def test_client_errors_name_the_registry_when_it_is_unreachable(self):
        rs = self.record_server(_ok)
        url = rs.url
        rs.close()  # nothing is listening now

        c = registry.Client(url)
        with self.assertRaises(registry.RegistryError) as caught:
            c.register([registry.Entry(model="m", address="a:1")])
        self.assertIn(url, str(caught.exception), "error %r does not name the registry" % caught.exception)

        with self.assertRaises(registry.RegistryError) as caught:
            c.list("m")
        self.assertIn(url, str(caught.exception), "error %r does not name the registry" % caught.exception)


class TestClientGivesUpOnASilentRegistry(_ServerTestCase):
    # Go ends these calls with a context deadline; here the timeout is the only
    # thing that stops `connect` hanging on a registry that stopped answering
    # instead of falling back to whatever else it knows.
    def test_client_gives_up_on_a_silent_registry(self):
        def silent(_req, resp):
            time.sleep(0.5)
            resp.send_bytes(200)

        rs = self.record_server(silent)
        c = registry.Client(rs.url, timeout=0.2)

        with self.assertRaises(registry.RegistryError) as caught:
            c.register([registry.Entry(model="m", address="a:1")])
        self.assertIn(rs.url, str(caught.exception), "error %r does not name the registry" % caught.exception)


class TestListDecodesEntries(_ServerTestCase):
    def test_list_decodes_entries(self):
        body = (
            '{"entries":[{"model":"llama3.1:8b","digest":"sha256:aa","address":"box:7777",'
            '"free":2}],"count":1}'
        )
        rs = self.record_server(lambda _req, resp: resp.send_bytes(200, body))

        entries = registry.Client(rs.url, "").list("llama3.1:8b")
        self.assertEqual(len(entries), 1, "entries = %r, want 1" % (entries,))
        got = entries[0]
        self.assertEqual(
            (got.address, got.digest, got.free_slots()),
            ("box:7777", "sha256:aa", (2, True)),
            "entry = %r" % (got,),
        )


class TestListDecodesTheTimestampsTheRegistrySent(_ServerTestCase):
    # `last_seen` is the registry's clock, and a client that cannot read it back
    # sees every host as equally old. The literal below is the instant
    # 2026-09-14T10:01:44Z, which is a fact about the wire format rather than
    # something this module computes.
    def test_list_decodes_the_timestamps_the_registry_sent(self):
        body = '{"entries":[{"model":"m","address":"a:1","last_seen":"2026-09-14T10:01:44Z"}]}'
        rs = self.record_server(lambda _req, resp: resp.send_bytes(200, body))

        entries = registry.Client(rs.url, "").list("m")
        self.assertEqual(entries[0].last_seen, 1789380104.0, "the registry's timestamp did not survive the wire")

    # Go writes time.Time with up to nine fractional digits and may write an
    # offset, so both have to land on the same instant they name.
    def test_from_json_reads_the_shapes_go_writes(self):
        cases = {
            "2026-09-14T10:01:44Z": 1789380104.0,
            "2026-09-14T10:01:44.123456789Z": 1789380104.123456,
            "2026-09-14T12:01:44+02:00": 1789380104.0,
        }
        for raw, want in cases.items():
            with self.subTest(raw=raw):
                got = registry.Entry.from_json({"model": "m", "address": "a:1", "last_seen": raw})
                self.assertAlmostEqual(got.last_seen, want, places=6)

    def test_from_json_refuses_a_timestamp_it_cannot_place(self):
        for raw in ("", "not a time", "2026-09-14T10:01:44"):
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError):
                    registry.Entry.from_json({"model": "m", "address": "a:1", "last_seen": raw})

    # An entry the registry never stamped is not an entry from 1970.
    def test_an_absent_timestamp_stays_zero(self):
        self.assertEqual(registry.Entry.from_json({"model": "m", "address": "a:1"}).last_seen, 0.0)
        self.assertEqual(registry.Entry.from_json({"model": "m", "address": "a:1", "last_seen": None}).last_seen, 0.0)

    # Writing and reading are one clock: what a registry stamps comes back as the
    # instant it was, whole or not.
    def test_a_stamped_entry_round_trips(self):
        for seconds in (1789380104.0, 1789380104.123456):
            with self.subTest(seconds=seconds):
                entry = registry.Entry(model="m", address="a:1", last_seen=seconds)
                self.assertEqual(registry.Entry.from_json(entry.to_json()).last_seen, seconds)


class TestListEscapesTheModelName(_ServerTestCase):
    # A model name is a query value, and a tag is not always URL-safe. Getting
    # this wrong asks the registry for a different model and looks like "nobody
    # has it".
    #
    # httpx hands a handler parsed query parameters, not the raw request target,
    # so what is asserted here is the name the registry actually received -- which
    # is the half of it that matters: an unescaped "+" arrives as a space and a
    # raw "/" splits the value.
    def test_list_escapes_the_model_name(self):
        rs = self.record_server(lambda _req, resp: resp.send_bytes(200, '{"entries":[]}'))

        model_name = "llama3.1:8b+vision/tools"
        self.assertEqual(registry.Client(rs.url, "").list(model_name), [])
        self.assertEqual(
            rs.recorded()[0].query.get("model"),
            [model_name],
            "the registry was asked for the wrong model name",
        )


class TestListOmitsTheQueryWhenNoModelIsAskedFor(_ServerTestCase):
    # No model means "everyone", which has to be a bare /models request: an empty
    # ?model= would be a filter for the empty name on some servers.
    def test_list_omits_the_query_when_no_model_is_asked_for(self):
        rs = self.record_server(lambda _req, resp: resp.send_bytes(200, '{"entries":[]}'))
        registry.Client(rs.url, "").list("")
        self.assertEqual(rs.recorded()[0].query, {}, "query = %r, want none" % (rs.recorded()[0].query,))


class TestListRejectsAMalformedAnswer(_ServerTestCase):
    def test_a_non_200_status(self):
        rs = self.record_server(lambda _req, resp: resp.send_bytes(500, "boom"))

        with self.assertRaises(registry.RegistryError) as caught:
            registry.Client(rs.url, "").list("m")
        message = str(caught.exception)
        for want in ("500", "boom"):
            self.assertIn(want, message, "error %r does not mention %r" % (message, want))

    def test_a_body_that_is_not_the_documented_shape(self):
        rs = self.record_server(lambda _req, resp: resp.send_bytes(200, '{"entries":"nope"}'))

        with self.assertRaises(registry.RegistryError) as caught:
            registry.Client(rs.url, "").list("m")
        self.assertIn("/models", str(caught.exception), "error %r does not say which endpoint failed" % caught.exception)


class TestNewClientTrimsTrailingSlashes(_ServerTestCase):
    # A base URL written with a trailing slash is the same registry, and a double
    # slash is a 404 on many servers.
    def test_new_client_trims_trailing_slashes(self):
        c = registry.Client("http://reg.example/", "t")
        self.assertEqual(c.base_url, "http://reg.example", "want the trailing slash trimmed")
        self.assertEqual(c.token, "t", "want the token carried through")

        rs = self.record_server(_ok)
        registry.Client(rs.url + "///", "").register([registry.Entry(model="m", address="a:1")])
        self.assertEqual(rs.recorded()[0].path, "/register", "path = %r, want /register" % rs.recorded()[0].path)
