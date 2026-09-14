package httpx

import (
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
)

// Every one of the four roles ends its life through this function on SIGTERM, and
// `docker compose stop` relies on it. Nothing tested it, which meant a change
// that made shutdown hang would have looked green everywhere.
func TestServeServesThenShutsDownOnCancel(t *testing.T) {
	l, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	addr := l.Addr().String()
	l.Close() // hand the port to Serve; a small race, and not a flaky one in practice

	log := slog.New(slog.NewTextHandler(io.Discard, nil))
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	done := make(chan error, 1)
	go func() {
		done <- Serve(ctx, addr, http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
			JSON(w, http.StatusOK, map[string]any{"ok": true})
		}), log)
	}()

	client := &http.Client{Timeout: 2 * time.Second}
	var body string
	for i := 0; i < 100; i++ {
		resp, err := client.Get("http://" + addr + "/")
		if err == nil {
			b, _ := io.ReadAll(resp.Body)
			resp.Body.Close()
			body = string(b)
			break
		}
		time.Sleep(20 * time.Millisecond)
	}
	if !strings.Contains(body, `"ok": true`) {
		t.Fatalf("the server never answered, so this proves nothing: last body %q", body)
	}

	cancel()
	select {
	case err := <-done:
		if err != nil {
			t.Errorf("Serve returned %v, want nil after a graceful shutdown", err)
		}
	case <-time.After(8 * time.Second):
		t.Fatal("Serve did not return within 8s of its context being cancelled")
	}

	if resp, err := client.Get("http://" + addr + "/"); err == nil {
		resp.Body.Close()
		t.Error("the server still accepts connections after shutting down")
	}
}

// A role that cannot bind has to fail with the reason rather than start up and
// sit there. Four containers are launched together by compose, and a process
// that swallowed a bind error would look exactly like a service that is up and
// idle — including to its own healthcheck-less siblings, which would then wait
// on an address nobody is listening to.
func TestServeReturnsTheBindErrorInsteadOfHanging(t *testing.T) {
	// Hold the port, so Serve's own bind is the one that fails.
	held, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer held.Close()
	addr := held.Addr().String()

	log := slog.New(slog.NewTextHandler(io.Discard, nil))
	done := make(chan error, 1)
	go func() {
		done <- Serve(context.Background(), addr, http.NotFoundHandler(), log)
	}()

	select {
	case err := <-done:
		if err == nil {
			t.Fatal("Serve returned nil after failing to bind, so nothing would ever be retried or reported")
		}
		if !strings.Contains(err.Error(), "address already in use") {
			t.Errorf("error %q does not say the port was taken", err)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("Serve neither bound nor failed within 5s on a port that was already taken")
	}
}

// An empty token means open, which callers are supposed to warn about; a set
// token means every route behind it is closed.
func TestRequireToken(t *testing.T) {
	handler := RequireToken("secret", http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		JSON(w, http.StatusOK, map[string]any{"ok": true})
	}))

	for _, tc := range []struct {
		name       string
		header     string
		value      string
		wantStatus int
	}{
		{"the key header", KeyHeader, "secret", http.StatusOK},
		{"a bearer token", "Authorization", "Bearer secret", http.StatusOK},
		{"no credential", "", "", http.StatusUnauthorized},
		{"the wrong key", KeyHeader, "wrong", http.StatusUnauthorized},
	} {
		t.Run(tc.name, func(t *testing.T) {
			req, err := http.NewRequest(http.MethodGet, "http://host/", nil)
			if err != nil {
				t.Fatal(err)
			}
			if tc.header != "" {
				req.Header.Set(tc.header, tc.value)
			}
			rec := httptest.NewRecorder()
			handler.ServeHTTP(rec, req)
			if rec.Code != tc.wantStatus {
				t.Errorf("status = %d, want %d", rec.Code, tc.wantStatus)
			}
		})
	}

	if got := RequireToken("", http.HandlerFunc(func(http.ResponseWriter, *http.Request) {})); got == nil {
		t.Error("an empty token should leave the handler in place rather than wrap it")
	}
}

// Errors are deliberately OpenAI-shaped so that a client which already parses
// error.message shows something instead of a blank failure.
func TestErrorIsOpenAIShaped(t *testing.T) {
	rec := httptest.NewRecorder()
	Error(rec, http.StatusTooManyRequests, "slow down")

	if rec.Code != http.StatusTooManyRequests {
		t.Errorf("status = %d, want 429", rec.Code)
	}
	if ct := rec.Header().Get("Content-Type"); ct != "application/json" {
		t.Errorf("Content-Type = %q, want application/json", ct)
	}
	var body struct {
		Error struct {
			Message string `json:"message"`
			Type    string `json:"type"`
		} `json:"error"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
		t.Fatalf("body is not JSON: %v (%q)", err, rec.Body)
	}
	if body.Error.Message != "slow down" || body.Error.Type != "bothy_error" {
		t.Errorf("body = %+v, want the message under error.message", body)
	}
}

func TestTokenFromPrefersTheKeyHeader(t *testing.T) {
	req, err := http.NewRequest(http.MethodGet, "http://host/", nil)
	if err != nil {
		t.Fatal(err)
	}
	req.Header.Set(KeyHeader, " key ")
	req.Header.Set("Authorization", "Bearer other")
	if got := TokenFrom(req, KeyHeader); got != "key" {
		t.Errorf("TokenFrom = %q, want the trimmed key header", got)
	}
	req.Header.Del(KeyHeader)
	if got := TokenFrom(req, KeyHeader); got != "other" {
		t.Errorf("TokenFrom = %q, want the bearer fallback", got)
	}
}
