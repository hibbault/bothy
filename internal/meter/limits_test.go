package meter

import (
	"errors"
	"strings"
	"testing"
	"time"
)

// peerRow finds one peer's row in the usage report.
func peerRow(t *testing.T, m *Meter, name string) PeerUsage {
	t.Helper()
	for _, row := range m.Snapshot() {
		if row.Peer == name {
			return row
		}
	}
	t.Fatalf("no usage row for %q", name)
	return PeerUsage{}
}

func reasonOf(t *testing.T, err error) *LimitError {
	t.Helper()
	if err == nil {
		t.Fatal("want a refusal, got none")
	}
	var limit *LimitError
	if !errors.As(err, &limit) {
		t.Fatalf("error %v is not a LimitError", err)
	}
	return limit
}

// TestOwnerReserveKeepsAPeerSlotFree is the whole point of the reserve: with the
// cap reached, the owner still has somewhere to go.
func TestOwnerReserveKeepsAPeerSlotFree(t *testing.T) {
	m := New(Options{MaxConcurrent: 4, OwnerReserve: 1})
	now := time.Now()

	for i := 0; i < 3; i++ {
		if err := m.Begin("alice", now); err != nil {
			t.Fatalf("peer request %d refused: %v", i+1, err)
		}
	}
	if limit := reasonOf(t, m.Begin("alice", now)); limit.Reason != ReasonConcurrency {
		t.Fatalf("fourth peer request was refused for %q, want concurrency", limit.Reason)
	}
	if got := m.InFlight(); got != 3 {
		t.Errorf("InFlight() = %d, want 3 — the reserved slot must stay free", got)
	}
	if got := m.Capacity(); got != 0 {
		t.Errorf("Capacity() = %d, want 0 — a reserved slot is not capacity to advertise", got)
	}
}

func TestOwnerReserveIsAdvertisedAsUnavailable(t *testing.T) {
	m := New(Options{MaxConcurrent: 4, OwnerReserve: 1})
	now := time.Now()

	if got := m.PeerSlots(); got != 3 {
		t.Fatalf("PeerSlots() = %d, want 3", got)
	}
	for want := 3; want >= 1; want-- {
		if got := m.Capacity(); got != want {
			t.Fatalf("Capacity() = %d, want %d", got, want)
		}
		if err := m.Begin("alice", now); err != nil {
			t.Fatal(err)
		}
	}
	if got := m.Capacity(); got != 0 {
		t.Errorf("Capacity() = %d with every peer slot taken, want 0", got)
	}
}

func TestWithoutAReservePeersMayUseEverySlot(t *testing.T) {
	m := New(Options{MaxConcurrent: 2})
	now := time.Now()

	if got := m.PeerSlots(); got != 2 {
		t.Fatalf("PeerSlots() = %d, want 2", got)
	}
	for i := 0; i < 2; i++ {
		if err := m.Begin("alice", now); err != nil {
			t.Fatal(err)
		}
	}
	if limit := reasonOf(t, m.Begin("alice", now)); limit.Reason != ReasonConcurrency {
		t.Fatalf("third request refused for %q", limit.Reason)
	}
}

// TestAReserveThatSwallowsTheCapFailsClosed is the misconfiguration the host
// refuses at startup. If it is ever reached anyway, the meter must serve nobody
// rather than everybody: "no slots" must never read as "no limit".
func TestAReserveThatSwallowsTheCapFailsClosed(t *testing.T) {
	m := New(Options{MaxConcurrent: 2, OwnerReserve: 5})
	now := time.Now()

	if got := m.PeerSlots(); got != 0 {
		t.Fatalf("PeerSlots() = %d, want 0", got)
	}
	if got := m.Capacity(); got != 0 {
		t.Errorf("Capacity() = %d, want 0", got)
	}
	if limit := reasonOf(t, m.Begin("alice", now)); limit.Reason != ReasonConcurrency {
		t.Fatalf("a request was refused for %q, want concurrency", limit.Reason)
	}
	if got := m.InFlight(); got != 0 {
		t.Errorf("InFlight() = %d, want 0: nothing may be admitted", got)
	}
}

func TestNoCapStillMeansNoCap(t *testing.T) {
	m := New(Options{MaxConcurrent: 0, OwnerReserve: 1})
	now := time.Now()

	// Zero here means "uncapped and therefore unknown", not "no slots".
	if got := m.PeerSlots(); got != 0 {
		t.Errorf("PeerSlots() = %d, want 0 (uncapped)", got)
	}
	if got := m.Capacity(); got != 0 {
		t.Errorf("Capacity() = %d, want 0 (unknown)", got)
	}
	for i := 0; i < 20; i++ {
		if err := m.Begin("alice", now); err != nil {
			t.Fatalf("request %d refused with no cap set: %v", i+1, err)
		}
	}
}

func TestQuotaIsASustainedBudgetNotARate(t *testing.T) {
	m := New(Options{MaxConcurrent: 10, PeerQuota: Quota{Requests: 2, Window: time.Hour}})
	now := time.Now()

	for i := 0; i < 2; i++ {
		if err := m.Begin("alice", now); err != nil {
			t.Fatalf("request %d of the budget refused: %v", i+1, err)
		}
		m.End("alice", Usage{}, false, 0, now)
	}

	limit := reasonOf(t, m.Begin("alice", now))
	if limit.Reason != ReasonQuota {
		t.Fatalf("refused for %q, want quota", limit.Reason)
	}
	if limit.Quota != 2 || limit.Window != time.Hour {
		t.Errorf("refusal says %d per %s, want 2 per 1h0m0s", limit.Quota, limit.Window)
	}
	if limit.RetryAfter <= 0 || limit.RetryAfter > time.Hour {
		t.Errorf("RetryAfter = %s, want something up to an hour", limit.RetryAfter)
	}
	// The message has to be usable by whoever is looking at a peer's logs.
	for _, want := range []string{"budget of 2 requests", "retry in"} {
		if !strings.Contains(limit.Error(), want) {
			t.Errorf("refusal %q does not mention %q", limit, want)
		}
	}
}

func TestQuotaWindowTurnsOver(t *testing.T) {
	m := New(Options{MaxConcurrent: 10, PeerQuota: Quota{Requests: 1, Window: time.Hour}})
	start := time.Now()

	if err := m.Begin("alice", start); err != nil {
		t.Fatal(err)
	}
	m.End("alice", Usage{}, false, 0, start)
	if err := m.Begin("alice", start.Add(time.Minute)); err == nil {
		t.Fatal("the budget was refilled before the window turned over")
	}

	// The window is the peer's own hour, so it starts when they started.
	if err := m.Begin("alice", start.Add(time.Hour+time.Second)); err != nil {
		t.Fatalf("the budget did not come back after the window: %v", err)
	}
	if used := peerRow(t, m, "alice").QuotaUsed; used != 1 {
		t.Errorf("QuotaUsed = %d after the window turned over, want 1", used)
	}
}

func TestQuotaIsPerPeer(t *testing.T) {
	m := New(Options{MaxConcurrent: 10, PeerQuota: Quota{Requests: 1, Window: time.Hour}})
	now := time.Now()

	if err := m.Begin("alice", now); err != nil {
		t.Fatal(err)
	}
	m.End("alice", Usage{}, false, 0, now)
	if limit := reasonOf(t, m.Begin("alice", now)); limit.Reason != ReasonQuota {
		t.Fatalf("alice was refused for %q, want quota", limit.Reason)
	}
	if err := m.Begin("bob", now); err != nil {
		t.Fatalf("bob was punished for alice's usage: %v", err)
	}
}

// TestARefusalDoesNotSpendAnotherLimitsAllowance pins the two-phase Begin. A peer
// turned away for one reason must not also lose part of a different budget for a
// request that was never served.
func TestARefusalDoesNotSpendAnotherLimitsAllowance(t *testing.T) {
	t.Run("a rate refusal does not spend budget", func(t *testing.T) {
		m := New(Options{MaxConcurrent: 10, RequestsPerMinute: 1, PeerQuota: Quota{Requests: 5, Window: time.Hour}})
		now := time.Now()

		if err := m.Begin("alice", now); err != nil {
			t.Fatal(err)
		}
		m.End("alice", Usage{}, false, 0, now)

		if limit := reasonOf(t, m.Begin("alice", now)); limit.Reason != ReasonRate {
			t.Fatalf("refused for %q, want rate", limit.Reason)
		}
		if used := peerRow(t, m, "alice").QuotaUsed; used != 1 {
			t.Errorf("QuotaUsed = %d after a rate refusal, want 1", used)
		}
	})

	t.Run("a concurrency refusal does not spend budget", func(t *testing.T) {
		m := New(Options{MaxConcurrent: 1, PeerQuota: Quota{Requests: 2, Window: time.Hour}})
		now := time.Now()

		if err := m.Begin("alice", now); err != nil {
			t.Fatal(err)
		}
		if limit := reasonOf(t, m.Begin("alice", now)); limit.Reason != ReasonConcurrency {
			t.Fatalf("refused for %q, want concurrency", limit.Reason)
		}
		m.End("alice", Usage{}, false, 0, now)

		// Two requests of budget, one used. If the concurrency refusal had spent
		// the second, this would come back as a quota refusal.
		if err := m.Begin("alice", now); err != nil {
			t.Fatalf("a refusal spent the peer's budget: %v", err)
		}
		if used := peerRow(t, m, "alice").QuotaUsed; used != 2 {
			t.Errorf("QuotaUsed = %d, want 2", used)
		}
	})
}

func TestUsageReportsTheBudgetSoItCanBeToldFromACrash(t *testing.T) {
	now := time.Now()

	withQuota := New(Options{MaxConcurrent: 10, PeerQuota: Quota{Requests: 3, Window: time.Hour}})
	if err := withQuota.Begin("alice", now); err != nil {
		t.Fatal(err)
	}
	withQuota.End("alice", Usage{}, false, 0, now)

	row := peerRow(t, withQuota, "alice")
	if row.QuotaUsed != 1 {
		t.Errorf("QuotaUsed = %d, want 1", row.QuotaUsed)
	}
	reset, err := time.Parse(time.RFC3339, row.QuotaReset)
	if err != nil {
		t.Fatalf("QuotaReset = %q, which is not a timestamp: %v", row.QuotaReset, err)
	}
	if !reset.After(now) || reset.After(now.Add(time.Hour+time.Minute)) {
		t.Errorf("QuotaReset = %s, which is not inside this window", reset)
	}

	withoutQuota := New(Options{MaxConcurrent: 10})
	if err := withoutQuota.Begin("alice", now); err != nil {
		t.Fatal(err)
	}
	withoutQuota.End("alice", Usage{}, false, 0, now)
	row = peerRow(t, withoutQuota, "alice")
	if row.QuotaUsed != 0 || row.QuotaReset != "" {
		t.Errorf("a host with no budget reported one: %+v", row)
	}
}

// TestARefusalOpensNoWindow: a peer who was never served has a row, because a
// refusal is worth counting, but no budget window — so they are not reported as
// being part-way through one.
func TestARefusalOpensNoWindow(t *testing.T) {
	m := New(Options{MaxConcurrent: 1, PeerQuota: Quota{Requests: 3, Window: time.Hour}})
	now := time.Now()

	if err := m.Begin("alice", now); err != nil {
		t.Fatal(err)
	}
	if limit := reasonOf(t, m.Begin("bob", now)); limit.Reason != ReasonConcurrency {
		t.Fatalf("bob was refused for %q, want concurrency", limit.Reason)
	}

	if row := peerRow(t, m, "bob"); row.QuotaUsed != 0 || row.QuotaReset != "" {
		t.Errorf("a peer who was never served reported a budget window: %+v", row)
	}
	if row := peerRow(t, m, "alice"); row.QuotaUsed != 1 || row.QuotaReset == "" {
		t.Errorf("alice's admitted request did not open a window: %+v", row)
	}
}
