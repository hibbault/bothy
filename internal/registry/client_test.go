package registry

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"
)

// recordServer captures what a client actually puts on the wire. A client test
// that only checked error values would notice a dropped token header not at all.
type recordServer struct {
	*httptest.Server

	mu       sync.Mutex
	requests []*http.Request
	bodies   []string
}

func newRecordServer(t *testing.T, handler func(w http.ResponseWriter, r *http.Request)) *recordServer {
	t.Helper()
	rs := &recordServer{}
	rs.Server = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		rs.mu.Lock()
		rs.requests = append(rs.requests, r.Clone(context.Background()))
		rs.bodies = append(rs.bodies, string(body))
		rs.mu.Unlock()
		handler(w, r)
	}))
	t.Cleanup(rs.Close)
	return rs
}

func (rs *recordServer) recorded() ([]*http.Request, []string) {
	rs.mu.Lock()
	defer rs.mu.Unlock()
	return append([]*http.Request(nil), rs.requests...), append([]string(nil), rs.bodies...)
}

func (rs *recordServer) count() int {
	rs.mu.Lock()
	defer rs.mu.Unlock()
	return len(rs.requests)
}

// Register is the entire liveness protocol — a host's heartbeat is this call
// repeated — so the shape it sends has to be exactly what the registry parses.
func TestRegisterPostsEntriesWithTheToken(t *testing.T) {
	srv := newRecordServer(t, func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
	})

	c := NewClient(srv.URL, "reg-token")
	if err := c.Register(context.Background(), []Entry{{
		Model: "llama3.1:8b", Digest: "sha256:aa", Address: "box:7777", Host: "box", Capacity: 3,
	}}); err != nil {
		t.Fatalf("Register: %v", err)
	}

	reqs, bodies := srv.recorded()
	if len(reqs) != 1 {
		t.Fatalf("the registry saw %d requests, want 1", len(reqs))
	}
	if reqs[0].Method != http.MethodPost || reqs[0].URL.Path != "/register" {
		t.Errorf("sent %s %s, want POST /register", reqs[0].Method, reqs[0].URL.Path)
	}
	if got := reqs[0].Header.Get("Authorization"); got != "Bearer reg-token" {
		t.Errorf("Authorization = %q, want the register token", got)
	}
	if ct := reqs[0].Header.Get("Content-Type"); ct != "application/json" {
		t.Errorf("Content-Type = %q, want application/json", ct)
	}

	var payload struct {
		Entries []Entry `json:"entries"`
	}
	if err := json.Unmarshal([]byte(bodies[0]), &payload); err != nil {
		t.Fatalf("the body is not {\"entries\":[…]}: %v (%q)", err, bodies[0])
	}
	if len(payload.Entries) != 1 {
		t.Fatalf("entries = %+v, want 1", payload.Entries)
	}
	got := payload.Entries[0]
	if got.Model != "llama3.1:8b" || got.Address != "box:7777" || got.Digest != "sha256:aa" || got.Capacity != 3 {
		t.Errorf("entry = %+v, want the fields as given", got)
	}
}

// An open registry is a supported configuration, and it must not send an empty
// Authorization header — some proxies reject a malformed credential outright.
func TestRegisterWithoutATokenSendsNoCredential(t *testing.T) {
	srv := newRecordServer(t, func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
	})
	if err := NewClient(srv.URL, "").Register(context.Background(), []Entry{{Model: "m", Address: "a:1"}}); err != nil {
		t.Fatalf("Register: %v", err)
	}
	reqs, _ := srv.recorded()
	if got := reqs[0].Header.Get("Authorization"); got != "" {
		t.Errorf("Authorization = %q, want none when no token is configured", got)
	}
}

// A refused registration is a heartbeat that did not land, so the error has to
// say which registry and why — otherwise the host logs "registration failed" and
// the operator is left with nothing.
func TestRegisterReportsARefusalFromTheRegistry(t *testing.T) {
	srv := newRecordServer(t, func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusUnauthorized)
		_, _ = io.WriteString(w, `{"error":{"message":"bad token"}}`)
	})

	err := NewClient(srv.URL, "wrong").Register(context.Background(), []Entry{{Model: "m", Address: "a:1"}})
	if err == nil {
		t.Fatal("a 401 from the registry was accepted as a successful heartbeat")
	}
	for _, want := range []string{srv.URL, "401", "bad token"} {
		if !strings.Contains(err.Error(), want) {
			t.Errorf("error %q does not mention %q", err, want)
		}
	}
}

func TestClientErrorsNameTheRegistryWhenItIsUnreachable(t *testing.T) {
	srv := newRecordServer(t, func(http.ResponseWriter, *http.Request) {})
	addr := srv.URL
	srv.Close() // nothing is listening now

	c := NewClient(addr, "")
	if err := c.Register(context.Background(), []Entry{{Model: "m", Address: "a:1"}}); err == nil {
		t.Error("Register to a dead registry succeeded")
	} else if !strings.Contains(err.Error(), addr) {
		t.Errorf("error %q does not name the registry", err)
	}
	if _, err := c.List(context.Background(), "m"); err == nil {
		t.Error("List against a dead registry succeeded")
	} else if !strings.Contains(err.Error(), addr) {
		t.Errorf("error %q does not name the registry", err)
	}
}

func TestListDecodesEntries(t *testing.T) {
	srv := newRecordServer(t, func(w http.ResponseWriter, _ *http.Request) {
		_, _ = io.WriteString(w, `{"entries":[{"model":"llama3.1:8b","digest":"sha256:aa","address":"box:7777","capacity":2}],"count":1}`)
	})

	entries, err := NewClient(srv.URL, "").List(context.Background(), "llama3.1:8b")
	if err != nil {
		t.Fatalf("List: %v", err)
	}
	if len(entries) != 1 {
		t.Fatalf("entries = %+v, want 1", entries)
	}
	if entries[0].Address != "box:7777" || entries[0].Digest != "sha256:aa" || entries[0].Capacity != 2 {
		t.Errorf("entry = %+v", entries[0])
	}
}

// A model name is a query value, and a tag is not always URL-safe. Getting this
// wrong asks the registry for a different model and looks like "nobody has it".
func TestListEscapesTheModelName(t *testing.T) {
	srv := newRecordServer(t, func(w http.ResponseWriter, _ *http.Request) {
		_, _ = io.WriteString(w, `{"entries":[]}`)
	})

	c := NewClient(srv.URL, "")
	const model = "llama3.1:8b+vision/tools"
	if _, err := c.List(context.Background(), model); err != nil {
		t.Fatalf("List: %v", err)
	}
	reqs, _ := srv.recorded()
	if got := reqs[0].URL.Query().Get("model"); got != model {
		t.Errorf("the registry was asked for %q, want %q", got, model)
	}
	if raw := reqs[0].URL.RawQuery; strings.ContainsAny(raw, " /+") {
		t.Errorf("raw query %q is not escaped", raw)
	}
}

// No model means "everyone", which has to be a bare /models request: an empty
// ?model= would be a filter for the empty name on some servers.
func TestListOmitsTheQueryWhenNoModelIsAskedFor(t *testing.T) {
	srv := newRecordServer(t, func(w http.ResponseWriter, _ *http.Request) {
		_, _ = io.WriteString(w, `{"entries":[]}`)
	})
	if _, err := NewClient(srv.URL, "").List(context.Background(), ""); err != nil {
		t.Fatalf("List: %v", err)
	}
	reqs, _ := srv.recorded()
	if raw := reqs[0].URL.RawQuery; raw != "" {
		t.Errorf("query = %q, want none", raw)
	}
}

func TestListRejectsAMalformedAnswer(t *testing.T) {
	t.Run("a non-200 status", func(t *testing.T) {
		srv := newRecordServer(t, func(w http.ResponseWriter, _ *http.Request) {
			w.WriteHeader(http.StatusInternalServerError)
			_, _ = io.WriteString(w, "boom")
		})
		_, err := NewClient(srv.URL, "").List(context.Background(), "m")
		if err == nil {
			t.Fatal("a 500 from the registry was read as an empty model list")
		}
		for _, want := range []string{"500", "boom"} {
			if !strings.Contains(err.Error(), want) {
				t.Errorf("error %q does not mention %q", err, want)
			}
		}
	})

	t.Run("a body that is not the documented shape", func(t *testing.T) {
		srv := newRecordServer(t, func(w http.ResponseWriter, _ *http.Request) {
			_, _ = io.WriteString(w, `{"entries":"nope"}`)
		})
		_, err := NewClient(srv.URL, "").List(context.Background(), "m")
		if err == nil {
			t.Fatal("a body that cannot be decoded was accepted")
		}
		if !strings.Contains(err.Error(), "/models") {
			t.Errorf("error %q does not say which endpoint failed", err)
		}
	})
}

// A cancelled context has to end the call, or `connect` would hang on a registry
// that stopped answering instead of falling back.
func TestClientHonoursContextCancellation(t *testing.T) {
	srv := newRecordServer(t, func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
	})
	c := NewClient(srv.URL, "")

	ctx, cancel := context.WithCancel(context.Background())
	cancel()

	if err := c.Register(ctx, []Entry{{Model: "m", Address: "a:1"}}); err == nil {
		t.Error("Register with a cancelled context was still sent")
	}
	if _, err := c.List(ctx, "m"); err == nil {
		t.Error("List with a cancelled context was still sent")
	}
	if n := srv.count(); n != 0 {
		t.Errorf("the registry saw %d requests, want 0 for cancelled calls", n)
	}
}

// A base URL written with a trailing slash is the same registry, and a double
// slash is a 404 on many servers.
func TestNewClientTrimsTrailingSlashes(t *testing.T) {
	c := NewClient("http://reg.example/", "t")
	if c.BaseURL != "http://reg.example" {
		t.Errorf("BaseURL = %q, want the trailing slash trimmed", c.BaseURL)
	}
	if c.Token != "t" {
		t.Errorf("Token = %q, want it carried through", c.Token)
	}

	srv := newRecordServer(t, func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
	})
	if err := NewClient(srv.URL+"///", "").Register(context.Background(), []Entry{{Model: "m", Address: "a:1"}}); err != nil {
		t.Fatalf("Register: %v", err)
	}
	reqs, _ := srv.recorded()
	if reqs[0].URL.Path != "/register" {
		t.Errorf("path = %q, want /register", reqs[0].URL.Path)
	}
}

// The registry's TTL is what clients are promised: an entry outlives its host's
// last heartbeat by exactly this long, and the client reports it so the promise
// is visible rather than implied.
func TestStoreReportsItsTTL(t *testing.T) {
	if got := NewStore(90 * time.Second).TTL(); got != 90*time.Second {
		t.Errorf("TTL() = %s, want 1m30s", got)
	}
}

// Sorting is what lets a client take the first entry and be done: capacity
// descending, then a stable order for ties so two identical lookups agree.
func TestListIsStableForEqualCapacity(t *testing.T) {
	s := NewStore(time.Minute)
	s.Register([]Entry{
		{Model: "m", Address: "c:1", Host: "c"},
		{Model: "m", Address: "a:1", Host: "a"},
		{Model: "m", Address: "b:1", Host: "b"},
	})

	first := s.List("m")
	if len(first) != 3 {
		t.Fatalf("List returned %d entries, want 3", len(first))
	}
	if first[0].Host != "a" || first[1].Host != "b" || first[2].Host != "c" {
		t.Errorf("order = %s,%s,%s; want host order for equal capacity", first[0].Host, first[1].Host, first[2].Host)
	}
	second := s.List("m")
	for i := range first {
		if first[i].Address != second[i].Address {
			t.Fatalf("two identical List calls disagreed: %+v vs %+v", first, second)
		}
	}
}

// A host offering several models announces them in one call, and a bad entry in
// the batch must not take the good ones down with it.
func TestRegisterKeepsTheUsableEntriesOfABatch(t *testing.T) {
	s := NewStore(time.Minute)
	n := s.Register([]Entry{
		{Model: "a", Address: "x:1"},
		{Model: "", Address: "y:1"},
		{Model: "b", Address: ""},
		{Model: "c", Address: "z:1"},
	})
	if n != 2 {
		t.Fatalf("Register accepted %d of 4 entries, want 2", n)
	}
	if got := len(s.List("")); got != 2 {
		t.Errorf("List returned %d live entries, want 2", got)
	}
}
