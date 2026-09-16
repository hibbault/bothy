"""The meter: what each peer uses, and the limits that apply to it.

These tests are the definition of the behaviour. Limits are the one part of a
host that a stranger can feel, so each one here is pinned to the refusal a peer
actually reads: a host that is merely busy must not blame the peer, a spent
budget must say what the budget was, and a Retry-After has to be arithmetic about
that peer's own bucket rather than a number that looks reassuring.
"""

from __future__ import annotations

import io
import time
import unittest
from datetime import datetime, timezone

from bothy import meter
from bothy.meter import LimitError, Meter, Options, Quota, Sniffer, Usage


class ChunkReader:
    """Hands out bytes in exactly the pieces given, so a test can split a stream
    anywhere -- including mid-JSON, which is what a naive line parser gets wrong."""

    def __init__(self, chunks):
        self._chunks = [bytes(chunk) for chunk in chunks]
        self.closed = False

    def read(self, size=-1):
        while self._chunks:
            chunk = self._chunks[0]
            if not chunk:
                self._chunks.pop(0)
                continue
            if size is None or size < 0 or size >= len(chunk):
                self._chunks.pop(0)
                return chunk
            self._chunks[0] = chunk[size:]
            return chunk[:size]
        return b""

    def close(self):
        self.closed = True


class CountedBody:
    """A body that notices being closed, standing in for the engine's connection."""

    def __init__(self, data):
        self._buf = io.BytesIO(data)
        self.closed = False

    def read(self, size=-1):
        return self._buf.read(size)

    def close(self):
        self.closed = True


class _MeterCase(unittest.TestCase):
    """The lookups the Go tests took a *testing.T for."""

    def peer_row(self, m, name):
        """Finds one peer's row in the usage report."""
        for row in m.snapshot():
            if row.peer == name:
                return row
        self.fail("no usage row for %r" % (name,))

    def refusal(self, m, peer, now):
        """The refusal a request is turned away with."""
        try:
            m.begin(peer, now)
        except meter.LimitError as err:
            return err
        self.fail("want a refusal, got none")


class TestExtractOpenAIUsage(unittest.TestCase):
    def test_extract_openai_usage(self):
        body = (
            b'{"choices":[{"message":{"content":"hi"}}],'
            b'"usage":{"prompt_tokens":11,"completion_tokens":7,"total_tokens":18}}'
        )
        usage, ok = meter.extract(body)
        self.assertTrue(ok, "expected usage to be found")
        self.assertEqual(
            (usage.prompt_tokens, usage.completion_tokens, usage.total_tokens), (11, 7, 18)
        )


class TestExtractOllamaUsage(unittest.TestCase):
    # Ollama's native shape has no nested usage object and no total, so the total
    # has to be derived.
    def test_extract_ollama_usage(self):
        body = b'{"model":"llama3.1:8b","done":true,"prompt_eval_count":9,"eval_count":4}'
        usage, ok = meter.extract(body)
        self.assertTrue(ok, "expected usage to be found")
        self.assertEqual(
            (usage.prompt_tokens, usage.completion_tokens, usage.total_tokens),
            (9, 4, 13),
            "want 9/4/13",
        )


class TestExtractIgnoresObjectsWithNoCounts(unittest.TestCase):
    def test_extract_ignores_objects_with_no_counts(self):
        for body in [
            b'{"choices":[{"delta":{"content":"hello"}}]}',
            b'{"model":"llama3.1:8b","done":false,"response":"hi"}',
            b"not json at all",
            b"",
        ]:
            with self.subTest(body=body):
                usage, ok = meter.extract(body)
                self.assertFalse(ok, "extract(%r) = %r, true; want no usage" % (body, usage))


class TestExtractKeepsAReportedTotalWithNoParts(unittest.TestCase):
    # An engine that reports a total and no breakdown is still reporting
    # something, and the total has to survive: recomputing it as 0+0 would record
    # a fabricated zero for a response that cost real tokens, in both the OpenAI
    # and the flat shape.
    def test_extract_keeps_a_reported_total_with_no_parts(self):
        for body in [b'{"usage":{"total_tokens":42}}', b'{"total_tokens":42}']:
            with self.subTest(body=body):
                usage, ok = meter.extract(body)
                if not ok:
                    self.fail("extract(%r) found no usage, but it reports a total" % (body,))
                self.assertEqual(usage.total_tokens, 42, "extract(%r).total_tokens" % (body,))
                self.assertEqual(
                    (usage.prompt_tokens, usage.completion_tokens),
                    (0, 0),
                    "extract(%r) = %r, want no invented parts" % (body, usage),
                )


class TestSnifferPassesBytesThroughUnchanged(unittest.TestCase):
    def test_sniffer_passes_bytes_through_unchanged(self):
        body = b'{"usage":{"prompt_tokens":3,"completion_tokens":2,"total_tokens":5}}'
        sniffer = Sniffer(ChunkReader([body]), "application/json")

        got = sniffer.read()
        self.assertEqual(got, body, "sniffer altered the body")
        usage, reported = sniffer.usage()
        self.assertTrue(reported)
        self.assertEqual(usage.total_tokens, 5, "usage = %r, reported = %r" % (usage, reported))
        self.assertEqual(sniffer.bytes(), len(body))


class TestSnifferHandlesAnySplitBoundary(unittest.TestCase):
    # The same stream is replayed split at every awkward boundary, because a
    # sniffer that only worked on whole frames would miss the usage line in
    # practice.
    def test_sniffer_handles_any_split_boundary(self):
        body = (
            b'data: {"choices":[{"delta":{"content":"one"}}]}\n\n'
            b'data: {"choices":[{"delta":{"content":"two"}}]}\n\n'
            b'data: {"usage":{"prompt_tokens":8,"completion_tokens":5,"total_tokens":13}}\n\n'
            b"data: [DONE]\n\n"
        )
        for size in [1, 2, 3, 5, 7, 13, 40, 256, len(body)]:
            with self.subTest(chunk=size):
                chunks = [body[i : i + size] for i in range(0, len(body), size)]
                sniffer = Sniffer(ChunkReader(chunks), "text/event-stream")

                got = sniffer.read()
                self.assertEqual(got, body, "sniffer altered the stream")
                usage, reported = sniffer.usage()
                self.assertTrue(reported, "usage = %r, reported = %r" % (usage, reported))
                self.assertEqual(
                    (usage.prompt_tokens, usage.completion_tokens, usage.total_tokens),
                    (8, 5, 13),
                )


class TestSnifferMergesUsageFromLaterFrames(unittest.TestCase):
    def test_sniffer_merges_usage_from_later_frames(self):
        # An engine that streams partial counts must not lose them to a later zero.
        body = (
            'data: {"prompt_eval_count":5,"eval_count":1}\n\n'
            'data: {"done":true,"prompt_eval_count":5,"eval_count":9}\n\n'
        )
        sniffer = Sniffer(ChunkReader([body.encode()]), "text/event-stream")
        sniffer.read()
        usage, reported = sniffer.usage()
        self.assertTrue(reported, "usage = %r, reported = %r" % (usage, reported))
        self.assertEqual(
            (usage.prompt_tokens, usage.completion_tokens),
            (5, 9),
            "want the final counts",
        )


class TestSnifferReportsNothingWhenTheEngineSaysNothing(unittest.TestCase):
    def test_sniffer_reports_nothing_when_the_engine_says_nothing(self):
        body = b'{"choices":[{"message":{"content":"hi"}}]}'
        sniffer = Sniffer(ChunkReader([body]), "application/json")
        sniffer.read()
        _, reported = sniffer.usage()
        self.assertFalse(reported, "an engine that reports no usage must not be recorded as reporting zero")
        self.assertEqual(sniffer.bytes(), len(body))


class TestClosingTheSnifferClosesTheEngineConnection(unittest.TestCase):
    # The host replaces the engine's response body with a sniffer, so the
    # sniffer's close is the only thing that closes the connection to the engine.
    # If it did not reach through, every request would leak a connection --
    # quietly, and only under load, which is the worst way to find out.
    def test_closing_the_sniffer_closes_the_engine_connection(self):
        stream = 'data: {"usage":{"total_tokens":7}}\n\ndata: [DONE]\n\n'
        body = CountedBody(stream.encode())
        sniffer = Sniffer(body, "text/event-stream")

        sniffer.read()
        usage, reported = sniffer.usage()
        self.assertTrue(reported, "usage = %r reported = %r, want the streamed total" % (usage, reported))
        self.assertEqual(usage.total_tokens, 7)
        self.assertEqual(
            sniffer.bytes(),
            len(stream),
            "every byte has to be counted on the way past",
        )
        sniffer.close()
        self.assertTrue(body.closed, "closing the sniffer did not close the engine's body")


class TestAStreamedResponseHoldsItsSlotUntilTheStreamIsDone(unittest.TestCase):
    # The slot a streamed request reserved is released when the request is over,
    # and a stream is not over until its last frame is out. Releasing it at the
    # headers would let one long generation hold capacity the host had already
    # advertised as free, so this pins the pairing: read, then release.
    def test_a_streamed_response_holds_its_slot_until_the_stream_is_done(self):
        m = Meter(Options(max_concurrent=1))
        now = time.time()
        m.begin("alice", now)
        self.assertEqual(m.free_slots(), 0)

        frames = [
            b'data: {"prompt_eval_count":3,"eval_count":1}\n\n',
            b'data: {"done":true,"prompt_eval_count":3,"eval_count":7}\n\n',
            b"data: [DONE]\n\n",
        ]
        sniffer = Sniffer(ChunkReader(frames), "text/event-stream")

        self.assertTrue(sniffer.read(4096), "want the first frame")
        # Mid-stream, the request is still being served: a stream that let go of
        # its slot here would hand the host's capacity back before the work that
        # is using it had finished.
        self.assertEqual(m.in_flight(), 1)
        self.assertEqual(m.free_slots(), 0)

        while sniffer.read(4096):
            pass
        usage, reported = sniffer.usage()
        m.end("alice", usage, reported, sniffer.bytes(), now)

        self.assertTrue(reported)
        self.assertEqual((usage.prompt_tokens, usage.completion_tokens), (3, 7))
        self.assertEqual(m.in_flight(), 0)
        self.assertEqual(m.free_slots(), 1)


class TestMeterConcurrencyCap(_MeterCase):
    def test_meter_concurrency_cap(self):
        m = Meter(Options(max_concurrent=2))
        now = time.time()

        m.begin("alice", now)
        m.begin("bob", now)
        limit = self.refusal(m, "alice", now)
        self.assertEqual(limit.reason, meter.reason_concurrency)

        m.end("alice", Usage(), False, 0, now)
        m.begin("bob", now)


class TestMeterRateLimitRefills(_MeterCase):
    def test_meter_rate_limit_refills(self):
        m = Meter(Options(requests_per_minute=3))
        now = time.time()

        for _ in range(3):
            m.begin("alice", now)
            m.end("alice", Usage(), False, 0, now)

        limit = self.refusal(m, "alice", now)
        self.assertEqual(limit.reason, meter.reason_rate)
        self.assertGreater(limit.retry_after, 0, "a rate limit must tell the peer how long to wait")

        # A third of a minute later, one token has refilled.
        m.begin("alice", now + 20.0)


class TestMeterRatesAreIsolatedPerPeer(unittest.TestCase):
    def test_meter_rates_are_isolated_per_peer(self):
        m = Meter(Options(requests_per_minute=1))
        now = time.time()

        m.begin("alice", now)
        m.end("alice", Usage(), False, 0, now)
        with self.assertRaises(meter.LimitError):
            m.begin("alice", now)
        m.begin("bob", now)


class TestMeterFreeSlotsFollowsLoad(unittest.TestCase):
    def test_meter_free_slots_follows_load(self):
        m = Meter(Options(max_concurrent=3))
        now = time.time()

        self.assertEqual(m.free_slots(), 3)
        m.begin("alice", now)
        self.assertEqual(m.free_slots(), 2)
        m.end("alice", Usage(), False, 0, now)
        self.assertEqual(m.free_slots(), 3, "after release")


class TestMeterUncappedAdvertisesNoNumber(unittest.TestCase):
    def test_meter_uncapped_advertises_no_number(self):
        m = Meter(Options())
        self.assertEqual(m.free_slots(), 0, "want 0 for an uncapped host")
        m.begin("alice", time.time())


class TestMeterDistinguishesUnmeteredFromFree(unittest.TestCase):
    # Reported-but-zero differs from unreported, and the counters have to keep
    # them apart, or "the engine said nothing" would look like "the engine said
    # free".
    def test_meter_distinguishes_unmetered_from_free(self):
        m = Meter(Options())
        now = time.time()

        m.begin("alice", now)
        m.end("alice", Usage(prompt_tokens=10, completion_tokens=4, total_tokens=14), True, 100, now)
        m.begin("alice", now)
        m.end("alice", Usage(), False, 50, now)

        rows = m.snapshot()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual((row.requests, row.prompt_tokens, row.completion_tokens), (2, 10, 4))
        self.assertEqual(row.unmetered, 1)
        self.assertEqual(row.response_bytes, 150)
        self.assertEqual(row.limited, 0)


class TestMeterSnapshotPutsHeaviestPeerFirst(unittest.TestCase):
    def test_meter_snapshot_puts_heaviest_peer_first(self):
        m = Meter(Options())
        now = time.time()

        m.begin("small", now)
        m.end("small", Usage(prompt_tokens=1, completion_tokens=1), True, 0, now)
        m.begin("big", now)
        m.end("big", Usage(prompt_tokens=900, completion_tokens=100), True, 0, now)

        rows = m.snapshot()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0].peer, "big", "rows = %r, want big first" % (rows,))


class TestMeterCountsRefusals(unittest.TestCase):
    def test_meter_counts_refusals(self):
        m = Meter(Options(max_concurrent=1))
        now = time.time()

        m.begin("alice", now)
        with self.assertRaises(meter.LimitError):
            m.begin("alice", now)
        rows = m.snapshot()
        self.assertEqual(len(rows), 1, "rows = %r, want one refused request recorded" % (rows,))
        self.assertEqual(rows[0].limited, 1)


class TestOwnerReserveKeepsAPeerSlotFree(_MeterCase):
    # The whole point of the reserve: with the cap reached, the owner still has
    # somewhere to go.
    def test_owner_reserve_keeps_a_peer_slot_free(self):
        m = Meter(Options(max_concurrent=4, owner_reserve=1))
        now = time.time()

        for i in range(3):
            try:
                m.begin("alice", now)
            except meter.LimitError as err:
                self.fail("peer request %d refused: %s" % (i + 1, err))

        self.assertEqual(self.refusal(m, "alice", now).reason, meter.reason_concurrency)
        self.assertEqual(m.in_flight(), 3, "the reserved slot must stay free")
        self.assertEqual(m.free_slots(), 0, "a reserved slot is not capacity to advertise")


class TestOwnerReserveIsAdvertisedAsUnavailable(_MeterCase):
    def test_owner_reserve_is_advertised_as_unavailable(self):
        m = Meter(Options(max_concurrent=4, owner_reserve=1))
        now = time.time()

        self.assertEqual(m.peer_slots(), 3)
        for want in range(3, 0, -1):
            self.assertEqual(m.free_slots(), want)
            m.begin("alice", now)
        self.assertEqual(m.free_slots(), 0, "with every peer slot taken")


class TestWithoutAReservePeersMayUseEverySlot(_MeterCase):
    def test_without_a_reserve_peers_may_use_every_slot(self):
        m = Meter(Options(max_concurrent=2))
        now = time.time()

        self.assertEqual(m.peer_slots(), 2)
        for _ in range(2):
            m.begin("alice", now)
        self.assertEqual(self.refusal(m, "alice", now).reason, meter.reason_concurrency)


class TestAReserveThatSwallowsTheCapFailsClosed(_MeterCase):
    # The misconfiguration the host refuses at startup. If it is ever reached
    # anyway, the meter must serve nobody rather than everybody: "no slots" must
    # never read as "no limit".
    def test_a_reserve_that_swallows_the_cap_fails_closed(self):
        m = Meter(Options(max_concurrent=2, owner_reserve=5))
        now = time.time()

        self.assertEqual(m.peer_slots(), 0)
        self.assertEqual(m.free_slots(), 0)
        self.assertEqual(self.refusal(m, "alice", now).reason, meter.reason_concurrency)
        self.assertEqual(m.in_flight(), 0, "nothing may be admitted")


class TestNoCapStillMeansNoCap(unittest.TestCase):
    def test_no_cap_still_means_no_cap(self):
        m = Meter(Options(max_concurrent=0, owner_reserve=1))
        now = time.time()

        # Zero here means "uncapped and therefore unknown", not "no slots".
        self.assertEqual(m.peer_slots(), 0, "want 0 (uncapped)")
        self.assertEqual(m.free_slots(), 0, "want 0 (unknown)")
        for i in range(20):
            try:
                m.begin("alice", now)
            except meter.LimitError as err:
                self.fail("request %d refused with no cap set: %s" % (i + 1, err))


class TestQuotaIsASustainedBudgetNotARate(_MeterCase):
    def test_quota_is_a_sustained_budget_not_a_rate(self):
        m = Meter(Options(max_concurrent=10, peer_quota=Quota(requests=2, window=3600.0)))
        now = time.time()

        for i in range(2):
            try:
                m.begin("alice", now)
            except meter.LimitError as err:
                self.fail("request %d of the budget refused: %s" % (i + 1, err))
            m.end("alice", Usage(), False, 0, now)

        limit = self.refusal(m, "alice", now)
        self.assertEqual(limit.reason, meter.reason_quota)
        self.assertEqual((limit.quota, limit.window), (2, 3600.0), "want 2 per 1h0m0s")
        self.assertGreater(limit.retry_after, 0)
        self.assertLessEqual(limit.retry_after, 3600.0, "want something up to an hour")
        # The message has to be usable by whoever is looking at a peer's logs.
        for want in ["budget of 2 requests", "retry in"]:
            self.assertIn(want, str(limit), "refusal %r does not mention %r" % (str(limit), want))


class TestQuotaWindowTurnsOver(_MeterCase):
    def test_quota_window_turns_over(self):
        m = Meter(Options(max_concurrent=10, peer_quota=Quota(requests=1, window=3600.0)))
        start = time.time()

        m.begin("alice", start)
        m.end("alice", Usage(), False, 0, start)
        with self.assertRaises(meter.LimitError):
            m.begin("alice", start + 60.0)

        # The window is the peer's own hour, so it starts when they started.
        try:
            m.begin("alice", start + 3601.0)
        except meter.LimitError as err:
            self.fail("the budget did not come back after the window: %s" % (err,))
        used = self.peer_row(m, "alice").quota_used
        self.assertEqual(used, 1, "after the window turned over")


class TestQuotaIsPerPeer(_MeterCase):
    def test_quota_is_per_peer(self):
        m = Meter(Options(max_concurrent=10, peer_quota=Quota(requests=1, window=3600.0)))
        now = time.time()

        m.begin("alice", now)
        m.end("alice", Usage(), False, 0, now)
        self.assertEqual(self.refusal(m, "alice", now).reason, meter.reason_quota)
        try:
            m.begin("bob", now)
        except meter.LimitError as err:
            self.fail("bob was punished for alice's usage: %s" % (err,))


class TestARefusalDoesNotSpendAnotherLimitsAllowance(_MeterCase):
    # Pins the two-phase begin. A peer turned away for one reason must not also
    # lose part of a different budget for a request that was never served.
    def test_a_refusal_does_not_spend_another_limits_allowance(self):
        with self.subTest(case="a rate refusal does not spend budget"):
            m = Meter(
                Options(
                    max_concurrent=10,
                    requests_per_minute=1,
                    peer_quota=Quota(requests=5, window=3600.0),
                )
            )
            now = time.time()

            m.begin("alice", now)
            m.end("alice", Usage(), False, 0, now)

            self.assertEqual(self.refusal(m, "alice", now).reason, meter.reason_rate)
            used = self.peer_row(m, "alice").quota_used
            self.assertEqual(used, 1, "after a rate refusal")

        with self.subTest(case="a concurrency refusal does not spend budget"):
            m = Meter(Options(max_concurrent=1, peer_quota=Quota(requests=2, window=3600.0)))
            now = time.time()

            m.begin("alice", now)
            self.assertEqual(self.refusal(m, "alice", now).reason, meter.reason_concurrency)
            m.end("alice", Usage(), False, 0, now)

            # Two requests of budget, one used. If the concurrency refusal had
            # spent the second, this would come back as a quota refusal.
            try:
                m.begin("alice", now)
            except meter.LimitError as err:
                self.fail("a refusal spent the peer's budget: %s" % (err,))
            self.assertEqual(self.peer_row(m, "alice").quota_used, 2)


class TestUsageReportsTheBudgetSoItCanBeToldFromACrash(_MeterCase):
    def test_usage_reports_the_budget_so_it_can_be_told_from_a_crash(self):
        now = time.time()

        with_quota = Meter(Options(max_concurrent=10, peer_quota=Quota(requests=3, window=3600.0)))
        with_quota.begin("alice", now)
        with_quota.end("alice", Usage(), False, 0, now)

        row = self.peer_row(with_quota, "alice")
        self.assertEqual(row.quota_used, 1)
        try:
            reset = datetime.fromisoformat(row.quota_reset.replace("Z", "+00:00"))
        except ValueError as err:
            self.fail("quota_reset = %r, which is not a timestamp: %s" % (row.quota_reset, err))
        self.assertGreater(reset, datetime.fromtimestamp(now, timezone.utc))
        self.assertLessEqual(reset, datetime.fromtimestamp(now + 3660.0, timezone.utc))

        without_quota = Meter(Options(max_concurrent=10))
        without_quota.begin("alice", now)
        without_quota.end("alice", Usage(), False, 0, now)
        row = self.peer_row(without_quota, "alice")
        self.assertEqual(
            (row.quota_used, row.quota_reset),
            (0, ""),
            "a host with no budget reported one: %r" % (row,),
        )


class TestARefusalOpensNoWindow(_MeterCase):
    # A peer who was never served has a row, because a refusal is worth counting,
    # but no budget window -- so they are not reported as being part-way through
    # one.
    def test_a_refusal_opens_no_window(self):
        m = Meter(Options(max_concurrent=1, peer_quota=Quota(requests=3, window=3600.0)))
        now = time.time()

        m.begin("alice", now)
        self.assertEqual(self.refusal(m, "bob", now).reason, meter.reason_concurrency)

        row = self.peer_row(m, "bob")
        self.assertEqual(
            (row.quota_used, row.quota_reset),
            (0, ""),
            "a peer who was never served reported a budget window: %r" % (row,),
        )
        row = self.peer_row(m, "alice")
        self.assertEqual(row.quota_used, 1, "alice's admitted request did not open a window")
        self.assertNotEqual(row.quota_reset, "")


class TestEachRefusalExplainsItself(unittest.TestCase):
    # The text of a refusal is the only thing a peer ever reads: it goes out as
    # the 429 body, and an OpenAI-shaped client shows it verbatim. So each reason
    # has to name what it is actually about -- the host, for a host that is full
    # -- rather than reporting that something went wrong somewhere. The
    # concurrency case in particular must not blame the peer: nothing they did
    # caused it, and a peer told to slow down for a host that is merely busy will
    # retry in a way that makes it worse.
    def test_each_refusal_explains_itself(self):
        cases = [
            (
                "a full host is about the host, not the peer",
                LimitError(peer="alice", reason=meter.reason_concurrency),
                ["host"],
                ["alice"],
            ),
            (
                "a rate refusal names the peer and says when to come back",
                LimitError(peer="alice", reason=meter.reason_rate, retry_after=30.0),
                ["alice", "30s"],
                [],
            ),
            (
                "a spent budget says what the budget was",
                LimitError(
                    peer="alice",
                    reason=meter.reason_quota,
                    quota=200,
                    window=3600.0,
                    retry_after=600.0,
                ),
                ["alice", "200", "1h0m0s", "10m0s"],
                [],
            ),
            (
                "a reason this version does not know still says something",
                LimitError(peer="alice", reason="something new"),
                ["refused"],
                [],
            ),
        ]
        for name, err, must_say, must_not in cases:
            with self.subTest(case=name):
                msg = str(err)
                for want in must_say:
                    self.assertIn(want, msg, "message %r does not mention %r" % (msg, want))
                for unwanted in must_not:
                    self.assertNotIn(
                        unwanted,
                        msg,
                        "message %r blames %r for something that is not their doing" % (msg, unwanted),
                    )


class TestRetryAfterTellsTheTruthAboutThisPeersBucket(_MeterCase):
    # Retry-After is a promise to a peer that is already being told no, so it has
    # to be arithmetic about that peer's own bucket. A number that is too small
    # turns one refusal into a retry storm; one that is too large parks a client
    # for longer than it needed to wait. So: it must be positive, roughly right,
    # and actually sufficient -- waiting the time it quoted has to work.
    def test_retry_after_tells_the_truth_about_this_peers_bucket(self):
        now = time.time()
        m = Meter(Options(requests_per_minute=60))

        # The bucket starts full, so a minute's worth of requests is allowed as a
        # burst. Spending exactly the burst leaves it empty.
        for i in range(60):
            try:
                m.begin("alice", now)
            except meter.LimitError as err:
                self.fail("request %d of the allowed burst was refused: %s" % (i + 1, err))
            m.end("alice", Usage(), False, 0, now)

        limit = self.refusal(m, "alice", now)
        self.assertEqual(limit.reason, meter.reason_rate)
        self.assertGreater(
            limit.retry_after, 0, "from an empty bucket at 60/min, want a positive wait"
        )
        self.assertLessEqual(
            limit.retry_after,
            2.0,
            "want about a second: at 60/minute one token is back after 1s",
        )

        # One peer's bucket is their own. A host that refused everybody because
        # one peer had been busy would be worse than no limiter at all.
        try:
            m.begin("bob", now)
        except meter.LimitError as err:
            self.fail("bob was refused because alice had used her burst: %s" % (err,))

        # The wait it quoted has to be enough, or every client that obeys it
        # comes straight back to another refusal.
        try:
            m.begin("alice", now + limit.retry_after)
        except meter.LimitError as err:
            self.fail("alice was still refused after waiting the retry_after she was given: %s" % (err,))


class TestAFullHostQuotesNoWait(_MeterCase):
    # A concurrency refusal is about the host and carries no wait, because the
    # slot belongs to whoever is holding it and their completion time is not
    # knowable here. A bogus Retry-After would be worse than none.
    def test_a_full_host_quotes_no_wait(self):
        m = Meter(Options(max_concurrent=1))
        now = time.time()
        m.begin("alice", now)
        limit = self.refusal(m, "bob", now)
        self.assertEqual(limit.reason, meter.reason_concurrency)
        self.assertEqual(
            limit.retry_after,
            0,
            "the wait is the other peer's completion, which is unknown",
        )


class TestMeterReportsTheBudgetItEnforces(unittest.TestCase):
    # The usage route is how an owner sees what is being shared, so the meter has
    # to be able to report what budget it is enforcing and not only refuse things.
    # A half-configured budget must read as no budget: a window with no request
    # count would refuse everything, and a count with no window could never
    # reset.
    def test_meter_reports_the_budget_it_enforces(self):
        m = Meter(Options(peer_quota=Quota(requests=200, window=3600.0)))
        got = m.quota()
        self.assertTrue(got.enabled())
        self.assertEqual((got.requests, got.window), (200, 3600.0), "want the configured 200/1h")
        self.assertFalse(
            Meter(Options()).quota().enabled(), "want it reported as absent"
        )
        for quota in [
            Quota(requests=5),
            Quota(window=60.0),
            Quota(requests=-1, window=60.0),
            Quota(requests=5, window=-60.0),
        ]:
            with self.subTest(quota=quota):
                self.assertFalse(
                    quota.enabled(),
                    "%r reads as an enabled budget, but it cannot both allow a request and reset"
                    % (quota,),
                )


class TestOnePeerCannotHoldEverySlot(_MeterCase):
    # Without a per-peer cap, max_concurrent is first-come-first-served: one
    # caller with parallel requests holds every slot and everyone else is told
    # the host is full. The host having room and the peer having room are
    # different answers, and a caller needs to be able to tell them apart -- one
    # means "try another host", the other means "wait for your own request".
    def test_one_peer_cannot_hold_every_slot(self):
        m = Meter(Options(max_concurrent=4, owner_reserve=1, peer_max_concurrent=1))
        now = time.time()

        m.begin("alice", now)
        limit = self.refusal(m, "alice", now)
        self.assertEqual(limit.reason, meter.reason_peer_concurrency)
        self.assertGreater(
            limit.retry_after,
            0,
            "a refusal that resolves when their own request finishes still needs a Retry-After, "
            "or it reads as never",
        )

        # The point of the cap: somebody else can still be served.
        try:
            m.begin("bob", now)
        except meter.LimitError as err:
            self.fail("bob was refused while the host had free slots: %s" % (err,))
        self.assertEqual(m.in_flight(), 2)

        # Finishing a request hands the slot back.
        m.end("alice", Usage(prompt_tokens=1, completion_tokens=1), True, 10, now)
        try:
            m.begin("alice", now)
        except meter.LimitError as err:
            self.fail("alice was refused after releasing her slot: %s" % (err,))


class TestAPeerSlotRefusalDoesNotSpendTheBudget(_MeterCase):
    # A refusal must not spend anything: alice is turned away for holding too many
    # slots, and that must not also cost her a request of her budget.
    def test_a_peer_slot_refusal_does_not_spend_the_budget(self):
        m = Meter(
            Options(
                max_concurrent=4,
                owner_reserve=0,
                peer_max_concurrent=1,
                peer_quota=Quota(requests=2, window=3600.0),
            )
        )
        now = time.time()

        try:
            m.begin("alice", now)
        except meter.LimitError as err:
            self.fail("first request refused: %s" % (err,))
        with self.assertRaises(meter.LimitError):
            m.begin("alice", now)  # refused for the slot cap

        m.end("alice", Usage(), True, 0, now)
        try:
            m.begin("alice", now)
        except meter.LimitError as err:
            self.fail("second request refused, and it should have had budget: %s" % (err,))
        m.end("alice", Usage(), True, 0, now)

        self.assertEqual(
            self.refusal(m, "alice", now).reason,
            meter.reason_quota,
            "the slot refusal must not have counted",
        )
        self.assertEqual(self.peer_row(m, "alice").limited, 2)


if __name__ == "__main__":
    unittest.main()
