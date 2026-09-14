package meter

import (
	"io"
	"strings"
	"testing"
	"time"
)

// The text of a refusal is the only thing a peer ever reads: it goes out as the
// 429 body, and an OpenAI-shaped client shows it verbatim. So each reason has to
// name what it is actually about — the host, for a host that is full — rather
// than reporting that something went wrong somewhere. The concurrency case in
// particular must not blame the peer: nothing they did caused it, and a peer told
// to slow down for a host that is merely busy will retry in a way that makes it
// worse.
func TestEachRefusalExplainsItself(t *testing.T) {
	for _, tc := range []struct {
		name    string
		err     LimitError
		mustSay []string
		mustNot []string
	}{
		{
			name:    "a full host is about the host, not the peer",
			err:     LimitError{Peer: "alice", Reason: ReasonConcurrency},
			mustSay: []string{"host"},
			mustNot: []string{"alice"},
		},
		{
			name:    "a rate refusal names the peer and says when to come back",
			err:     LimitError{Peer: "alice", Reason: ReasonRate, RetryAfter: 30 * time.Second},
			mustSay: []string{"alice", "30s"},
		},
		{
			name: "a spent budget says what the budget was",
			err: LimitError{
				Peer: "alice", Reason: ReasonQuota,
				Quota: 200, Window: time.Hour, RetryAfter: 10 * time.Minute,
			},
			mustSay: []string{"alice", "200", "1h0m0s", "10m0s"},
		},
		{
			name:    "a reason this version does not know still says something",
			err:     LimitError{Peer: "alice", Reason: "something new"},
			mustSay: []string{"refused"},
		},
	} {
		t.Run(tc.name, func(t *testing.T) {
			msg := tc.err.Error()
			for _, want := range tc.mustSay {
				if !strings.Contains(msg, want) {
					t.Errorf("message %q does not mention %q", msg, want)
				}
			}
			for _, unwanted := range tc.mustNot {
				if strings.Contains(msg, unwanted) {
					t.Errorf("message %q blames %q for something that is not their doing", msg, unwanted)
				}
			}
		})
	}
}

// Retry-After is a promise to a peer that is already being told no, so it has to
// be arithmetic about that peer's own bucket. A number that is too small turns
// one refusal into a retry storm; one that is too large parks a client for
// longer than it needed to wait. So: it must be positive, roughly right, and
// actually sufficient — waiting the time it quoted has to work.
func TestRetryAfterTellsTheTruthAboutThisPeersBucket(t *testing.T) {
	now := time.Now()
	m := New(Options{RequestsPerMinute: 60})

	// The bucket starts full, so a minute's worth of requests is allowed as a
	// burst. Spending exactly the burst leaves it empty.
	for i := 0; i < 60; i++ {
		if err := m.Begin("alice", now); err != nil {
			t.Fatalf("request %d of the allowed burst was refused: %v", i+1, err)
		}
		m.End("alice", Usage{}, false, 0, now)
	}

	limit := reasonOf(t, m.Begin("alice", now))
	if limit.Reason != ReasonRate {
		t.Fatalf("reason = %q, want %q", limit.Reason, ReasonRate)
	}
	if limit.RetryAfter <= 0 {
		t.Fatalf("RetryAfter = %s from an empty bucket at 60/min, want a positive wait", limit.RetryAfter)
	}
	if limit.RetryAfter > 2*time.Second {
		t.Errorf("RetryAfter = %s, want about a second: at 60/minute one token is back after 1s", limit.RetryAfter)
	}

	// One peer's bucket is their own. A host that refused everybody because one
	// peer had been busy would be worse than no limiter at all.
	if err := m.Begin("bob", now); err != nil {
		t.Errorf("bob was refused because alice had used her burst: %v", err)
	}

	// The wait it quoted has to be enough, or every client that obeys it comes
	// straight back to another refusal.
	if err := m.Begin("alice", now.Add(limit.RetryAfter)); err != nil {
		t.Errorf("alice was still refused after waiting the RetryAfter she was given: %v", err)
	}
}

// A concurrency refusal is about the host and carries no wait, because the slot
// belongs to whoever is holding it and their completion time is not knowable
// here. A bogus Retry-After would be worse than none.
func TestAFullHostQuotesNoWait(t *testing.T) {
	m := New(Options{MaxConcurrent: 1})
	now := time.Now()
	if err := m.Begin("alice", now); err != nil {
		t.Fatal(err)
	}
	limit := reasonOf(t, m.Begin("bob", now))
	if limit.Reason != ReasonConcurrency {
		t.Fatalf("reason = %q, want %q", limit.Reason, ReasonConcurrency)
	}
	if limit.RetryAfter != 0 {
		t.Errorf("RetryAfter = %s for a busy host, want 0: the wait is the other peer's completion, which is unknown", limit.RetryAfter)
	}
}

// The usage route is how an owner sees what is being shared, so the meter has to
// be able to report what budget it is enforcing and not only refuse things. A
// half-configured budget must read as no budget: a window with no request count
// would refuse everything, and a count with no window could never reset.
func TestMeterReportsTheBudgetItEnforces(t *testing.T) {
	m := New(Options{PeerQuota: Quota{Requests: 200, Window: time.Hour}})
	if got := m.Quota(); !got.Enabled() || got.Requests != 200 || got.Window != time.Hour {
		t.Errorf("Quota() = %+v, want the configured 200/1h", got)
	}
	if got := New(Options{}).Quota(); got.Enabled() {
		t.Errorf("Quota() = %+v on a meter with no budget, want it reported as absent", got)
	}
	for _, q := range []Quota{
		{Requests: 5},
		{Window: time.Minute},
		{Requests: -1, Window: time.Minute},
		{Requests: 5, Window: -time.Minute},
	} {
		if q.Enabled() {
			t.Errorf("%+v reads as an enabled budget, but it cannot both allow a request and reset", q)
		}
	}
}

// The host replaces the engine's response body with a sniffer, so the sniffer's
// Close is the only thing that closes the connection to the engine. If it did not
// reach through, every request would leak a connection — quietly, and only under
// load, which is the worst way to find out.
func TestClosingTheSnifferClosesTheEngineConnection(t *testing.T) {
	const stream = "data: {\"usage\":{\"total_tokens\":7}}\n\ndata: [DONE]\n\n"
	body := &countedBody{Reader: strings.NewReader(stream)}
	s := NewSniffer(body, "text/event-stream")

	if _, err := io.ReadAll(s); err != nil {
		t.Fatalf("reading the stream: %v", err)
	}
	if usage, reported := s.Usage(); !reported || usage.TotalTokens != 7 {
		t.Errorf("usage = %+v reported = %v, want the streamed total", usage, reported)
	}
	if s.Bytes() != int64(len(stream)) {
		t.Errorf("Bytes() = %d, want %d: every byte has to be counted on the way past", s.Bytes(), len(stream))
	}
	if err := s.Close(); err != nil {
		t.Fatalf("Close: %v", err)
	}
	if !body.closed {
		t.Error("closing the sniffer did not close the engine's body")
	}
}

type countedBody struct {
	io.Reader
	closed bool
}

func (b *countedBody) Close() error {
	b.closed = true
	return nil
}
