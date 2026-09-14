package client

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"sync/atomic"
	"testing"

	"github.com/hibbault/bothy/internal/httpx"
	"github.com/hibbault/bothy/internal/model"
	"github.com/hibbault/bothy/internal/registry"
)

func capturedLog() (*slog.Logger, *bytes.Buffer) {
	var buf bytes.Buffer
	return slog.New(slog.NewTextHandler(&buf, nil)), &buf
}

// hostOffering is a host that answers /bothy/models, which is the route a client
// pointed straight at an address reads to learn what it can verify.
func hostOffering(t *testing.T, models []model.Model) *httptest.Server {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/bothy/models" {
			http.NotFound(w, r)
			return
		}
		httpx.JSON(w, http.StatusOK, map[string]any{"models": models})
	}))
	t.Cleanup(srv.Close)
	return srv
}

func TestFetchModelsReadsTheHostsListWithTheShareKey(t *testing.T) {
	var gotKey string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotKey = r.Header.Get(httpx.KeyHeader)
		httpx.JSON(w, http.StatusOK, map[string]any{"models": []model.Model{
			{Name: "llama3.1:8b", Digest: digestA},
		}})
	}))
	defer srv.Close()

	c := New(Config{ShareKey: "peer-key"}, quietLog())
	models, err := c.fetchModels(context.Background(), srv.URL)
	if err != nil {
		t.Fatalf("fetchModels: %v", err)
	}
	if len(models) != 1 || models[0].Digest != digestA {
		t.Fatalf("models = %+v", models)
	}
	if gotKey != "peer-key" {
		t.Errorf("%s = %q, want the share key the host expects", httpx.KeyHeader, gotKey)
	}
}

func TestFetchModelsReportsWhatWentWrong(t *testing.T) {
	t.Run("a refusal", func(t *testing.T) {
		srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
			httpx.Error(w, http.StatusUnauthorized, "nope")
		}))
		defer srv.Close()
		_, err := New(Config{}, quietLog()).fetchModels(context.Background(), srv.URL)
		if err == nil {
			t.Fatal("a 401 was read as an empty model list")
		}
		if !strings.Contains(err.Error(), "/bothy/models") {
			t.Errorf("error %q does not say which route failed", err)
		}
	})

	t.Run("a body that is not the documented shape", func(t *testing.T) {
		srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
			_, _ = io.WriteString(w, `{"models":"nope"}`)
		}))
		defer srv.Close()
		if _, err := New(Config{}, quietLog()).fetchModels(context.Background(), srv.URL); err == nil {
			t.Fatal("an undecodable body was accepted")
		}
	})

	t.Run("a host that is not there", func(t *testing.T) {
		srv := hostOffering(t, nil)
		addr := srv.URL
		srv.Close()
		if _, err := New(Config{}, quietLog()).fetchModels(context.Background(), addr); err == nil {
			t.Fatal("a refused connection was accepted")
		}
	})
}

// A direct address is the two-friend setup, and it still ends up with a
// verifiable digest — without discovery being involved at all.
func TestResolveAgainstADirectHostLearnsTheDigest(t *testing.T) {
	srv := hostOffering(t, []model.Model{
		{Name: "qwen2.5:7b", Digest: digestB},
		{Name: "llama3.1:8b", Digest: digestA},
	})

	entry, err := New(Config{HostAddress: strings.TrimPrefix(srv.URL, "http://"), Model: "llama3.1:8b"}, quietLog()).resolve(context.Background())
	if err != nil {
		t.Fatalf("resolve: %v", err)
	}
	if entry.Model != "llama3.1:8b" || entry.Digest != digestA {
		t.Errorf("entry = %+v, want the offered model and its digest", entry)
	}
	if entry.Address == "" {
		t.Error("entry has no address to dial")
	}
}

func TestResolveAgainstADirectHostWithNoModelTakesTheFirstOffered(t *testing.T) {
	srv := hostOffering(t, []model.Model{{Name: "qwen2.5:7b", Digest: digestB}})
	entry, err := New(Config{HostAddress: strings.TrimPrefix(srv.URL, "http://")}, quietLog()).resolve(context.Background())
	if err != nil {
		t.Fatalf("resolve: %v", err)
	}
	if entry.Model != "qwen2.5:7b" || entry.Digest != digestB {
		t.Errorf("entry = %+v, want the only model offered", entry)
	}
}

// A host that is up but silent about its models is still usable, because the
// person pointed at it on purpose. What must not happen is a made-up digest: the
// entry keeps the requested name and no digest, and the log says why.
func TestResolveKeepsGoingWhenTheHostsModelListCannotBeRead(t *testing.T) {
	dead := hostOffering(t, nil)
	addr := strings.TrimPrefix(dead.URL, "http://")
	dead.Close()

	log, buf := capturedLog()
	entry, err := New(Config{HostAddress: addr, Model: "llama3.1:8b"}, log).resolve(context.Background())
	if err != nil {
		t.Fatalf("a direct host that cannot be read should still resolve: %v", err)
	}
	if entry.Model != "llama3.1:8b" {
		t.Errorf("entry.Model = %q, want the requested model", entry.Model)
	}
	if entry.Digest != "" {
		t.Errorf("entry.Digest = %q, want empty: a digest nobody reported cannot be invented", entry.Digest)
	}
	if !strings.Contains(buf.String(), "cannot read the host's model list") {
		t.Errorf("nothing warned that the digest could not be checked: %s", buf.String())
	}
}

func TestResolveReportsUsableFailures(t *testing.T) {
	t.Run("nothing to connect to", func(t *testing.T) {
		_, err := New(Config{}, quietLog()).resolve(context.Background())
		if err == nil {
			t.Fatal("a client with no host and no registry resolved anyway")
		}
		if !strings.Contains(err.Error(), "-host") || !strings.Contains(err.Error(), "-discovery-url") {
			t.Errorf("error %q does not say what to set", err)
		}
	})

	t.Run("an address that is not a URL", func(t *testing.T) {
		_, err := New(Config{HostAddress: "http://[::1"}, quietLog()).resolve(context.Background())
		if err == nil {
			t.Fatal("an unparseable host address was accepted")
		}
		if !strings.Contains(err.Error(), "http://[::1") {
			t.Errorf("error %q does not quote the address", err)
		}
	})
}

func TestResolveReportsAnEmptyRegistry(t *testing.T) {
	reg := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		httpx.JSON(w, http.StatusOK, map[string]any{"entries": []any{}})
	}))
	defer reg.Close()

	if _, err := New(Config{DiscoveryURL: reg.URL}, quietLog()).resolve(context.Background()); err == nil {
		t.Error("an empty registry with no model requested resolved to nothing")
	} else if !strings.Contains(err.Error(), "no hosts are registered") {
		t.Errorf("error %q should say the registry is empty", err)
	}

	_, err := New(Config{DiscoveryURL: reg.URL, Model: "llama3.1:8b"}, quietLog()).resolve(context.Background())
	if err == nil {
		t.Error("an empty registry resolved a host for a named model")
	} else if !strings.Contains(err.Error(), "llama3.1:8b") {
		t.Errorf("error %q should name the model nobody has", err)
	}
}

func TestResolvePropagatesARegistryFailure(t *testing.T) {
	reg := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		httpx.Error(w, http.StatusServiceUnavailable, "down")
	}))
	defer reg.Close()

	if _, err := New(Config{DiscoveryURL: reg.URL, Model: "m"}, quietLog()).resolve(context.Background()); err == nil {
		t.Fatal("a failing registry was treated as an empty one")
	}
}

// The client holds the host it resolved — that is what keeps a request from
// re-looking-up on every call — and re-resolves only once it has been
// invalidated, which is the promise re-resolution rests on.
func TestEnsureTargetResolvesOnceAndAgainAfterInvalidation(t *testing.T) {
	var asked int32
	reg := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		atomic.AddInt32(&asked, 1)
		httpx.JSON(w, http.StatusOK, map[string]any{
			"entries": []map[string]any{{
				"model": "llama3.1:8b", "digest": digestA, "address": "box:7777",
			}},
		})
	}))
	defer reg.Close()

	c := New(Config{DiscoveryURL: reg.URL, Model: "llama3.1:8b"}, quietLog())
	for i := 0; i < 3; i++ {
		target, err := c.ensureTarget(context.Background())
		if err != nil {
			t.Fatalf("ensureTarget: %v", err)
		}
		if target.Host != "box:7777" {
			t.Fatalf("target = %v", target)
		}
	}
	if n := atomic.LoadInt32(&asked); n != 1 {
		t.Errorf("the registry was asked %d times, want 1 while the host is held", n)
	}

	c.invalidate()
	if _, err := c.ensureTarget(context.Background()); err != nil {
		t.Fatal(err)
	}
	if n := atomic.LoadInt32(&asked); n != 2 {
		t.Errorf("the registry was asked %d times after invalidating, want 2", n)
	}
}

// A pinned digest that cannot match is fatal, and must not be replaced by a
// half-resolved target: serving requests that can never succeed would hide the
// misconfiguration until somebody read a log.
func TestEnsureTargetRefusesAMismatch(t *testing.T) {
	reg := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		httpx.JSON(w, http.StatusOK, map[string]any{
			"entries": []map[string]any{{
				"model": "llama3.1:8b", "digest": digestB, "address": "box:7777",
			}},
		})
	}))
	defer reg.Close()

	c := New(Config{DiscoveryURL: reg.URL, Model: "llama3.1:8b", ExpectedDigest: digestA}, quietLog())
	_, err := c.ensureTarget(context.Background())
	var mismatch *MismatchError
	if !errors.As(err, &mismatch) {
		t.Fatalf("error = %v, want a *MismatchError", err)
	}
	if c.target != nil {
		t.Error("a host was resolved despite the mismatch")
	}
}

func TestEnsureTargetRefusesAnEntryWithNoAddress(t *testing.T) {
	reg := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		httpx.JSON(w, http.StatusOK, map[string]any{
			"entries": []map[string]any{{"model": "m", "digest": "", "address": ""}},
		})
	}))
	defer reg.Close()

	c := New(Config{DiscoveryURL: reg.URL}, quietLog())
	_, err := c.ensureTarget(context.Background())
	if err == nil {
		t.Fatal("an entry with no address was accepted")
	}
	if !strings.Contains(err.Error(), "no host") {
		t.Errorf("error %q does not say the address was unusable", err)
	}
}

// The director is where the local caller's credential is replaced by the share
// key. Getting this backwards leaks a local API key to a stranger's engine.
func TestDirectorSwapsTheLocalKeyForTheShareKey(t *testing.T) {
	target, err := url.Parse("http://box:7777")
	if err != nil {
		t.Fatal(err)
	}

	t.Run("with a share key", func(t *testing.T) {
		c := New(Config{ShareKey: "peer-key"}, quietLog())
		c.target = target
		req := httptest.NewRequest(http.MethodPost, "/v1/chat/completions", nil)
		req.Header.Set(httpx.KeyHeader, "local-api-key")
		req.Header.Set("Authorization", "Bearer local-api-key")

		c.director(req)

		if req.URL.Scheme != "http" || req.URL.Host != "box:7777" {
			t.Errorf("upstream = %s://%s, want http://box:7777", req.URL.Scheme, req.URL.Host)
		}
		if req.Host != "box:7777" {
			t.Errorf("Host = %q, want the host's own name", req.Host)
		}
		if got := req.Header.Get("Authorization"); got != "" {
			t.Errorf("Authorization = %q, want the local credential dropped", got)
		}
		if got := req.Header.Get(httpx.KeyHeader); got != "peer-key" {
			t.Errorf("%s = %q, want the share key", httpx.KeyHeader, got)
		}
	})

	t.Run("with no share key", func(t *testing.T) {
		c := New(Config{}, quietLog())
		c.target = target
		req := httptest.NewRequest(http.MethodPost, "/v1/chat/completions", nil)
		req.Header.Set(httpx.KeyHeader, "local-api-key")

		c.director(req)

		if got := req.Header.Get(httpx.KeyHeader); got != "" {
			t.Errorf("%s = %q, want no credential sent to a host that expects none", httpx.KeyHeader, got)
		}
	})

	t.Run("with nothing resolved yet", func(t *testing.T) {
		c := New(Config{}, quietLog())
		req := httptest.NewRequest(http.MethodPost, "/v1/chat/completions", nil)
		original := req.URL.String()
		c.director(req)
		if req.URL.String() != original {
			t.Errorf("URL = %s, want it untouched", req.URL)
		}
	})
}

// Both mismatch sentences are read by someone deciding whether their setup is
// wrong, so each has to name the model and say the useful thing.
func TestMismatchErrorSaysWhatIsWrong(t *testing.T) {
	err := Verify(digestA, digestB, "llama3.1:8b")
	var mismatch *MismatchError
	if !errors.As(err, &mismatch) {
		t.Fatal(err)
	}
	msg := err.Error()
	for _, want := range []string{"llama3.1:8b", digestA, digestB, "mismatch"} {
		if !strings.Contains(msg, want) {
			t.Errorf("message %q does not mention %q", msg, want)
		}
	}

	unknown := Verify(digestA, "", "llama3.1:8b").Error()
	for _, want := range []string{"llama3.1:8b", digestA, "no digest"} {
		if !strings.Contains(unknown, want) {
			t.Errorf("message %q does not mention %q", unknown, want)
		}
	}
	// The two cases have to be tellable apart: "different weights" is a host
	// problem, "no digest at all" is usually an older engine.
	if unknown == msg {
		t.Error("a host that reported no digest is described the same way as one serving different weights")
	}
}

func TestStatusBeforeAnythingIsResolved(t *testing.T) {
	c := New(Config{
		Listen:         "127.0.0.1:11434",
		DiscoveryURL:   "http://reg:8080",
		Model:          "llama3.1:8b",
		ExpectedDigest: digestA,
	}, quietLog())

	rec := httptest.NewRecorder()
	c.handleStatus(rec, httptest.NewRequest(http.MethodGet, "/bothy/status", nil))

	var status map[string]any
	if err := json.NewDecoder(rec.Body).Decode(&status); err != nil {
		t.Fatalf("status is not JSON: %v", err)
	}
	if status["connected"] != false {
		t.Errorf("connected = %#v, want false", status["connected"])
	}
	for _, key := range []string{"listening", "discovery", "requested_model", "expected_digest"} {
		if _, ok := status[key]; !ok {
			t.Errorf("status is missing %q: %+v", key, status)
		}
	}
	// Nothing is connected, so there is no host to describe — and definitely no
	// digest_verified:true.
	for _, key := range []string{"host", "digest", "digest_verified"} {
		if _, ok := status[key]; ok {
			t.Errorf("status claims %q while nothing is connected: %+v", key, status)
		}
	}
}

func TestOrUnknown(t *testing.T) {
	if got := orUnknown("  "); got != "unknown" {
		t.Errorf("orUnknown(blank) = %q, want unknown", got)
	}
	if got := orUnknown(digestA); got != digestA {
		t.Errorf("orUnknown = %q, want it unchanged", got)
	}
}

// An unset share key on a host that requires one is the most common setup
// mistake, so the entry the client resolved still has to be honest about it.
func TestResolveUsesTheRegistryOrder(t *testing.T) {
	var served int32
	first := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		atomic.AddInt32(&served, 1)
		httpx.JSON(w, http.StatusOK, map[string]any{"host": "first"})
	}))
	defer first.Close()
	second := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		httpx.JSON(w, http.StatusOK, map[string]any{"host": "second"})
	}))
	defer second.Close()

	reg := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		httpx.JSON(w, http.StatusOK, map[string]any{"entries": []registry.Entry{
			{Model: "m", Address: strings.TrimPrefix(first.URL, "http://"), Digest: digestA, Capacity: 9},
			{Model: "m", Address: strings.TrimPrefix(second.URL, "http://"), Digest: digestA, Capacity: 1},
		}})
	}))
	defer reg.Close()

	entry, err := New(Config{DiscoveryURL: reg.URL, Model: "m"}, quietLog()).resolve(context.Background())
	if err != nil {
		t.Fatalf("resolve: %v", err)
	}
	if !strings.Contains(entry.Address, strings.TrimPrefix(first.URL, "http://")) {
		t.Errorf("chose %q, want the first entry the registry put in front", entry.Address)
	}
	if n := atomic.LoadInt32(&served); n != 0 {
		t.Errorf("resolving dialed the hosts %d times; it should only pick one", n)
	}
}
