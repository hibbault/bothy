// Package registry holds the discovery record type, the in-memory TTL store
// behind the discovery service, and the client that hosts and clients use to
// talk to it.
package registry

import (
	"sort"
	"sync"
	"time"

	"github.com/hibbault/bothy/internal/model"
)

// Entry is one registration: a host offering a model at an address.
//
// Address is opaque to everyone except the client that dials it — today a
// host:port, later whatever the transport needs. The share key is deliberately
// absent, because the registry is public and keys travel out of band.
type Entry struct {
	Model    string    `json:"model"`
	Digest   string    `json:"digest"`
	Address  string    `json:"address"`
	Host     string    `json:"host,omitempty"`
	Capacity int       `json:"capacity,omitempty"`
	LastSeen time.Time `json:"last_seen"`
}

func (e Entry) key() string { return e.Model + "\x00" + e.Address }

// Store is the in-memory registry.
//
// Registrations double as heartbeats: there is no separate liveness call. A host
// that stops re-registering expires, so clients stop dialing machines that went
// to sleep instead of hanging on them.
type Store struct {
	mu      sync.Mutex
	ttl     time.Duration
	now     func() time.Time
	entries map[string]Entry
}

// NewStore returns a store whose entries expire after ttl.
func NewStore(ttl time.Duration) *Store {
	return &Store{ttl: ttl, now: time.Now, entries: make(map[string]Entry)}
}

// TTL reports how long an entry survives without a heartbeat.
func (s *Store) TTL() time.Duration { return s.ttl }

// Register inserts or refreshes entries, stamping each with the current time.
// It returns how many were accepted; entries without a model or address are
// dropped rather than stored as unusable rows.
func (s *Store) Register(entries []Entry) int {
	s.mu.Lock()
	defer s.mu.Unlock()
	now := s.now()
	n := 0
	for _, e := range entries {
		if e.Model == "" || e.Address == "" {
			continue
		}
		e.LastSeen = now
		s.entries[e.key()] = e
		n++
	}
	return n
}

// List returns live entries ordered so a client can take the first one: most
// free capacity first, then by host and model for a stable order. An empty name
// returns every live entry. Expired entries are dropped as they are seen.
func (s *Store) List(name string) []Entry {
	s.mu.Lock()
	defer s.mu.Unlock()
	now := s.now()
	out := make([]Entry, 0, len(s.entries))
	for k, e := range s.entries {
		if now.Sub(e.LastSeen) > s.ttl {
			delete(s.entries, k)
			continue
		}
		if !model.Matches(name, e.Model) {
			continue
		}
		out = append(out, e)
	}
	sort.Slice(out, func(i, j int) bool {
		if out[i].Capacity != out[j].Capacity {
			return out[i].Capacity > out[j].Capacity
		}
		if out[i].Host != out[j].Host {
			return out[i].Host < out[j].Host
		}
		return out[i].Model < out[j].Model
	})
	return out
}

// Len reports how many entries are currently live.
func (s *Store) Len() int {
	s.mu.Lock()
	defer s.mu.Unlock()
	now := s.now()
	n := 0
	for _, e := range s.entries {
		if now.Sub(e.LastSeen) <= s.ttl {
			n++
		}
	}
	return n
}
