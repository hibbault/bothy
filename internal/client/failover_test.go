package client

import (
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/hibbault/bothy/internal/httpx"
)

func quietLog() *slog.Logger {
	return slog.New(slog.NewTextHandler(io.Discard, nil))
}

func hostSaying(text string) *httptest.Server {
	return httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		httpx.JSON(w, http.StatusOK, map[string]any{"answered_by": text})
	}))
}

// PROTOCOL.md promises that "re-resolution happens automatically after an
// upstream failure". That is the thing that keeps a client alive when the host it
// picked goes to sleep mid-session — a normal event on a network of home
// machines, and precisely the case nobody had a test for.
func TestClientReResolvesAfterTheHostDisappears(t *testing.T) {
	first := hostSaying("host A")
	second := hostSaying("host B")
	defer second.Close()

	// A registry that names A until A stops heartbeating, then names B, which is
	// what the real one does once an entry expires.
	var asked int32
	reg := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		url := first.URL
		if atomic.AddInt32(&asked, 1) > 1 {
			url = second.URL
		}
		httpx.JSON(w, http.StatusOK, map[string]any{
			"entries": []map[string]any{{
				"model":    "llama3.1:8b",
				"digest":   "sha256:1111",
				"address":  strings.TrimPrefix(url, "http://"),
				"host":     "somewhere",
				"capacity": 4,
			}},
		})
	}))
	defer reg.Close()

	c := New(Config{DiscoveryURL: reg.URL, Model: "llama3.1:8b", ShareKey: "k"}, quietLog())
	srv := httptest.NewServer(c.Handler())
	defer srv.Close()

	client := &http.Client{Timeout: 5 * time.Second}
	call := func() (int, string) {
		t.Helper()
		resp, err := client.Get(srv.URL + "/v1/chat/completions")
		if err != nil {
			t.Fatalf("the local endpoint failed outright: %v", err)
		}
		defer resp.Body.Close()
		body, _ := io.ReadAll(resp.Body)
		return resp.StatusCode, string(body)
	}

	if code, body := call(); code != http.StatusOK || !strings.Contains(body, "host A") {
		t.Fatalf("the first request = %d %s, want it to reach the host that was resolved", code, body)
	}

	// The host goes to sleep.
	first.Close()

	// This one is allowed to fail: the client is still holding the address it
	// resolved. A clear failure beats a hang, which is why it is asserted.
	if code, body := call(); code != http.StatusBadGateway {
		t.Errorf("a request to a dead host = %d %s, want 502", code, body)
	}

	// And this is the promise. Without re-resolution the client would be wedged
	// on a dead address for the rest of its life.
	code, body := call()
	if code != http.StatusOK || !strings.Contains(body, "host B") {
		t.Fatalf("the client did not move on after the failure: %d %s", code, body)
	}
	if n := atomic.LoadInt32(&asked); n < 2 {
		t.Errorf("the registry was asked %d times, want a fresh lookup after the failure", n)
	}
}

// A client pointed straight at an address has no registry to re-resolve against,
// so the failure has to stay a clear one rather than becoming a panic or a hang.
func TestClientWithADirectAddressFailsCleanly(t *testing.T) {
	dead := hostSaying("never reached")
	addr := strings.TrimPrefix(dead.URL, "http://")
	dead.Close()

	c := New(Config{HostAddress: addr, Model: "llama3.1:8b", ShareKey: "k"}, quietLog())
	srv := httptest.NewServer(c.Handler())
	defer srv.Close()

	resp, err := (&http.Client{Timeout: 5 * time.Second}).Get(srv.URL + "/v1/chat/completions")
	if err != nil {
		t.Fatalf("the local endpoint failed outright: %v", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusBadGateway {
		t.Errorf("status = %d, want 502 for an unreachable host", resp.StatusCode)
	}
}
