package host

import (
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/hibbault/bothy/internal/httpx"
)

func TestParsePeers(t *testing.T) {
	p, err := parsePeers("alice:key-one,bob:key-two")
	if err != nil {
		t.Fatal(err)
	}
	if got, ok := p.byKey["key-one"]; !ok || got != "alice" {
		t.Fatalf("key-one resolved to %q, %v", got, ok)
	}
	if len(p.byKey) != 2 {
		t.Fatalf("parsed %d keys, want 2", len(p.byKey))
	}
	if p.open() {
		t.Fatal("a configured host is not open")
	}
}

// Keys may contain colons, so the name is only everything before the first one.
func TestParsePeersAllowsColonsInKeys(t *testing.T) {
	p, err := parsePeers("alice:sk-abc:def")
	if err != nil {
		t.Fatal(err)
	}
	if got := p.byKey["sk-abc:def"]; got != "alice" {
		t.Fatalf("resolved %q, want alice", got)
	}
}

func TestParsePeersRejectsBadSpecs(t *testing.T) {
	for _, spec := range []string{"alice", "alice:", ":key", "alice:a,alice:b,alice:b"} {
		if _, err := parsePeers(spec); err == nil {
			t.Errorf("parsePeers(%q) succeeded, want an error", spec)
		}
	}
}

func TestResolvePeersFallsBackToTheSingleKey(t *testing.T) {
	p, err := resolvePeers("", "just-one")
	if err != nil {
		t.Fatal(err)
	}
	if p.open() {
		t.Fatal("a single key should still require that key")
	}
	if got := p.byKey["just-one"]; got != "default" {
		t.Fatalf("single key attributed to %q, want default", got)
	}
}

func request(key string) *http.Request {
	r := httptest.NewRequest(http.MethodGet, "/v1/models", nil)
	if key != "" {
		r.Header.Set(httpx.KeyHeader, key)
	}
	return r
}

func TestPeersAcceptsHeaderAndBearer(t *testing.T) {
	p, err := parsePeers("alice:key-one")
	if err != nil {
		t.Fatal(err)
	}
	if name, ok := p.resolve(request("key-one")); !ok || name != "alice" {
		t.Fatalf("resolve with X-Bothy-Key = %q, %v", name, ok)
	}

	r := httptest.NewRequest(http.MethodGet, "/v1/models", nil)
	r.Header.Set("Authorization", "Bearer key-one")
	if name, ok := p.resolve(r); !ok || name != "alice" {
		t.Fatalf("resolve with Bearer = %q, %v", name, ok)
	}
}

func TestPeersRejectsUnknownKeys(t *testing.T) {
	p, err := parsePeers("alice:key-one,bob:key-two")
	if err != nil {
		t.Fatal(err)
	}
	for _, key := range []string{"", "wrong", "key-on", "key-oneX", "keytwo"} {
		if _, ok := p.resolve(request(key)); ok {
			t.Errorf("resolve(%q) was accepted, want refusal", key)
		}
	}
}

// Header values legitimately carry optional whitespace, so trimming it is
// correct — and it cannot help an attacker, because whitespace can only make a
// correct key match, never a wrong one.
func TestPeersToleratesHeaderWhitespace(t *testing.T) {
	p, err := parsePeers("alice:key-one")
	if err != nil {
		t.Fatal(err)
	}
	if name, ok := p.resolve(request("  key-one  ")); !ok || name != "alice" {
		t.Fatalf("resolve with padded whitespace = %q, %v; want alice", name, ok)
	}
}

// An open host still needs a peer identity, or its limits would apply to
// everybody at once and its usage report would be one anonymous row.
func TestOpenHostMetersByAddress(t *testing.T) {
	p, err := resolvePeers("", "")
	if err != nil {
		t.Fatal(err)
	}
	if !p.open() {
		t.Fatal("a host with no keys is open")
	}
	r := httptest.NewRequest(http.MethodGet, "/v1/models", nil)
	r.RemoteAddr = "203.0.113.7:54321"
	if name, ok := p.resolve(r); !ok || name != "addr:203.0.113.7" {
		t.Fatalf("resolve = %q, %v; want the caller's address", name, ok)
	}
}

func TestPeerContextRoundTrip(t *testing.T) {
	ctx := withPeer(httptest.NewRequest(http.MethodGet, "/", nil).Context(), "alice")
	if got := peerFrom(ctx); got != "alice" {
		t.Fatalf("peerFrom = %q, want alice", got)
	}
	if got := peerFrom(httptest.NewRequest(http.MethodGet, "/", nil).Context()); got != "" {
		t.Fatalf("peerFrom on a bare context = %q, want empty", got)
	}
}
