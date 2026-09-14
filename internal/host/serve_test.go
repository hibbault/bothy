package host

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
	"sync"
	"testing"
	"time"

	"github.com/hibbault/bothy/internal/engine"
	"github.com/hibbault/bothy/internal/httpx"
	"github.com/hibbault/bothy/internal/model"
)

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

func capturedLog() (*slog.Logger, *bytes.Buffer) {
	var buf bytes.Buffer
	return slog.New(slog.NewTextHandler(&buf, nil)), &buf
}

// hostLoggingTo keeps newTestHost's defaults and swaps in a logger that can be
// read afterwards, for the promises that are only ever said out loud.
func hostLoggingTo(t *testing.T, cfg Config, log *slog.Logger) *Host {
	t.Helper()
	h := newTestHost(t, cfg)
	h.log = log
	return h
}

// recordingRegistry counts announcements and keeps the last body, so what a host
// advertises can be asserted rather than assumed.
type recordingRegistry struct {
	*httptest.Server

	mu     sync.Mutex
	bodies []string
}

func newRecordingRegistry(t *testing.T) *recordingRegistry {
	t.Helper()
	r := &recordingRegistry{}
	r.Server = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, req *http.Request) {
		body, _ := io.ReadAll(req.Body)
		r.mu.Lock()
		r.bodies = append(r.bodies, string(body))
		r.mu.Unlock()
		httpx.JSON(w, http.StatusOK, map[string]any{"registered": 1})
	}))
	t.Cleanup(r.Close)
	return r
}

func (r *recordingRegistry) count() int {
	r.mu.Lock()
	defer r.mu.Unlock()
	return len(r.bodies)
}

func (r *recordingRegistry) last() string {
	r.mu.Lock()
	defer r.mu.Unlock()
	if len(r.bodies) == 0 {
		return ""
	}
	return r.bodies[len(r.bodies)-1]
}

func (r *recordingRegistry) entries(t *testing.T) []struct {
	Model    string `json:"model"`
	Digest   string `json:"digest"`
	Address  string `json:"address"`
	Capacity int    `json:"capacity"`
} {
	t.Helper()
	var payload struct {
		Entries []struct {
			Model    string `json:"model"`
			Digest   string `json:"digest"`
			Address  string `json:"address"`
			Capacity int    `json:"capacity"`
		} `json:"entries"`
	}
	if err := json.Unmarshal([]byte(r.last()), &payload); err != nil {
		t.Fatalf("the announced body is not {\"entries\":[…]}: %v (%q)", err, r.last())
	}
	return payload.Entries
}

// Registration is the heartbeat: a host that stops sending it is a host whose
// entry expires, which is how clients stop dialing a machine that went to sleep.
// Serve has to run that loop itself, and stop when told to.
func TestServeAnnouncesOnTheHeartbeatAndStopsOnCancel(t *testing.T) {
	reg := newRecordingRegistry(t)
	addr := freeAddr(t)
	h := newTestHost(t, Config{
		Listen:        addr,
		DiscoveryURL:  reg.URL,
		ShareKey:      "k",
		PublicAddress: "public.example:7777",
		MaxConcurrent: 4,
		Heartbeat:     20 * time.Millisecond,
	})

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan error, 1)
	go func() { done <- h.Serve(ctx) }()

	// Two announcements, not one: one would also be produced by a single
	// registration at startup, which is not a heartbeat.
	deadline := time.Now().Add(5 * time.Second)
	for reg.count() < 2 && time.Now().Before(deadline) {
		time.Sleep(10 * time.Millisecond)
	}
	if got := reg.count(); got < 2 {
		t.Fatalf("the registry saw %d announcements, want at least 2 from the heartbeat", got)
	}

	entries := reg.entries(t)
	if len(entries) != 1 || entries[0].Model != "llama3.1:8b" || entries[0].Digest != "sha256:1111" {
		t.Errorf("announced %+v, want the engine's models with digests", entries)
	}
	if entries[0].Address != "public.example:7777" {
		t.Errorf("announced address = %q, want the public one a peer can dial", entries[0].Address)
	}
	if entries[0].Capacity != 4 {
		t.Errorf("announced capacity = %d, want the free slots under the cap", entries[0].Capacity)
	}

	// The proxy is actually listening, so this test is about a served host and
	// not just a loop in the background.
	client := &http.Client{Timeout: 2 * time.Second}
	var served bool
	for i := 0; i < 100; i++ {
		resp, err := client.Get("http://" + addr + "/bothy/healthz")
		if err == nil {
			resp.Body.Close()
			served = resp.StatusCode == http.StatusOK
			break
		}
		time.Sleep(20 * time.Millisecond)
	}
	if !served {
		t.Fatal("the host never answered /bothy/healthz")
	}

	cancel()
	select {
	case err := <-done:
		if err != nil {
			t.Errorf("Serve returned %v, want nil after a graceful shutdown", err)
		}
	case <-time.After(8 * time.Second):
		t.Fatal("Serve did not return after its context was cancelled")
	}
}

// A host starts before its engine is ready — compose starts services in whatever
// order it likes — so an unlistable engine must be a warning, not a crash and not
// a registration of nothing.
func TestAnnounceSurvivesAnEngineThatCannotBeListed(t *testing.T) {
	// A closed port: the lister will be refused.
	dead := httptest.NewServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) {}))
	engineURL := dead.URL
	dead.Close()

	reg := newRecordingRegistry(t)
	log, buf := capturedLog()
	h := hostLoggingTo(t, Config{
		EngineKind:   "ollama",
		EngineURL:    engineURL,
		DiscoveryURL: reg.URL,
		ShareKey:     "k",
	}, log)

	h.announce(context.Background())

	if got := reg.count(); got != 0 {
		t.Errorf("the registry saw %d announcements, want 0: there was nothing to announce", got)
	}
	if models := h.currentModels(); models != nil {
		t.Errorf("currentModels() = %+v, want none", models)
	}
	if !strings.Contains(buf.String(), "cannot list engine models") {
		t.Errorf("nothing warned that the engine was unreadable: %s", buf.String())
	}
}

// A model with no digest is the llama.cpp/vLLM case. PROTOCOL.md says clients
// cannot verify it, so the host has to say so and register it anyway rather than
// dropping the model silently or inventing a digest.
func TestAnnounceWarnsAboutAModelWithNoDigestButStillRegistersIt(t *testing.T) {
	reg := newRecordingRegistry(t)
	log, buf := capturedLog()
	h := hostLoggingTo(t, Config{
		DiscoveryURL: reg.URL,
		ShareKey:     "k",
		Engine:       engine.Options{Static: []model.Model{{Name: "llama3.1:8b"}}},
	}, log)

	h.announce(context.Background())

	entries := reg.entries(t)
	if len(entries) != 1 || entries[0].Digest != "" {
		t.Fatalf("announced %+v, want the model registered with no digest", entries)
	}
	if !strings.Contains(buf.String(), "no digest") {
		t.Errorf("nothing warned that clients cannot verify this model: %s", buf.String())
	}
}

// The limits a host is *not* enforcing are the ones that matter, because a silent
// default is how somebody ends up giving their GPU away without meaning to.
func TestDescribeLimitsSaysWhatIsNotConfigured(t *testing.T) {
	log, buf := capturedLog()
	h := hostLoggingTo(t, Config{
		ShareKey:      "",
		MaxConcurrent: 0,
		OwnerReserve:  1,
	}, log)

	h.describeLimits()
	said := buf.String()

	for _, want := range []string{
		"no share key set",
		"no concurrency cap",
		"owner-reserve has no effect",
		"no per-peer budget",
		"no admin key",
	} {
		if !strings.Contains(said, want) {
			t.Errorf("describeLimits never said %q:\n%s", want, said)
		}
	}
}

func TestDescribeLimitsNamesWhatIsConfigured(t *testing.T) {
	log, buf := capturedLog()
	h := hostLoggingTo(t, Config{
		ShareKeys:     "alice:key-a,bob:key-b",
		MaxConcurrent: 4,
		OwnerReserve:  1,
		PeerQuota:     "200/1h",
		AdminKey:      "admin",
	}, log)

	h.describeLimits()
	said := buf.String()

	for _, want := range []string{
		"per-peer share keys required",
		"slots kept for you",
		"per-peer budget",
	} {
		if !strings.Contains(said, want) {
			t.Errorf("describeLimits never said %q:\n%s", want, said)
		}
	}
	if strings.Contains(said, "no per-peer budget") {
		t.Errorf("a configured budget was reported as absent:\n%s", said)
	}
}

// Starting paused is a supported way to say "not right now", and it is the one
// state where a host serves nobody while looking perfectly healthy. So it says so
// at startup rather than being discovered from a peer's 503 later.
func TestDescribeLimitsWarnsWhenStartingPaused(t *testing.T) {
	log, buf := capturedLog()
	h := hostLoggingTo(t, Config{Paused: true}, log)

	h.describeLimits()
	if said := buf.String(); !strings.Contains(said, "starting paused") {
		t.Errorf("a host that starts paused never said so:\n%s", said)
	}

	// And a host that is not paused must not cry wolf.
	log, buf = capturedLog()
	hostLoggingTo(t, Config{}, log).describeLimits()
	if said := buf.String(); strings.Contains(said, "starting paused") {
		t.Errorf("a host that is sharing normally claimed to be paused:\n%s", said)
	}
}

// The advertised address is what peers dial, so a host listening on every
// interface must not advertise ":7777" — that is not something anyone can reach.
func TestDefaultAddressIsDialable(t *testing.T) {
	for _, listen := range []string{":7777", "127.0.0.1:9999", "0.0.0.0:8080"} {
		got := defaultAddress(listen)
		host, port, err := net.SplitHostPort(got)
		if err != nil {
			t.Fatalf("defaultAddress(%q) = %q, which is not host:port: %v", listen, got, err)
		}
		if host == "" {
			t.Errorf("defaultAddress(%q) = %q, want a host a peer can resolve", listen, got)
		}
		wantPort := listen[strings.LastIndex(listen, ":")+1:]
		if port != wantPort {
			t.Errorf("defaultAddress(%q) = %q, want the port %s", listen, got, wantPort)
		}
	}
}

// An engine that dies mid-session has to be a clear failure at the edge, because
// a client that gets a hang cannot tell it from a slow model.
func TestAnUnreachableEngineIsABadGateway(t *testing.T) {
	dead := httptest.NewServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) {}))
	engineURL := dead.URL
	dead.Close()

	h := newTestHost(t, Config{EngineURL: engineURL, ShareKey: "k", MaxConcurrent: 4})
	rec := do(h, http.MethodPost, "/v1/chat/completions", "k", `{"model":"llama3.1:8b"}`)

	if rec.Code != http.StatusBadGateway {
		t.Fatalf("status = %d, want 502 for an unreachable engine: %s", rec.Code, rec.Body)
	}
	if !strings.Contains(rec.Body.String(), "engine unreachable") {
		t.Errorf("refusal %q does not say the engine was unreachable", rec.Body)
	}
	// The slot was taken before the request went out, so a failed request must
	// give it back — otherwise an engine outage quietly fills the host's cap.
	if got := h.meter.InFlight(); got != 0 {
		t.Errorf("InFlight() = %d after a failed request, want 0", got)
	}
}

// Run is the command line surface: every one of these is a typo that must be an
// error at startup rather than a host that runs with a limit nobody configured.
func TestRunRefusesConfigurationsItCannotHonour(t *testing.T) {
	log := slog.New(slog.NewTextHandler(io.Discard, nil))
	ctx := context.Background()

	for _, tc := range []struct {
		name string
		args []string
	}{
		{"an unknown engine kind", []string{"-engine-kind", "nonsense"}},
		{"an engine URL that is not a URL", []string{"-engine-url", "://bad"}},
		{"a model list with no name", []string{"-models", "=sha256:abc"}},
		{"a quota without a period", []string{"-peer-quota", "200"}},
		{"a reserve that swallows the cap", []string{"-owner-reserve", "9", "-max-concurrent", "2"}},
		{"a negative reserve", []string{"-owner-reserve", "-1"}},
		{"share keys that are not name:key", []string{"-share-keys", "alice"}},
		// A heartbeat of zero used to reach time.NewTicker and panic inside the
		// announce loop, which is a crash rather than a startup error.
		{"a heartbeat of zero", []string{"-heartbeat", "0s"}},
		{"a negative heartbeat", []string{"-heartbeat", "-1s"}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			args := append([]string{"-listen", "127.0.0.1:0"}, tc.args...)
			if err := Run(ctx, log, args); err == nil {
				t.Fatal("accepted a configuration it cannot honour")
			}
		})
	}
}

// The happy path of the command, so the flag wiring is covered rather than just
// its refusals.
func TestRunServesUntilCancelled(t *testing.T) {
	addr := freeAddr(t)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	done := make(chan error, 1)
	go func() {
		done <- Run(ctx, slog.New(slog.NewTextHandler(io.Discard, nil)), []string{
			"-listen", addr,
			"-engine-kind", "static",
			"-models", "llama3.1:8b=sha256:1111",
			"-share-key", "k",
		})
	}()

	client := &http.Client{Timeout: 2 * time.Second}
	served := false
	for i := 0; i < 100; i++ {
		resp, err := client.Get("http://" + addr + "/bothy/healthz")
		if err == nil {
			resp.Body.Close()
			served = resp.StatusCode == http.StatusOK
			break
		}
		time.Sleep(20 * time.Millisecond)
	}
	if !served {
		t.Fatal("the host never answered, so this proves nothing")
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
