package client

import (
	"encoding/json"
	"errors"
	"log/slog"
	"net/http/httptest"
	"net/url"
	"os"
	"testing"

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
