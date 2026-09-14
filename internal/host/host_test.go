package host

import (
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/hibbault/bothy/internal/engine"
	"github.com/hibbault/bothy/internal/httpx"
	"github.com/hibbault/bothy/internal/model"
)

// fakeEngine stands in for Ollama or vLLM. It records what it was sent, because
// a good half of what the host promises is about what the engine does *not* see.
type fakeEngine struct {
	*httptest.Server

	mu      sync.Mutex
	paths   []string
	headers []http.Header
	hosts   []string

	release chan struct{} // handlers wait here before replying, when set
	arrived chan struct{} // handlers announce themselves here, when set
}

func newFakeEngine(t *testing.T) *fakeEngine {
	t.Helper()
	f := &fakeEngine{}
	f.Server = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		f.mu.Lock()
		f.paths = append(f.paths, r.URL.Path)
		f.headers = append(f.headers, r.Header.Clone())
		f.hosts = append(f.hosts, r.Host)
		release, arrived := f.release, f.arrived
		f.mu.Unlock()

		if arrived != nil {
			select {
			case arrived <- struct{}{}:
			default:
			}
		}
		if release != nil {
			<-release
		}
		// A whole response with usage, which is the shape the meter reads.
		httpx.JSON(w, http.StatusOK, map[string]any{
			"choices": []any{map[string]any{"message": map[string]any{"content": "from the engine"}}},
			"usage":   map[string]any{"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7},
		})
	}))
	t.Cleanup(f.Close)
	return f
}

func (f *fakeEngine) hold(release chan struct{}) {
	f.mu.Lock()
	f.release = release
	f.mu.Unlock()
}

func (f *fakeEngine) announce(arrived chan struct{}) {
	f.mu.Lock()
	f.arrived = arrived
	f.mu.Unlock()
}

func (f *fakeEngine) seen() ([]string, []http.Header, []string) {
	f.mu.Lock()
	defer f.mu.Unlock()
	return append([]string(nil), f.paths...),
		append([]http.Header(nil), f.headers...),
		append([]string(nil), f.hosts...)
}

func testModels() []model.Model {
	return []model.Model{{Name: "llama3.1:8b", Digest: "sha256:1111"}}
}

func testLogger() *slog.Logger {
	return slog.New(slog.NewTextHandler(io.Discard, nil))
}

// newTestHost builds a host in front of an engine. The engine kind is static so
// no probe is needed and the test is only about the host.
func newTestHost(t *testing.T, cfg Config) *Host {
	t.Helper()
	if cfg.Listen == "" {
		cfg.Listen = "127.0.0.1:0"
	}
	if cfg.EngineKind == "" {
		cfg.EngineKind = "static"
	}
	if cfg.EngineURL == "" {
		cfg.EngineURL = "http://127.0.0.1:1"
	}
	if cfg.PublicAddress == "" {
		cfg.PublicAddress = "host:7777"
	}
	if cfg.Heartbeat == 0 {
		cfg.Heartbeat = time.Hour
	}
	if cfg.Engine.Static == nil {
		cfg.Engine = engine.Options{Static: testModels()}
	}
	h, err := New(cfg, testLogger())
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	return h
}

func do(h *Host, method, path, key, body string) *httptest.ResponseRecorder {
	var reader io.Reader
	if body != "" {
		reader = strings.NewReader(body)
	}
	req := httptest.NewRequest(method, path, reader)
	if key != "" {
		req.Header.Set(httpx.KeyHeader, key)
	}
	rec := httptest.NewRecorder()
	h.Handler().ServeHTTP(rec, req)
	return rec
}

// SECURITY.md and PROTOCOL.md both promise that a share key is stripped before
// the request reaches the engine. That promise lives in four lines of a Director
// closure, and until this test existed nothing checked it: a careless reorder
// would have leaked every peer's key into the engine's logs with the whole suite
// still green.
func TestTheEngineNeverSeesTheShareKey(t *testing.T) {
	for _, tc := range []struct {
		name string
		set  func(*http.Request)
	}{
		{"the key header", func(r *http.Request) { r.Header.Set(httpx.KeyHeader, "key-a") }},
		{"bearer auth", func(r *http.Request) { r.Header.Set("Authorization", "Bearer key-a") }},
	} {
		t.Run(tc.name, func(t *testing.T) {
			f := newFakeEngine(t)
			h := newTestHost(t, Config{EngineURL: f.URL, ShareKeys: "alice:key-a"})

			req := httptest.NewRequest(http.MethodPost, "/v1/chat/completions",
				strings.NewReader(`{"model":"llama3.1:8b"}`))
			tc.set(req)
			rec := httptest.NewRecorder()
			h.Handler().ServeHTTP(rec, req)

			if rec.Code != http.StatusOK {
				t.Fatalf("status %d, want 200 — the request never reached the engine: %s", rec.Code, rec.Body)
			}
			_, headers, hosts := f.seen()
			if len(headers) != 1 {
				t.Fatalf("the engine saw %d requests, want 1", len(headers))
			}
			if got := headers[0].Get(httpx.KeyHeader); got != "" {
				t.Errorf("the engine received the share key in %s: %q", httpx.KeyHeader, got)
			}
			if got := headers[0].Get("Authorization"); got != "" {
				t.Errorf("the engine received an Authorization header: %q", got)
			}
			// And it is told its own name, not ours.
			if want := strings.TrimPrefix(f.URL, "http://"); hosts[0] != want {
				t.Errorf("the engine saw Host %q, want %q", hosts[0], want)
			}
		})
	}
}

// "Set BOTHY_MAX_CONCURRENT. A GPU serialises work anyway; the cap is what stops
// one peer from occupying it indefinitely." — SECURITY.md. The limiter logic is
// tested in the meter package; this is the part that was not: that the host
// actually refuses, counts the refusal, and does not reach the engine.
func TestTheConcurrencyCapRefusesRatherThanQueues(t *testing.T) {
	f := newFakeEngine(t)
	release := make(chan struct{})
	arrived := make(chan struct{}, 4)
	f.hold(release)
	f.announce(arrived)
	// Once, so the happy path and the cleanup cannot both close the channel.
	var letGoOnce sync.Once
	letGo := func() { letGoOnce.Do(func() { close(release) }) }

	h := newTestHost(t, Config{EngineURL: f.URL, ShareKey: "k", MaxConcurrent: 2})
	srv := httptest.NewServer(h.Handler())
	// Registered in this order so the deferred letGo runs *before* srv.Close.
	// The other way round, Close waits on two requests still held at the engine,
	// and the test takes the client's whole timeout to notice.
	defer srv.Close()
	defer letGo()

	client := &http.Client{Timeout: 10 * time.Second}
	inflight := make(chan *http.Response, 2)
	for i := 0; i < 2; i++ {
		go func() {
			req, _ := http.NewRequest(http.MethodPost, srv.URL+"/v1/chat/completions",
				strings.NewReader(`{"model":"llama3.1:8b"}`))
			req.Header.Set(httpx.KeyHeader, "k")
			resp, err := client.Do(req)
			if err != nil {
				inflight <- nil
				return
			}
			inflight <- resp
		}()
	}
	// Only once both are genuinely through to the engine is the cap full.
	for i := 0; i < 2; i++ {
		select {
		case <-arrived:
		case <-time.After(5 * time.Second):
			t.Fatal("the first two requests never reached the engine")
		}
	}

	third := do(h, http.MethodPost, "/v1/chat/completions", "k", `{"model":"llama3.1:8b"}`)
	if third.Code != http.StatusTooManyRequests {
		t.Errorf("a request over the cap got %d, want 429", third.Code)
	}
	if _, headers, _ := f.seen(); len(headers) != 2 {
		t.Errorf("the engine saw %d requests, want 2 — a refused request must not be forwarded", len(headers))
	}

	usage := do(h, http.MethodGet, "/bothy/usage", "k", "")
	var report struct {
		Peers []struct {
			Peer    string `json:"peer"`
			Limited int    `json:"limited"`
		} `json:"peers"`
	}
	if err := json.Unmarshal(usage.Body.Bytes(), &report); err != nil {
		t.Fatalf("usage is not JSON: %v", err)
	}
	if len(report.Peers) != 1 || report.Peers[0].Limited != 1 {
		t.Errorf("usage = %+v, want one peer with one limited request", report.Peers)
	}

	// Let the two through and check they were served. A test that walked away
	// leaving two requests blocked at the engine would pass while proving less
	// than it looks like it proves.
	letGo()
	for i := 0; i < 2; i++ {
		select {
		case resp := <-inflight:
			if resp == nil {
				t.Error("one of the first two requests failed instead of being served")
				continue
			}
			if resp.StatusCode != http.StatusOK {
				t.Errorf("a request that was within the cap got %d, want 200", resp.StatusCode)
			}
			resp.Body.Close()
		case <-time.After(5 * time.Second):
			t.Fatal("a request that was let through never finished")
		}
	}
}

// The README calls the rate limit per-peer, which is the whole reason to have
// one: a noisy consumer should not slow anyone else down. Retry-After is part of
// that contract, since a client that knows when to come back does not hammer.
func TestTheRateLimitIsPerPeerAndSaysWhenToRetry(t *testing.T) {
	f := newFakeEngine(t)
	h := newTestHost(t, Config{
		EngineURL:         f.URL,
		ShareKeys:         "alice:key-a,bob:key-b",
		RequestsPerMinute: 1,
		MaxConcurrent:     0,
	})

	if rec := do(h, http.MethodPost, "/v1/chat/completions", "key-a", `{"model":"llama3.1:8b"}`); rec.Code != http.StatusOK {
		t.Fatalf("alice's first request got %d, want 200", rec.Code)
	}

	second := do(h, http.MethodPost, "/v1/chat/completions", "key-a", `{"model":"llama3.1:8b"}`)
	if second.Code != http.StatusTooManyRequests {
		t.Fatalf("alice's second request got %d, want 429", second.Code)
	}
	retry := second.Header().Get("Retry-After")
	secs, err := strconv.Atoi(retry)
	if err != nil || secs < 1 {
		t.Errorf("Retry-After = %q, want a positive number of seconds", retry)
	}

	// Bob is a different peer, so alice's limit must not touch him.
	if rec := do(h, http.MethodPost, "/v1/chat/completions", "key-b", `{"model":"llama3.1:8b"}`); rec.Code != http.StatusOK {
		t.Errorf("bob got %d while alice was over her limit, want 200", rec.Code)
	}
}

// Health has to stay unauthenticated or a container healthcheck needs a key, and
// everything else has to refuse without one.
func TestHealthIsOpenAndEverythingElseIsNot(t *testing.T) {
	f := newFakeEngine(t)
	h := newTestHost(t, Config{EngineURL: f.URL, ShareKey: "k"})

	health := do(h, http.MethodGet, "/bothy/healthz", "", "")
	if health.Code != http.StatusOK {
		t.Errorf("healthz without a key got %d, want 200", health.Code)
	}
	var report map[string]any
	if err := json.Unmarshal(health.Body.Bytes(), &report); err != nil {
		t.Fatalf("healthz is not JSON: %v", err)
	}
	if report["key_required"] != true {
		t.Errorf("healthz says key_required = %v, want true", report["key_required"])
	}

	for _, path := range []string{"/v1/models", "/bothy/models", "/bothy/usage"} {
		rec := do(h, http.MethodGet, path, "", "")
		if rec.Code != http.StatusUnauthorized {
			t.Errorf("%s without a key got %d, want 401", path, rec.Code)
		}
	}
}

// The two routes a client reads over the protocol, plus the meter wiring behind
// them: a proxied request has to come back countable, per peer.
func TestUsageAndModelsReportWhatTheProtocolDescribes(t *testing.T) {
	f := newFakeEngine(t)
	h := newTestHost(t, Config{EngineURL: f.URL, ShareKeys: "alice:key-a"})
	models := testModels()
	h.models.Store(&models)

	if rec := do(h, http.MethodPost, "/v1/chat/completions", "key-a", `{"model":"llama3.1:8b"}`); rec.Code != http.StatusOK {
		t.Fatalf("the proxied request got %d, want 200", rec.Code)
	}

	var served struct {
		Host     string        `json:"host"`
		Address  string        `json:"address"`
		Capacity int           `json:"capacity"`
		Models   []model.Model `json:"models"`
	}
	rec := do(h, http.MethodGet, "/bothy/models", "key-a", "")
	if rec.Code != http.StatusOK {
		t.Fatalf("/bothy/models got %d, want 200", rec.Code)
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &served); err != nil {
		t.Fatalf("/bothy/models is not the documented shape: %v", err)
	}
	if served.Address != "host:7777" || len(served.Models) != 1 || served.Models[0].Digest != "sha256:1111" {
		t.Errorf("/bothy/models = %+v, want the address and the models with digests", served)
	}

	var usage struct {
		MaxConcurrent int `json:"max_concurrent"`
		Peers         []struct {
			Peer             string `json:"peer"`
			Requests         int    `json:"requests"`
			PromptTokens     int    `json:"prompt_tokens"`
			CompletionTokens int    `json:"completion_tokens"`
			Unmetered        int    `json:"unmetered_responses"`
		} `json:"peers"`
	}
	rec = do(h, http.MethodGet, "/bothy/usage", "key-a", "")
	if rec.Code != http.StatusOK {
		t.Fatalf("/bothy/usage got %d, want 200", rec.Code)
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &usage); err != nil {
		t.Fatalf("/bothy/usage is not the documented shape: %v", err)
	}
	if len(usage.Peers) != 1 {
		t.Fatalf("usage reports %d peers, want 1", len(usage.Peers))
	}
	got := usage.Peers[0]
	if got.Peer != "alice" || got.Requests != 1 {
		t.Errorf("usage peer = %+v, want alice with one request", got)
	}
	// The counts came from the engine's reported usage, through the sniffer.
	if got.PromptTokens != 3 || got.CompletionTokens != 4 {
		t.Errorf("tokens = %d/%d, want 3/4 — the response was not metered",
			got.PromptTokens, got.CompletionTokens)
	}
	if got.Unmetered != 0 {
		t.Errorf("unmetered_responses = %d, want 0 for an engine that reports usage", got.Unmetered)
	}
}
