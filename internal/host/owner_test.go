package host

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/hibbault/bothy/internal/httpx"
)

// getJSON reads a documented JSON route.
func getJSON(t *testing.T, h *Host, path, key string) map[string]any {
	t.Helper()
	rec := do(h, http.MethodGet, path, key, "")
	if rec.Code != http.StatusOK {
		t.Fatalf("GET %s = %d, want 200: %s", path, rec.Code, rec.Body)
	}
	var out map[string]any
	if err := json.Unmarshal(rec.Body.Bytes(), &out); err != nil {
		t.Fatalf("GET %s is not JSON: %v", path, err)
	}
	return out
}

// refusalMessage reads the message out of a refusal body.
func refusalMessage(t *testing.T, rec *httptest.ResponseRecorder) string {
	t.Helper()
	var payload struct {
		Error struct {
			Message string `json:"message"`
		} `json:"error"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &payload); err != nil {
		t.Fatalf("a refusal is not the documented error shape: %s", rec.Body)
	}
	return payload.Error.Message
}

func number(t *testing.T, body map[string]any, key string) float64 {
	t.Helper()
	v, ok := body[key].(float64)
	if !ok {
		t.Fatalf("%s is %#v, want a number", key, body[key])
	}
	return v
}

// TestTheOwnerReserveIsHeldBackOnTheWire is the feature end to end: a host whose
// cap would allow more, refusing because the extra slot is not the peers'.
func TestTheOwnerReserveIsHeldBackOnTheWire(t *testing.T) {
	f := newFakeEngine(t)
	release := make(chan struct{})
	arrived := make(chan struct{}, 2)
	f.hold(release)
	f.announce(arrived)
	var once sync.Once
	letGo := func() { once.Do(func() { close(release) }) }

	h := newTestHost(t, Config{EngineURL: f.URL, ShareKey: "k", MaxConcurrent: 2, OwnerReserve: 1})
	srv := httptest.NewServer(h.Handler())
	defer srv.Close()
	defer letGo()

	// One request, held at the engine, is all the peers are allowed.
	done := make(chan *http.Response, 1)
	go func() {
		req, _ := http.NewRequest(http.MethodPost, srv.URL+"/v1/chat/completions",
			strings.NewReader(`{"model":"llama3.1:8b"}`))
		req.Header.Set(httpx.KeyHeader, "k")
		resp, err := (&http.Client{Timeout: 10 * time.Second}).Do(req)
		if err != nil {
			done <- nil
			return
		}
		done <- resp
	}()
	select {
	case <-arrived:
	case <-time.After(5 * time.Second):
		t.Fatal("the first request never reached the engine")
	}

	second := do(h, http.MethodPost, "/v1/chat/completions", "k", `{"model":"llama3.1:8b"}`)
	if second.Code != http.StatusTooManyRequests {
		t.Errorf("a request beyond the peer slots got %d, want 429 — the reserved slot is being given away", second.Code)
	}
	if _, headers, _ := f.seen(); len(headers) != 1 {
		t.Errorf("the engine saw %d requests, want 1", len(headers))
	}

	// And the reservation is visible rather than a mystery about why a host with
	// a cap of 2 refuses the second request.
	for _, path := range []string{"/bothy/healthz", "/bothy/usage"} {
		body := getJSON(t, h, path, "k")
		if got := number(t, body, "owner_reserve"); got != 1 {
			t.Errorf("%s owner_reserve = %v, want 1", path, got)
		}
		if got := number(t, body, "peer_slots"); got != 1 {
			t.Errorf("%s peer_slots = %v, want 1", path, got)
		}
		if got := number(t, body, "capacity"); got != 0 {
			t.Errorf("%s capacity = %v, want 0 — a reserved slot must not be advertised", path, got)
		}
	}
}

func TestNoReserveIsReportedAsNoReserve(t *testing.T) {
	h := newTestHost(t, Config{ShareKey: "k", MaxConcurrent: 3})
	body := getJSON(t, h, "/bothy/healthz", "k")
	if got := number(t, body, "owner_reserve"); got != 0 {
		t.Errorf("owner_reserve = %v, want 0", got)
	}
	if got := number(t, body, "peer_slots"); got != 3 {
		t.Errorf("peer_slots = %v, want 3", got)
	}
}

// TestPausedRefusesPeersWithoutCountingItAgainstThem: the refusal is not the
// peer's doing, so it must not look like one in the usage report.
func TestPausedRefusesPeersWithoutCountingItAgainstThem(t *testing.T) {
	f := newFakeEngine(t)
	h := newTestHost(t, Config{EngineURL: f.URL, ShareKey: "k", MaxConcurrent: 4, Paused: true})

	rec := do(h, http.MethodPost, "/v1/chat/completions", "k", `{"model":"llama3.1:8b"}`)
	if rec.Code != http.StatusServiceUnavailable {
		t.Fatalf("status %d, want 503 while paused: %s", rec.Code, rec.Body)
	}
	if msg := refusalMessage(t, rec); !strings.Contains(msg, "paused") {
		t.Errorf("refusal %q does not say the host is paused", msg)
	}
	if _, headers, _ := f.seen(); len(headers) != 0 {
		t.Errorf("the engine saw %d requests while paused, want 0", len(headers))
	}

	health := getJSON(t, h, "/bothy/healthz", "k")
	if paused, ok := health["paused"].(bool); !ok || !paused {
		t.Errorf("healthz paused = %#v, want true", health["paused"])
	}

	// Nothing was counted, so no peer row claims a refusal they did not earn.
	usage := getJSON(t, h, "/bothy/usage", "k")
	if peers, ok := usage["peers"].([]any); !ok || len(peers) != 0 {
		t.Errorf("usage peers = %#v, want none: a pause is not a peer's usage", usage["peers"])
	}
}

// TestSharingCanOnlyBeControlledWithTheAdminKey is the security property: a share
// key is handed to peers, so a peer who can stop the host is worse than no
// control surface at all.
func TestSharingCanOnlyBeControlledWithTheAdminKey(t *testing.T) {
	t.Run("no admin key means no control surface at all", func(t *testing.T) {
		h := newTestHost(t, Config{ShareKey: "peer-key"})
		rec := do(h, http.MethodPost, "/bothy/sharing", "peer-key", `{"paused": true}`)
		if rec.Code != http.StatusNotFound {
			t.Errorf("status %d, want 404 — without a key there is no endpoint here", rec.Code)
		}
		if msg := refusalMessage(t, rec); !strings.Contains(msg, "admin key") {
			t.Errorf("refusal %q does not say what is missing", msg)
		}
	})

	h := newTestHost(t, Config{ShareKeys: "alice:key-a", AdminKey: "admin-secret"})

	t.Run("a share key is refused", func(t *testing.T) {
		rec := do(h, http.MethodPost, "/bothy/sharing", "key-a", `{"paused": true}`)
		if rec.Code != http.StatusUnauthorized {
			t.Errorf("a peer's share key got %d, want 401", rec.Code)
		}
		if paused, _ := getJSON(t, h, "/bothy/healthz", "key-a")["paused"].(bool); paused {
			t.Error("a peer paused the host")
		}
	})

	t.Run("no key is refused", func(t *testing.T) {
		if rec := do(h, http.MethodPost, "/bothy/sharing", "", `{"paused": true}`); rec.Code != http.StatusUnauthorized {
			t.Errorf("status %d, want 401", rec.Code)
		}
	})

	t.Run("reading the control endpoint controls nothing", func(t *testing.T) {
		if rec := do(h, http.MethodGet, "/bothy/sharing", "admin-secret", ""); rec.Code == http.StatusOK {
			t.Errorf("GET returned 200: %s", rec.Body)
		}
		if paused, _ := getJSON(t, h, "/bothy/healthz", "")["paused"].(bool); paused {
			t.Error("a GET changed the sharing state")
		}
	})

	t.Run("an unreadable body is refused, not guessed at", func(t *testing.T) {
		for _, body := range []string{"", "not json", "{}", `{"paused": "yes"}`} {
			rec := do(h, http.MethodPost, "/bothy/sharing", "admin-secret", body)
			if rec.Code != http.StatusBadRequest {
				t.Errorf("body %q got %d, want 400", body, rec.Code)
			}
		}
	})

	t.Run("the admin key pauses and resumes", func(t *testing.T) {
		f := newFakeEngine(t)
		controlled := newTestHost(t, Config{EngineURL: f.URL, ShareKey: "k", AdminKey: "admin-secret"})

		rec := do(controlled, http.MethodPost, "/bothy/sharing", "admin-secret", `{"paused": true}`)
		if rec.Code != http.StatusOK {
			t.Fatalf("pause got %d: %s", rec.Code, rec.Body)
		}
		var state struct {
			Paused bool   `json:"paused"`
			Since  string `json:"since"`
		}
		if err := json.Unmarshal(rec.Body.Bytes(), &state); err != nil {
			t.Fatal(err)
		}
		if !state.Paused || state.Since == "" {
			t.Errorf("pause reported %+v, want paused with a since time", state)
		}

		if got := do(controlled, http.MethodPost, "/v1/chat/completions", "k", `{"model":"llama3.1:8b"}`).Code; got != http.StatusServiceUnavailable {
			t.Errorf("a request while paused got %d, want 503", got)
		}

		if rec := do(controlled, http.MethodPost, "/bothy/sharing", "admin-secret", `{"paused": false}`); rec.Code != http.StatusOK {
			t.Fatalf("resume got %d: %s", rec.Code, rec.Body)
		}
		if got := do(controlled, http.MethodPost, "/v1/chat/completions", "k", `{"model":"llama3.1:8b"}`).Code; got != http.StatusOK {
			t.Errorf("a request after resuming got %d, want 200 — the engine should be reachable again", got)
		}
	})
}

// TestPausingStopsTheHostBeingAdvertised: the registry has no delete, so a paused
// host stops saying it is there and lets the entry expire, which is what sends
// clients elsewhere instead of to a host that will refuse them.
func TestPausingStopsTheHostBeingAdvertised(t *testing.T) {
	var mu sync.Mutex
	registrations := 0
	discovery := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		mu.Lock()
		registrations++
		mu.Unlock()
		httpx.JSON(w, http.StatusOK, map[string]any{"ok": true})
	}))
	defer discovery.Close()

	h := newTestHost(t, Config{
		DiscoveryURL:  discovery.URL,
		ShareKey:      "k",
		MaxConcurrent: 4,
		AdminKey:      "admin-secret",
	})

	count := func() int {
		mu.Lock()
		defer mu.Unlock()
		return registrations
	}

	h.announce(context.Background())
	if count() != 1 {
		t.Fatalf("registrations after one announce = %d, want 1", count())
	}

	do(h, http.MethodPost, "/bothy/sharing", "admin-secret", `{"paused": true}`)
	h.announce(context.Background())
	if count() != 1 {
		t.Errorf("registrations after announcing while paused = %d, want 1 — a paused host must stop advertising", count())
	}

	do(h, http.MethodPost, "/bothy/sharing", "admin-secret", `{"paused": false}`)
	h.announce(context.Background())
	if count() != 2 {
		t.Errorf("registrations after resuming = %d, want 2", count())
	}
}

func TestQuotaSpecIsCheckedAtStartup(t *testing.T) {
	for _, tc := range []struct {
		spec    string
		wantErr string
	}{
		{"", ""},
		{"200/1h", ""},
		{"1/1m", ""},
		{"200", "count/period"},
		{"200/", "not a duration"},
		{"/1h", "not a positive number"},
		{"0/1h", "not a positive number"},
		{"-5/1h", "not a positive number"},
		{"many/1h", "not a positive number"},
		{"200/soon", "not a duration"},
		{"200/0s", "not a duration"},
	} {
		t.Run(tc.spec, func(t *testing.T) {
			cfg := Config{ShareKey: "k", MaxConcurrent: 4, PeerQuota: tc.spec}
			if cfg.Listen == "" {
				cfg.Listen = "127.0.0.1:0"
			}
			cfg.EngineKind = "static"
			cfg.EngineURL = "http://127.0.0.1:1"
			cfg.PublicAddress = "host:7777"
			cfg.Heartbeat = time.Hour
			cfg.Engine.Static = testModels()

			_, err := New(cfg, testLogger())
			if tc.wantErr == "" {
				if err != nil {
					t.Fatalf("quota %q refused: %v", tc.spec, err)
				}
				return
			}
			if err == nil {
				t.Fatalf("quota %q accepted, want a refusal naming %q", tc.spec, tc.wantErr)
			}
			if !strings.Contains(err.Error(), tc.wantErr) {
				t.Errorf("error %q does not mention %q", err, tc.wantErr)
			}
		})
	}
}

// TestAPeerBudgetIsAFinalAnswerAndSaysWhenToComeBack: unlike a rate limit, a
// spent budget is not refilled by waiting a moment, so it is the one refusal
// where the peer most needs to be told when to try again.
func TestAPeerBudgetIsAFinalAnswerAndSaysWhenToComeBack(t *testing.T) {
	f := newFakeEngine(t)
	h := newTestHost(t, Config{
		EngineURL:     f.URL,
		ShareKey:      "k",
		MaxConcurrent: 10,
		PeerQuota:     "2/1h",
	})

	for i := 0; i < 2; i++ {
		if rec := do(h, http.MethodPost, "/v1/chat/completions", "k", `{"model":"llama3.1:8b"}`); rec.Code != http.StatusOK {
			t.Fatalf("request %d of the budget got %d: %s", i+1, rec.Code, rec.Body)
		}
	}

	rec := do(h, http.MethodPost, "/v1/chat/completions", "k", `{"model":"llama3.1:8b"}`)
	if rec.Code != http.StatusTooManyRequests {
		t.Fatalf("a request over budget got %d, want 429: %s", rec.Code, rec.Body)
	}
	if msg := refusalMessage(t, rec); !strings.Contains(msg, "budget") {
		t.Errorf("refusal %q does not say it is a budget", msg)
	}
	retryAfter := rec.Header().Get("Retry-After")
	if retryAfter == "" {
		t.Fatal("a refusal with no Retry-After leaves the peer guessing")
	}
	if retryAfter == "0" {
		t.Errorf("Retry-After = %q for an hour-long budget", retryAfter)
	}
	if _, headers, _ := f.seen(); len(headers) != 2 {
		t.Errorf("the engine saw %d requests, want 2 — a refused request must not be forwarded", len(headers))
	}

	usage := getJSON(t, h, "/bothy/usage", "k")
	if got := usage["peer_quota"]; got != "2/1h" {
		t.Errorf("usage peer_quota = %#v, want 2/1h", got)
	}
}

// TestAReserveThatLeavesNoRoomForPeersIsRefused: a host serving nobody while
// looking healthy is a misconfiguration, and pausing is the way to say "not right
// now" — out loud, and reversibly.
func TestAReserveThatLeavesNoRoomForPeersIsRefused(t *testing.T) {
	for _, tc := range []struct {
		name    string
		cfg     Config
		wantErr string
	}{
		{
			name:    "the reserve swallows the cap",
			cfg:     Config{MaxConcurrent: 2, OwnerReserve: 2},
			wantErr: "no slots for peers",
		},
		{
			name:    "the reserve is bigger than the cap",
			cfg:     Config{MaxConcurrent: 2, OwnerReserve: 5},
			wantErr: "no slots for peers",
		},
		{
			name:    "the reserve is negative",
			cfg:     Config{MaxConcurrent: 2, OwnerReserve: -1},
			wantErr: "negative",
		},
	} {
		t.Run(tc.name, func(t *testing.T) {
			cfg := tc.cfg
			cfg.ShareKey = "k"
			cfg.Listen = "127.0.0.1:0"
			cfg.EngineKind = "static"
			cfg.EngineURL = "http://127.0.0.1:1"
			cfg.PublicAddress = "host:7777"
			cfg.Heartbeat = time.Hour
			cfg.Engine.Static = testModels()

			_, err := New(cfg, testLogger())
			if err == nil {
				t.Fatal("accepted a host that would serve no peers at all")
			}
			if !strings.Contains(err.Error(), tc.wantErr) {
				t.Errorf("error %q does not mention %q", err, tc.wantErr)
			}
		})
	}
}
