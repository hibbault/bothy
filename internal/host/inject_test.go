package host

import (
	"io"
	"net/http"
	"strconv"
	"strings"
	"testing"
)

// The decision table is the whole risk here: adding a field to a body Bothy did
// not write is only safe because it happens in a narrow, enumerable set of
// cases. So the cases are written down.
func TestAddStreamUsage(t *testing.T) {
	for _, tc := range []struct {
		name        string
		body        string
		wantChanged bool
		wantHas     string // a substring the result must contain when changed
	}{
		{
			name:        "a streamed chat completion gets the ask",
			body:        `{"model":"llama3.1:8b","stream":true,"messages":[]}`,
			wantChanged: true,
			wantHas:     `"stream_options":{"include_usage":true}`,
		},
		{
			name:        "a streamed legacy completion gets it too",
			body:        `{"model":"llama3.1:8b","stream":true,"prompt":"hi"}`,
			wantChanged: true,
			wantHas:     `"include_usage":true`,
		},
		{
			name: "a caller that already asked is left exactly as it was",
			body: `{"model":"m","stream":true,"stream_options":{"include_usage":true}}`,
		},
		{
			name: "a caller that asked not to is not overridden",
			body: `{"model":"m","stream":true,"stream_options":{"include_usage":false}}`,
		},
		{
			name: "an unrelated stream_options key is still an opinion",
			body: `{"model":"m","stream":true,"stream_options":{"unknown":"x"}}`,
		},
		{
			name: "a whole-response request is not a stream",
			body: `{"model":"m","stream":false,"messages":[]}`,
		},
		{
			name: "a request with no stream field at all",
			body: `{"model":"m","messages":[]}`,
		},
		{
			name: "a stream field that is not a bool",
			body: `{"model":"m","stream":"yes"}`,
		},
		{
			name: "not JSON, so not ours to touch",
			body: `model=llama3.1&stream=true`,
		},
		{
			name: "JSON, but not an object",
			body: `["stream",true]`,
		},
		{
			name: "empty body",
			body: ``,
		},
	} {
		t.Run(tc.name, func(t *testing.T) {
			got, changed := addStreamUsage([]byte(tc.body))
			if changed != tc.wantChanged {
				t.Fatalf("changed = %v, want %v (result %q)", changed, tc.wantChanged, got)
			}
			if !changed {
				if string(got) != tc.body {
					t.Errorf("an untouched body was altered:\n got %q\nwant %q", got, tc.body)
				}
				return
			}
			if !strings.Contains(string(got), tc.wantHas) {
				t.Errorf("result %q does not contain %q", got, tc.wantHas)
			}
			// What was there before must still be there: this adds a field, and
			// a rewrite that dropped the prompt would be worse than no meter.
			for _, keep := range []string{`"model":"m"`, `"stream":true`} {
				if strings.Contains(tc.body, keep) && !strings.Contains(string(got), keep) {
					t.Errorf("result %q lost %q", got, keep)
				}
			}
		})
	}
}

func TestWantsStreamUsage(t *testing.T) {
	for _, tc := range []struct {
		method, path string
		want         bool
	}{
		{http.MethodPost, "/v1/chat/completions", true},
		{http.MethodPost, "/v1/completions", true},
		{http.MethodGet, "/v1/chat/completions", false},
		{http.MethodPost, "/api/chat", false},      // Ollama's own shape
		{http.MethodPost, "/v1/embeddings", false}, // not a streamed route
		{http.MethodPost, "/bothy/models", false},  // ours, and not OpenAI's
		{http.MethodPost, "/v1/unknown/thing", false},
	} {
		r, err := http.NewRequest(tc.method, "http://engine"+tc.path, nil)
		if err != nil {
			t.Fatal(err)
		}
		if got := wantsStreamUsage(r); got != tc.want {
			t.Errorf("%s %s -> %v, want %v", tc.method, tc.path, got, tc.want)
		}
	}
}

// A request that is not going to be rewritten must be forwarded byte for byte,
// including one too large to buffer. The body is read here as a stand-in for the
// proxy doing the same.
func TestInjectStreamUsageLeavesOtherRequestsAlone(t *testing.T) {
	const body = `{"model":"m","stream":true,"messages":[]}`

	r, err := http.NewRequest(http.MethodPost, "http://engine/api/chat", strings.NewReader(body))
	if err != nil {
		t.Fatal(err)
	}
	if injectStreamUsage(r) {
		t.Error("a non-OpenAI route was rewritten")
	}
	got, err := io.ReadAll(r.Body)
	if err != nil {
		t.Fatal(err)
	}
	if string(got) != body {
		t.Errorf("body = %q, want it unchanged", got)
	}
}

// A body past the buffer limit is forwarded untouched rather than truncated,
// because a half-read body would be a worse bug than an unmetered reply.
func TestInjectStreamUsageForwardsAnOversizedBody(t *testing.T) {
	big := `{"model":"m","stream":true,"padding":"` + strings.Repeat("x", maxInjectable) + `"}`
	r, err := http.NewRequest(http.MethodPost, "http://engine/v1/chat/completions", strings.NewReader(big))
	if err != nil {
		t.Fatal(err)
	}
	if injectStreamUsage(r) {
		t.Error("an oversized body was rewritten")
	}
	got, err := io.ReadAll(r.Body)
	if err != nil {
		t.Fatal(err)
	}
	if len(got) != len(big) {
		t.Fatalf("read back %d bytes, want %d — the body was truncated", len(got), len(big))
	}
	if !strings.HasPrefix(string(got), `{"model":"m"`) || !strings.HasSuffix(string(got), `"}`) {
		t.Error("the oversized body came back mangled")
	}
}

// And the case it exists for: the body is rewritten, the caller's fields survive,
// and the length the engine is told matches what it will receive.
func TestInjectStreamUsageRewritesAStreamedRequest(t *testing.T) {
	r, err := http.NewRequest(http.MethodPost, "http://engine/v1/chat/completions",
		strings.NewReader(`{"model":"llama3.1:8b","stream":true}`))
	if err != nil {
		t.Fatal(err)
	}
	if !injectStreamUsage(r) {
		t.Fatal("a streamed chat completion was not rewritten")
	}
	got, err := io.ReadAll(r.Body)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(got), `"include_usage":true`) {
		t.Errorf("body = %q, want stream_options added", got)
	}
	if r.ContentLength != int64(len(got)) {
		t.Errorf("ContentLength = %d, want %d", r.ContentLength, len(got))
	}
	// The engine has to be told the new length, or it reads a truncated body and
	// answers with a parse error that mentions nothing about metering.
	if want := strconv.FormatInt(r.ContentLength, 10); r.Header.Get("Content-Length") != want {
		t.Errorf("Content-Length header = %q, want %q", r.Header.Get("Content-Length"), want)
	}
}
