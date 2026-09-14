package registry

import (
	"testing"
	"time"
)

func TestListFiltersByModel(t *testing.T) {
	s := NewStore(time.Minute)
	s.Register([]Entry{
		{Model: "llama3.1:8b", Address: "a:1", Digest: "sha256:1"},
		{Model: "qwen2.5:7b", Address: "b:2", Digest: "sha256:2"},
	})

	got := s.List("llama3.1")
	if len(got) != 1 || got[0].Address != "a:1" {
		t.Fatalf("List(llama3.1) = %+v, want only a:1", got)
	}
	if n := len(s.List("")); n != 2 {
		t.Fatalf("List(\"\") returned %d entries, want 2", n)
	}
}

// A host that stops heartbeating must fall out of the list, or clients keep
// dialing machines that went to sleep.
func TestEntriesExpireWithoutHeartbeat(t *testing.T) {
	s := NewStore(30 * time.Second)
	now := time.Now()
	s.now = func() time.Time { return now }
	s.Register([]Entry{{Model: "m", Address: "a:1"}})

	now = now.Add(10 * time.Second)
	if n := len(s.List("")); n != 1 {
		t.Fatalf("after 10s there were %d entries, want 1", n)
	}

	now = now.Add(25 * time.Second) // 35s, past the 30s TTL
	if n := len(s.List("")); n != 0 {
		t.Fatalf("after 35s there were %d entries, want 0", n)
	}
	if n := s.Len(); n != 0 {
		t.Fatalf("Len() = %d, want 0 once expired", n)
	}
}

func TestReRegisterRefreshesInsteadOfDuplicating(t *testing.T) {
	s := NewStore(time.Minute)
	now := time.Now()
	s.now = func() time.Time { return now }
	s.Register([]Entry{{Model: "m", Address: "a:1", Digest: "sha256:old"}})

	now = now.Add(30 * time.Second)
	s.Register([]Entry{{Model: "m", Address: "a:1", Digest: "sha256:new"}})

	if n := s.Len(); n != 1 {
		t.Fatalf("Len() = %d, want 1: a heartbeat must not duplicate the entry", n)
	}
	if got := s.List("")[0].Digest; got != "sha256:new" {
		t.Fatalf("digest = %q, want the refreshed value", got)
	}
}

func TestUnusableEntriesAreDropped(t *testing.T) {
	s := NewStore(time.Minute)
	n := s.Register([]Entry{
		{Model: "no-address"},
		{Address: "no-model:1"},
		{Model: "ok", Address: "a:2"},
	})
	if n != 1 {
		t.Fatalf("Register accepted %d entries, want 1", n)
	}
}

func TestListPrefersHostWithFreeCapacity(t *testing.T) {
	s := NewStore(time.Minute)
	s.Register([]Entry{
		{Model: "m", Address: "busy:1", Host: "busy"},
		{Model: "m", Address: "free:1", Host: "free", Capacity: 4},
	})
	if got := s.List("m")[0].Address; got != "free:1" {
		t.Fatalf("first entry = %q, want the host with free capacity", got)
	}
}
