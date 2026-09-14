package discovery

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"log/slog"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/hibbault/bothy/internal/registry"
)

// newTestServerFor serves a Server built with its own config, so a test can set
// a TTL short enough to watch expire.
func newTestServerFor(t *testing.T, s *Server) *httptest.Server {
	t.Helper()
	srv := httptest.NewServer(s.Handler())
	t.Cleanup(srv.Close)
	return srv
}

func freeAddr(t *testing.T) string {
	t.Helper()
	l, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	addr := l.Addr().String()
	l.Close()
	return addr
}

// An open registry is the default, and it lets anyone who can reach the port
// publish entries. That has to be said out loud at startup — it is a spam problem
// rather than a security one, which is exactly the sort of thing a warning is for.
func TestRunWarnsWhenRegistrationIsOpen(t *testing.T) {
	var buf bytes.Buffer
	log := slog.New(slog.NewTextHandler(&buf, nil))
	addr := freeAddr(t)

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan error, 1)
	go func() { done <- Run(ctx, log, []string{"-listen", addr, "-ttl", "1m"}) }()

	client := &http.Client{Timeout: 2 * time.Second}
	var health map[string]any
	for i := 0; i < 100; i++ {
		resp, err := client.Get("http://" + addr + "/healthz")
		if err == nil {
			body, _ := io.ReadAll(resp.Body)
			resp.Body.Close()
			if err := json.Unmarshal(body, &health); err == nil {
				break
			}
		}
		time.Sleep(20 * time.Millisecond)
	}
	if health == nil {
		t.Fatal("the registry never answered, so this proves nothing")
	}
	if health["ttl"] != "1m0s" {
		t.Errorf("healthz ttl = %#v, want the configured 1m0s", health["ttl"])
	}
	if !strings.Contains(buf.String(), "registration is open") {
		t.Errorf("nothing warned that anyone reachable can publish entries:\n%s", buf.String())
	}

	cancel()
	select {
	case err := <-done:
		if err != nil {
			t.Errorf("Run returned %v, want nil after a graceful shutdown", err)
		}
	case <-time.After(8 * time.Second):
		t.Fatal("Run did not return after its context was cancelled")
	}
}

// With a token configured, the warning would be a lie, so it must not appear.
func TestRunDoesNotWarnWhenRegistrationIsClosed(t *testing.T) {
	var buf bytes.Buffer
	log := slog.New(slog.NewTextHandler(&buf, nil))
	addr := freeAddr(t)

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go func() { _ = Run(ctx, log, []string{"-listen", addr, "-register-token", "secret"}) }()

	client := &http.Client{Timeout: 2 * time.Second}
	up := false
	for i := 0; i < 100 && !up; i++ {
		if resp, err := client.Get("http://" + addr + "/healthz"); err == nil {
			resp.Body.Close()
			up = true
			break
		}
		time.Sleep(20 * time.Millisecond)
	}
	if !up {
		t.Fatal("the registry never answered")
	}
	if strings.Contains(buf.String(), "registration is open") {
		t.Errorf("warned that registration is open with a token set:\n%s", buf.String())
	}
}

// A typo in the listen address has to fail the process, not leave a registry that
// quietly is not there.
func TestRunRefusesAnAddressItCannotBind(t *testing.T) {
	log := slog.New(slog.NewTextHandler(io.Discard, nil))
	if err := Run(context.Background(), log, []string{"-listen", "127.0.0.1:not-a-port"}); err == nil {
		t.Fatal("an unbindable listen address was accepted")
	}
}

// A TTL that expires everything on arrival leaves a registry that answers every
// lookup with nothing while looking perfectly healthy — the kind of
// misconfiguration that should stop the process, not be discovered later.
func TestRunRefusesATTLLThatWouldServeNobody(t *testing.T) {
	log := slog.New(slog.NewTextHandler(io.Discard, nil))
	for _, ttl := range []string{"0s", "-1m"} {
		addr := freeAddr(t)
		err := Run(context.Background(), log, []string{"-listen", addr, "-ttl", ttl})
		if err == nil {
			t.Fatalf("ttl %s was accepted", ttl)
		}
		if !strings.Contains(err.Error(), "ttl") {
			t.Errorf("error %q does not name the setting", err)
		}
	}
}

// The register body is parsed into memory, so it is bounded. Without this, one
// POST is enough to make the registry allocate whatever the sender likes.
func TestRegisterRefusesAnOversizedBody(t *testing.T) {
	srv := newTestServer(t, "")
	huge := strings.Repeat("a", 1<<21) // 2 MiB, past the 1 MiB cap
	body := `{"entries":[{"model":"m","address":"` + huge + `"}]}`

	req, err := http.NewRequest(http.MethodPost, srv.URL+"/register", strings.NewReader(body))
	if err != nil {
		t.Fatal(err)
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusBadRequest {
		t.Fatalf("an oversized body got %d, want 400", resp.StatusCode)
	}

	// And nothing was stored on the way through.
	var health struct {
		LiveEntries int `json:"live_entries"`
	}
	resp2, err := http.Get(srv.URL + "/healthz")
	if err != nil {
		t.Fatal(err)
	}
	defer resp2.Body.Close()
	if err := json.NewDecoder(resp2.Body).Decode(&health); err != nil {
		t.Fatal(err)
	}
	if health.LiveEntries != 0 {
		t.Errorf("live_entries = %d, want 0 after a refused registration", health.LiveEntries)
	}
}

// A host announces several models in one call, and the answer says how many of
// them were usable — which is how a host notices it sent a broken entry.
func TestRegisterReportsAcceptedAndOffered(t *testing.T) {
	srv := newTestServer(t, "")
	body, err := json.Marshal(map[string]any{"entries": []registry.Entry{
		{Model: "a", Address: "x:1"},
		{Model: "", Address: "y:1"},
	}})
	if err != nil {
		t.Fatal(err)
	}
	resp, err := http.Post(srv.URL+"/register", "application/json", bytes.NewReader(body))
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()

	var payload struct {
		Registered int    `json:"registered"`
		Live       int    `json:"live"`
		TTL        string `json:"ttl"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&payload); err != nil {
		t.Fatal(err)
	}
	if payload.Registered != 1 || payload.Live != 1 {
		t.Errorf("response = %+v, want one of two entries accepted", payload)
	}
	if payload.TTL != "1m0s" {
		t.Errorf("ttl = %q, want the configured one", payload.TTL)
	}
}

// An expired registration must fall out of the list the service serves, not just
// out of an internal count — this is the whole anti-staleness promise.
func TestExpiredEntriesStopBeingServed(t *testing.T) {
	s := NewServer(Config{TTL: 30 * time.Millisecond}, slog.New(slog.NewTextHandler(io.Discard, nil)))
	srv := newTestServerFor(t, s)

	if code := register(t, srv.URL, "", []registry.Entry{{Model: "m", Address: "box:7777"}}); code != http.StatusOK {
		t.Fatalf("register status = %d", code)
	}
	if got := served(t, srv.URL); len(got) != 1 {
		t.Fatalf("served %d entries right after registering, want 1", len(got))
	}

	time.Sleep(60 * time.Millisecond)
	if got := served(t, srv.URL); len(got) != 0 {
		t.Errorf("served %d entries after the TTL, want 0", len(got))
	}
}

func served(t *testing.T, base string) []registry.Entry {
	t.Helper()
	resp, err := http.Get(base + "/models")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	var payload struct {
		Entries []registry.Entry `json:"entries"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&payload); err != nil {
		t.Fatal(err)
	}
	return payload.Entries
}
