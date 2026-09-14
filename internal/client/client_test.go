package client

import (
	"context"
	"encoding/json"
	"errors"
	"log/slog"
	"net/http/httptest"
	"net/url"
	"os"
	"strings"
	"testing"
	"time"

	"github.com/hibbault/bothy/internal/model"
	"github.com/hibbault/bothy/internal/registry"
)

const (
	digestA = "sha256:aaaa"
	digestB = "sha256:bbbb"
)

func TestVerify(t *testing.T) {
	tests := []struct {
		name     string
		expected string
		actual   string
		wantErr  bool
	}{
		{"no expectation accepts anything", "", digestB, false},
		{"no expectation accepts an unknown digest", "", "", false},
		{"matching digest", digestA, digestA, false},
		{"matching digest ignores case and prefix", "AAAA", digestA, false},
		{"different weights are refused", digestA, digestB, true},
		{"unknown digest cannot satisfy a requirement", digestA, "", true},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			err := Verify(tt.expected, tt.actual, "llama3.1:8b")
			if (err != nil) != tt.wantErr {
				t.Fatalf("Verify(%q, %q) error = %v, wantErr %v", tt.expected, tt.actual, err, tt.wantErr)
			}
		})
	}
}

// The client treats a mismatch as fatal, so it has to be a distinguishable type
// rather than an anonymous error string.
func TestVerifyReturnsMismatchForFatalHandling(t *testing.T) {
	err := Verify(digestA, digestB, "llama3.1:8b")
	var mismatch *MismatchError
	if !errors.As(err, &mismatch) {
		t.Fatalf("error = %v, want a *MismatchError", err)
	}
	if mismatch.Expected != digestA || mismatch.Actual != digestB {
		t.Fatalf("mismatch carries %+v", mismatch)
	}
}

func TestPickPrefersRequestedModel(t *testing.T) {
	models := []model.Model{
		{Name: "qwen2.5:7b", Digest: digestB},
		{Name: "llama3.1", Digest: digestA},
	}
	if m, ok := pick(models, "llama3.1:latest"); !ok || m.Digest != digestA {
		t.Fatalf("pick = %+v, %v; want llama3.1 matched as :latest", m, ok)
	}
	if m, ok := pick(models, ""); !ok || m.Name != "qwen2.5:7b" {
		t.Fatalf("pick with no name = %+v, %v; want the first model", m, ok)
	}
	if _, ok := pick(models, "mistral"); ok {
		t.Fatal("pick should not invent a model that is not offered")
	}
	if _, ok := pick(nil, "llama3.1"); ok {
		t.Fatal("pick should fail on an empty list")
	}
}
func TestWithScheme(t *testing.T) {
	if got := withScheme("box:7777"); got != "http://box:7777" {
		t.Fatalf("withScheme = %q", got)
	}
	if got := withScheme("https://box:7777"); got != "https://box:7777" {
		t.Fatalf("withScheme rewrote an explicit scheme: %q", got)
	}
}

// The status page is the first thing a new user curls after `connect`. When no
// digest was required, the connection satisfies the policy, so it must not
// report digest_verified:false on a healthy setup.
func TestStatusDigestVerified(t *testing.T) {
	tests := []struct {
		name     string
		expected string
		actual   string
		want     bool
	}{
		{"nothing required reads as verified", "", digestA, true},
		{"nothing required and nothing known reads as verified", "", "", true},
		{"matching digest reads as verified", digestA, digestA, true},
		{"different weights read as unverified", digestA, digestB, false},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			log := slog.New(slog.NewTextHandler(os.Stderr, nil))
			c := New(Config{ExpectedDigest: tt.expected}, log)
			c.target = &url.URL{Scheme: "http", Host: "box:7777"}
			c.entry = registry.Entry{Model: "llama3.1:8b", Digest: tt.actual}

			rec := httptest.NewRecorder()
			c.handleStatus(rec, httptest.NewRequest("GET", "/bothy/status", nil))

			var status map[string]any
			if err := json.NewDecoder(rec.Body).Decode(&status); err != nil {
				t.Fatalf("status is not JSON: %v", err)
			}
			if got, _ := status["digest_verified"].(bool); got != tt.want {
				t.Fatalf("digest_verified = %v, want %v (expected %q, actual %q)",
					got, tt.want, tt.expected, tt.actual)
			}
		})
	}
}

// A pinned digest the host cannot satisfy is a configuration error, not a
// transient one: no amount of waiting will fix it. A client that started anyway
// would serve an endpoint whose every request fails, with the reason arriving
// after the user has already pointed an editor at it. So connect fails the
// process and names the mismatch instead.
func TestRunRefusesAPinnedDigestTheHostCannotSatisfy(t *testing.T) {
	srv := hostOffering(t, []model.Model{{Name: "llama3.1:8b", Digest: digestB}})

	err := Run(context.Background(), quietLog(), []string{
		"-host", strings.TrimPrefix(srv.URL, "http://"),
		"-model", "llama3.1:8b",
		"-expected-digest", digestA,
		"-listen", "127.0.0.1:0",
	})
	if err == nil {
		t.Fatal("connect served a host whose weights are not the ones it was told to require")
	}
	var mismatch *MismatchError
	if !errors.As(err, &mismatch) {
		t.Fatalf("err = %v (%T), want a *MismatchError so the refusal is legible at startup", err, err)
	}
	if !strings.Contains(err.Error(), "llama3.1:8b") {
		t.Errorf("error %q does not name the model that could not be satisfied", err)
	}
}

// The other half of that rule: nothing being reachable yet must not stop the
// client from starting. Compose brings services up in whatever order it likes,
// so a client that exited because its host had not booted would be the most
// annoying possible failure — waiting is right here where waiting on a mismatch
// is not.
func TestRunServesWhenNothingIsReachableYet(t *testing.T) {
	for _, tc := range []struct {
		name string
		args []string
		why  string
	}{
		{
			name: "the host is not up yet",
			args: []string{"-host", "127.0.0.1:1"}, // nothing is listening on this port
			why:  "a direct host that cannot be read yet is not fatal",
		},
		{
			name: "the registry is not up yet",
			args: []string{"-discovery-url", "http://127.0.0.1:1"},
			why:  "a fleet that has not started is not fatal",
		},
	} {
		t.Run(tc.name, func(t *testing.T) {
			ctx, cancel := context.WithCancel(context.Background())
			defer cancel()

			args := append(append([]string{}, tc.args...),
				"-model", "llama3.1:8b", "-listen", "127.0.0.1:0")
			done := make(chan error, 1)
			go func() { done <- Run(ctx, quietLog(), args) }()

			select {
			case err := <-done:
				t.Fatalf("Run returned %v instead of serving: %s", err, tc.why)
			case <-time.After(300 * time.Millisecond):
			}

			cancel()
			select {
			case err := <-done:
				if err != nil {
					t.Errorf("Run returned %v after a graceful cancel, want nil", err)
				}
			case <-time.After(8 * time.Second):
				t.Fatal("Run did not shut down within 8s of its context being cancelled")
			}
		})
	}
}
