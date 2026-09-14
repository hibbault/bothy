package mockengine

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

func getRaw(t *testing.T, url string) (int, map[string]any) {
	t.Helper()
	resp, err := http.Get(url)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	body, err := io.ReadAll(resp.Body)
	if err != nil {
		t.Fatal(err)
	}
	var out map[string]any
	if err := json.Unmarshal(body, &out); err != nil {
		t.Fatalf("%s is not JSON: %v (%q)", url, err, body)
	}
	return resp.StatusCode, out
}

func TestHealthNamesTheEngine(t *testing.T) {
	srv := newMock(t)
	code, body := getRaw(t, srv.URL+"/healthz")
	if code != http.StatusOK {
		t.Fatalf("status = %d, want 200", code)
	}
	if body["ok"] != true || body["engine"] != "mock" || body["name"] != "mock-a" {
		t.Fatalf("healthz = %+v", body)
	}
}

// Ollama's own API, because `bothy share -engine-kind ollama` is meant to work
// against the mock exactly as it does against a real Ollama.
func TestTagsMirrorOllama(t *testing.T) {
	srv := newMock(t)
	code, body := getRaw(t, srv.URL+"/api/tags")
	if code != http.StatusOK {
		t.Fatalf("status = %d, want 200", code)
	}
	models, ok := body["models"].([]any)
	if !ok || len(models) != 1 {
		t.Fatalf("models = %#v, want one", body["models"])
	}
	first, _ := models[0].(map[string]any)
	if first["name"] != "llama3.1:8b" || first["model"] != "llama3.1:8b" || first["digest"] != digestA {
		t.Errorf("tag = %#v", first)
	}
	if _, err := time.Parse(time.RFC3339, first["modified_at"].(string)); err != nil {
		t.Errorf("modified_at = %#v, want RFC3339: %v", first["modified_at"], err)
	}
}

// A real /v1/models reports ids and no digests, and that emptiness is the whole
// reason the openai lister merges a configured digest by name. If the mock leaked
// a digest, that path would look exercised without being exercised.
func TestOpenAIModelsListsIDsOnly(t *testing.T) {
	srv := newMock(t)
	code, body := getRaw(t, srv.URL+"/v1/models")
	if code != http.StatusOK {
		t.Fatalf("status = %d, want 200", code)
	}
	if body["object"] != "list" {
		t.Errorf("object = %#v, want list", body["object"])
	}
	data, ok := body["data"].([]any)
	if !ok || len(data) != 1 {
		t.Fatalf("data = %#v", body["data"])
	}
	card, _ := data[0].(map[string]any)
	if card["id"] != "llama3.1:8b" || card["object"] != "model" {
		t.Errorf("card = %#v", card)
	}
	if _, leaked := card["digest"]; leaked {
		t.Errorf("card carries a digest, which a real OpenAI endpoint does not: %#v", card)
	}
}

// The older completions endpoint is what plenty of tooling still sends.
func TestCompletionsStreamsFramesAndUsageWhenAsked(t *testing.T) {
	for _, tc := range []struct {
		name        string
		includeUsag bool
		wantUsage   bool
	}{
		{"asked for usage", true, true},
		{"did not ask", false, false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			srv := newMock(t)
			payload := map[string]any{
				"model":  "llama3.1:8b",
				"stream": true,
				"prompt": "one two",
			}
			if tc.includeUsag {
				payload["stream_options"] = map[string]any{"include_usage": true}
			}
			resp := postJSON(t, srv.URL+"/v1/completions", payload)
			defer resp.Body.Close()
			if ct := resp.Header.Get("Content-Type"); !strings.HasPrefix(ct, "text/event-stream") {
				t.Fatalf("Content-Type = %q, want text/event-stream", ct)
			}
			raw, err := io.ReadAll(resp.Body)
			if err != nil {
				t.Fatal(err)
			}
			text := string(raw)
			if !strings.Contains(text, "text_completion") {
				t.Errorf("no completion frames in %q", text)
			}
			if !strings.Contains(text, "data: [DONE]") {
				t.Errorf("the stream did not terminate: %q", text)
			}
			usage := streamedUsage(t, raw)
			if (usage != nil) != tc.wantUsage {
				t.Errorf("usage = %v, want present = %v: %q", usage, tc.wantUsage, text)
			}
		})
	}
}

// A refusal has to be OpenAI-shaped, or a client shows a blank failure instead of
// the reason.
func TestInvalidJSONIsRefusedInTheDocumentedShape(t *testing.T) {
	srv := newMock(t)
	for _, path := range []string{"/v1/chat/completions", "/v1/completions"} {
		resp, err := http.Post(srv.URL+path, "application/json", strings.NewReader("not json"))
		if err != nil {
			t.Fatal(err)
		}
		body, _ := io.ReadAll(resp.Body)
		resp.Body.Close()
		if resp.StatusCode != http.StatusBadRequest {
			t.Errorf("%s = %d, want 400", path, resp.StatusCode)
		}
		var payload struct {
			Error struct {
				Message string `json:"message"`
			} `json:"error"`
		}
		if err := json.Unmarshal(body, &payload); err != nil {
			t.Fatalf("%s body is not the documented error shape: %s", path, body)
		}
		if payload.Error.Message == "" {
			t.Errorf("%s refusal carries no message: %s", path, body)
		}
	}
}

// The reply names the engine and the digest that produced it, which is how a
// client can tell whose GPU answered — and it must not pretend to know a model it
// was not configured with.
func TestTheReplyNamesTheEngineAndAdmitsAnUnknownModel(t *testing.T) {
	srv := newMock(t)

	resp := postJSON(t, srv.URL+"/v1/chat/completions", map[string]any{
		"model":    "not-installed:1b",
		"messages": []map[string]string{{"role": "user", "content": ""}},
	})
	defer resp.Body.Close()
	raw, err := io.ReadAll(resp.Body)
	if err != nil {
		t.Fatal(err)
	}
	text := string(raw)
	if !strings.Contains(text, "unknown-model") {
		t.Errorf("an unknown model was answered with a digest it does not have: %s", text)
	}
	if !strings.Contains(text, "(empty prompt)") {
		t.Errorf("an empty prompt was not said to be empty: %s", text)
	}
}

// A stream must stop when the caller goes away, or a `-delay` demo holds a slot
// on a real host for as long as the engine feels like it.
func TestStreamingStopsWhenTheCallerGoesAway(t *testing.T) {
	s := &Server{
		Config: Config{
			Name:   "mock-a",
			Models: []model.Model{{Name: "llama3.1:8b", Digest: digestA}},
			Delay:  20 * time.Millisecond,
		},
		Log: slog.New(slog.NewTextHandler(io.Discard, nil)),
	}
	srv := httptest.NewServer(s.Handler())
	defer srv.Close()

	ctx, cancel := context.WithCancel(context.Background())
	body := strings.NewReader(`{"model":"llama3.1:8b","stream":true,"prompt":"a b c d e f g h i j k l"}`)
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, srv.URL+"/v1/chat/completions", body)
	if err != nil {
		t.Fatal(err)
	}
	req.Header.Set("Content-Type", "application/json")

	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()

	buf := make([]byte, 64)
	if _, err := resp.Body.Read(buf); err != nil {
		t.Fatalf("no chunk arrived before the cancel: %v", err)
	}
	cancel()

	done := make(chan string, 1)
	go func() {
		raw, _ := io.ReadAll(resp.Body)
		done <- string(raw)
	}()
	select {
	case rest := <-done:
		if strings.Contains(rest, "data: [DONE]") {
			t.Errorf("the stream ran to completion after the caller left: %q", rest)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("the handler kept streaming after the caller went away")
	}
}

func TestRunRefusesConfigurationsItCannotServe(t *testing.T) {
	log := slog.New(slog.NewTextHandler(io.Discard, nil))
	ctx := context.Background()

	t.Run("a malformed model list", func(t *testing.T) {
		if err := Run(ctx, log, []string{"-models", "=sha256:abc"}); err == nil {
			t.Fatal("a model list with no name was accepted")
		}
	})

	t.Run("no models at all", func(t *testing.T) {
		err := Run(ctx, log, []string{"-models", ""})
		if err == nil {
			t.Fatal("a mock with nothing to serve started anyway")
		}
		if !strings.Contains(err.Error(), "no models") {
			t.Errorf("error %q does not say what is missing", err)
		}
	})

	t.Run("an address it cannot bind", func(t *testing.T) {
		if err := Run(ctx, log, []string{"-listen", "127.0.0.1:not-a-port"}); err == nil {
			t.Fatal("an unbindable listen address was accepted")
		}
	})
}

// The command lives behind flags and environment defaults, so the wiring itself
// gets a test: the name from -name must be what the engine reports.
func TestRunServesUntilCancelled(t *testing.T) {
	addr := freeAddr(t)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	done := make(chan error, 1)
	go func() {
		done <- Run(ctx, slog.New(slog.NewTextHandler(io.Discard, nil)), []string{
			"-listen", addr,
			"-name", "from-flags",
			"-models", "llama3.1:8b=" + digestA,
		})
	}()

	client := &http.Client{Timeout: 2 * time.Second}
	var body map[string]any
	for i := 0; i < 100; i++ {
		code, out := 0, map[string]any{}
		resp, err := client.Get("http://" + addr + "/healthz")
		if err == nil {
			code = resp.StatusCode
			b, _ := io.ReadAll(resp.Body)
			resp.Body.Close()
			_ = json.Unmarshal(b, &out)
		}
		if code == http.StatusOK {
			body = out
			break
		}
		time.Sleep(20 * time.Millisecond)
	}
	if body == nil {
		t.Fatal("the mock engine never answered, so this proves nothing")
	}
	if body["name"] != "from-flags" {
		t.Errorf("name = %#v, want the -name value", body["name"])
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
