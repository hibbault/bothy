package discovery

import (
	"bytes"
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/hibbault/bothy/internal/registry"
)

func newTestServer(t *testing.T, token string) *httptest.Server {
	t.Helper()
	s := NewServer(Config{TTL: time.Minute, Token: token}, slog.New(slog.NewTextHandler(io.Discard, nil)))
	srv := httptest.NewServer(s.Handler())
	t.Cleanup(srv.Close)
	return srv
}

// register posts a batch and returns the status code, closing the body.
func register(t *testing.T, base, token string, entries []registry.Entry) int {
	t.Helper()
	body, err := json.Marshal(map[string]any{"entries": entries})
	if err != nil {
		t.Fatal(err)
	}
	req, err := http.NewRequest(http.MethodPost, base+"/register", bytes.NewReader(body))
	if err != nil {
		t.Fatal(err)
	}
	req.Header.Set("Content-Type", "application/json")
	if token != "" {
		req.Header.Set("Authorization", "Bearer "+token)
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	return resp.StatusCode
}

func TestRegisterThenList(t *testing.T) {
	srv := newTestServer(t, "")
	if code := register(t, srv.URL, "", []registry.Entry{
		{Model: "llama3.1:8b", Digest: "sha256:aa", Address: "box:7777", Host: "box"},
	}); code != http.StatusOK {
		t.Fatalf("register status = %d, want 200", code)
	}

	resp, err := http.Get(srv.URL + "/models?model=llama3.1")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("models status = %d", resp.StatusCode)
	}
	var payload struct {
		Entries []registry.Entry `json:"entries"`
		Count   int              `json:"count"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&payload); err != nil {
		t.Fatal(err)
	}
	if len(payload.Entries) != 1 || payload.Entries[0].Address != "box:7777" {
		t.Fatalf("entries = %+v", payload.Entries)
	}
	if payload.Count != 1 {
		t.Fatalf("count = %d, want 1", payload.Count)
	}
}

func TestListForAnUnknownModelIsEmptyNotAnError(t *testing.T) {
	srv := newTestServer(t, "")
	if code := register(t, srv.URL, "", []registry.Entry{{Model: "llama3.1:8b", Address: "box:7777"}}); code != http.StatusOK {
		t.Fatalf("register status = %d", code)
	}
	resp, err := http.Get(srv.URL + "/models?model=mistral")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("status = %d, want 200 with an empty list", resp.StatusCode)
	}
	var payload struct {
		Entries []registry.Entry `json:"entries"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&payload); err != nil {
		t.Fatal(err)
	}
	if len(payload.Entries) != 0 {
		t.Fatalf("entries = %+v, want none", payload.Entries)
	}
}

// Without a token anyone reachable can fill the registry with junk, so the
// token has to actually be enforced.
func TestRegisterRequiresTheTokenWhenConfigured(t *testing.T) {
	srv := newTestServer(t, "secret")
	entries := []registry.Entry{{Model: "m", Address: "a:1"}}

	if code := register(t, srv.URL, "", entries); code != http.StatusUnauthorized {
		t.Fatalf("unauthenticated register status = %d, want 401", code)
	}
	if code := register(t, srv.URL, "wrong", entries); code != http.StatusUnauthorized {
		t.Fatalf("register with a bad token status = %d, want 401", code)
	}
	if code := register(t, srv.URL, "secret", entries); code != http.StatusOK {
		t.Fatalf("register with the right token status = %d, want 200", code)
	}
}

func TestRegisterRejectsAnEmptyBatch(t *testing.T) {
	srv := newTestServer(t, "")
	if code := register(t, srv.URL, "", nil); code != http.StatusBadRequest {
		t.Fatalf("status = %d, want 400", code)
	}
}

func TestHealthReportsLiveEntries(t *testing.T) {
	srv := newTestServer(t, "")
	if code := register(t, srv.URL, "", []registry.Entry{{Model: "m", Address: "a:1"}}); code != http.StatusOK {
		t.Fatalf("register status = %d", code)
	}
	resp, err := http.Get(srv.URL + "/healthz")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	var payload struct {
		OK          bool   `json:"ok"`
		LiveEntries int    `json:"live_entries"`
		TTL         string `json:"ttl"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&payload); err != nil {
		t.Fatal(err)
	}
	if !payload.OK || payload.LiveEntries != 1 || payload.TTL != "1m0s" {
		t.Fatalf("health = %+v", payload)
	}
}
