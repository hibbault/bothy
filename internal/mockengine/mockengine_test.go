package mockengine

import (
	"bytes"
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/hibbault/bothy/internal/model"
)

func newMock(t *testing.T) *httptest.Server {
	t.Helper()
	s := &Server{
		Config: Config{
			Name:   "mock-a",
			Models: []model.Model{{Name: "llama3.1:8b", Digest: digestA}},
		},
		Log: slog.New(slog.NewTextHandler(io.Discard, nil)),
	}
	srv := httptest.NewServer(s.Handler())
	t.Cleanup(srv.Close)
	return srv
}

func postJSON(t *testing.T, url string, payload any) *http.Response {
	t.Helper()
	body, err := json.Marshal(payload)
	if err != nil {
		t.Fatal(err)
	}
	resp, err := http.Post(url, "application/json", bytes.NewReader(body))
	if err != nil {
		t.Fatal(err)
	}
	return resp
}

// This is the route Bothy reads to learn digests; without it the mock cannot
// stand in for a real engine.
func TestInternalModelsReportsDigests(t *testing.T) {
	srv := newMock(t)
	resp, err := http.Get(srv.URL + "/internal/models")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	var payload struct {
		Models []model.Model `json:"models"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&payload); err != nil {
		t.Fatal(err)
	}
	if len(payload.Models) != 1 || payload.Models[0].Digest != digestA {
		t.Fatalf("models = %+v", payload.Models)
	}
}

func TestChatCompletionNamesTheEngineAndDigest(t *testing.T) {
	srv := newMock(t)
	resp := postJSON(t, srv.URL+"/v1/chat/completions", map[string]any{
		"model":    "llama3.1:8b",
		"messages": []map[string]string{{"role": "user", "content": "hello there"}},
	})
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("status = %d", resp.StatusCode)
	}
	var out struct {
		Model   string `json:"model"`
		Choices []struct {
			Message struct {
				Content string `json:"content"`
			} `json:"message"`
		} `json:"choices"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
		t.Fatal(err)
	}
	if len(out.Choices) != 1 {
		t.Fatalf("choices = %+v", out.Choices)
	}
	content := out.Choices[0].Message.Content
	if !strings.Contains(content, "mock-a") {
		t.Errorf("reply does not name the engine: %q", content)
	}
	if !strings.Contains(content, digestA) {
		t.Errorf("reply does not name the digest: %q", content)
	}
	if !strings.Contains(content, "hello there") {
		t.Errorf("reply does not echo the prompt: %q", content)
	}
}

// Streaming is the path that breaks if the response is buffered, so assert the
// frames actually arrive.
func TestChatCompletionStreamsServerSentEvents(t *testing.T) {
	srv := newMock(t)
	resp := postJSON(t, srv.URL+"/v1/chat/completions", map[string]any{
		"model":    "llama3.1:8b",
		"stream":   true,
		"messages": []map[string]string{{"role": "user", "content": "one two three"}},
	})
	defer resp.Body.Close()
	if ct := resp.Header.Get("Content-Type"); !strings.HasPrefix(ct, "text/event-stream") {
		t.Fatalf("Content-Type = %q, want text/event-stream", ct)
	}
	raw, err := io.ReadAll(resp.Body)
	if err != nil {
		t.Fatal(err)
	}
	text := string(raw)
	if !strings.Contains(text, "chat.completion.chunk") {
		t.Errorf("no chunk frames in %q", text)
	}
	if !strings.Contains(text, "data: [DONE]") {
		t.Errorf("no terminator in %q", text)
	}
	// Three words, plus a finish chunk, plus [DONE].
	if n := strings.Count(text, "data: "); n < 4 {
		t.Errorf("got %d frames, want at least 4: %q", n, text)
	}
}

// Tooling still sends the older completions shape, and content can be a list of
// typed parts rather than a string.
func TestContentPartsAreFlattened(t *testing.T) {
	srv := newMock(t)
	resp := postJSON(t, srv.URL+"/v1/chat/completions", map[string]any{
		"model": "llama3.1:8b",
		"messages": []any{
			map[string]any{"role": "user", "content": []any{
				map[string]any{"type": "text", "text": "part one"},
				map[string]any{"type": "text", "text": "part two"},
			}},
		},
	})
	defer resp.Body.Close()
	raw, err := io.ReadAll(resp.Body)
	if err != nil {
		t.Fatal(err)
	}
	// Parts are concatenated exactly, with no separator inserted: a split can
	// land mid-word, so inventing a space would corrupt the prompt.
	if !strings.Contains(string(raw), "part onepart two") {
		t.Fatalf("content parts were not flattened: %s", raw)
	}
}

func TestCompletionsEndpointWorks(t *testing.T) {
	srv := newMock(t)
	resp := postJSON(t, srv.URL+"/v1/completions", map[string]any{
		"model":  "llama3.1:8b",
		"prompt": "say hi",
	})
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("status = %d", resp.StatusCode)
	}
	raw, err := io.ReadAll(resp.Body)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(raw), "say hi") {
		t.Fatalf("prompt was not echoed: %s", raw)
	}
}
