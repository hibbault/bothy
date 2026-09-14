package httpx

import (
	"bytes"
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

// countingReader reports how much was actually read, because Snippet's whole
// point is that it does not buffer a whole body to log a warning.
type countingReader struct {
	data string
	read int
}

func (c *countingReader) Read(p []byte) (int, error) {
	if c.read >= len(c.data) {
		return 0, io.EOF
	}
	n := copy(p, c.data[c.read:])
	c.read += n
	return n, nil
}

func TestSnippetReadsAtMostN(t *testing.T) {
	r := &countingReader{data: "0123456789abcdef"}
	if got := Snippet(r, 4); got != "0123" {
		t.Errorf("Snippet = %q, want the first 4 bytes", got)
	}
	if r.read != 4 {
		t.Errorf("Snippet read %d bytes, want 4 — an error body must not be buffered whole", r.read)
	}
}

func TestSnippetTrimsWhitespace(t *testing.T) {
	if got := Snippet(strings.NewReader("  \n  no such model \n "), 300); got != "no such model" {
		t.Errorf("Snippet = %q, want the trimmed body", got)
	}
}

// The screenshot people look at when something breaks is this line, so it has to
// carry the fields it claims to: method, path, status, bytes and duration.
func TestLogRequestsRecordsOneLinePerRequest(t *testing.T) {
	var buf bytes.Buffer
	log := slog.New(slog.NewJSONHandler(&buf, nil))
	handler := LogRequests(log, http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusCreated)
		_, _ = io.WriteString(w, "hello")
	}))

	handler.ServeHTTP(httptest.NewRecorder(), httptest.NewRequest(http.MethodPost, "/v1/chat/completions", nil))

	lines := strings.Split(strings.TrimSpace(buf.String()), "\n")
	if len(lines) != 1 {
		t.Fatalf("got %d log lines, want exactly 1: %q", len(lines), buf.String())
	}
	var record map[string]any
	if err := json.Unmarshal([]byte(lines[0]), &record); err != nil {
		t.Fatalf("log line is not JSON: %v (%q)", err, lines[0])
	}
	for field, want := range map[string]any{
		"method": "POST",
		"path":   "/v1/chat/completions",
		"status": float64(http.StatusCreated),
		"bytes":  float64(len("hello")),
	} {
		if got := record[field]; got != want {
			t.Errorf("log %s = %#v, want %#v", field, got, want)
		}
	}
	if d, _ := record["duration"].(string); !strings.HasSuffix(d, "s") {
		t.Errorf("log duration = %#v, want a duration string", record["duration"])
	}
}

// A handler that only writes a body never calls WriteHeader, and the log must
// still say 200 rather than 0.
func TestRecorderDefaultsToOK(t *testing.T) {
	var buf bytes.Buffer
	log := slog.New(slog.NewJSONHandler(&buf, nil))
	handler := LogRequests(log, http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, _ = io.WriteString(w, "ok")
	}))
	handler.ServeHTTP(httptest.NewRecorder(), httptest.NewRequest(http.MethodGet, "/", nil))

	var record map[string]any
	if err := json.Unmarshal(buf.Bytes(), &record); err != nil {
		t.Fatal(err)
	}
	if got := record["status"]; got != float64(http.StatusOK) {
		t.Errorf("status = %#v, want 200 for a handler that wrote a body", got)
	}
}

// Wrapping a response must not cost streaming. Flush has to reach the real
// writer, and Unwrap has to expose it so anything expecting a richer writer can
// still find it.
func TestRecorderKeepsStreamingWorking(t *testing.T) {
	underlying := httptest.NewRecorder()
	rec := &recorder{ResponseWriter: underlying, status: http.StatusOK}

	flusher, ok := any(rec).(http.Flusher)
	if !ok {
		t.Fatal("the recorder does not implement Flusher, so a streamed reply would be buffered")
	}
	flusher.Flush()
	if !underlying.Flushed {
		t.Error("Flush did not reach the wrapped writer")
	}
	if got := rec.Unwrap(); got != http.ResponseWriter(underlying) {
		t.Errorf("Unwrap = %#v, want the wrapped writer", got)
	}
}

func TestRecorderCountsWhatWasWritten(t *testing.T) {
	rec := &recorder{ResponseWriter: httptest.NewRecorder(), status: http.StatusOK}
	rec.WriteHeader(http.StatusAccepted)
	n, err := rec.Write([]byte("abcdef"))
	if err != nil || n != 6 {
		t.Fatalf("Write = %d, %v", n, err)
	}
	if _, err := rec.Write([]byte("gh")); err != nil {
		t.Fatal(err)
	}
	if rec.status != http.StatusAccepted {
		t.Errorf("status = %d, want it captured from WriteHeader", rec.status)
	}
	if rec.bytes != 8 {
		t.Errorf("bytes = %d, want 8", rec.bytes)
	}
}

// A nil payload is how the code says "status only" — an empty 204 with a JSON
// content type, not the string "null".
func TestJSONNilWritesNoBody(t *testing.T) {
	rec := httptest.NewRecorder()
	JSON(rec, http.StatusNoContent, nil)
	if rec.Code != http.StatusNoContent {
		t.Errorf("status = %d, want 204", rec.Code)
	}
	if body := rec.Body.String(); body != "" {
		t.Errorf("body = %q, want empty", body)
	}
	if ct := rec.Header().Get("Content-Type"); ct != "application/json" {
		t.Errorf("Content-Type = %q", ct)
	}
}

func TestTokenFromEdgeCases(t *testing.T) {
	for _, tc := range []struct {
		name string
		key  string
		auth string
		want string
	}{
		{"nothing at all", "", "", ""},
		{"a key header", "k", "", "k"},
		{"a bearer token", "", "Bearer k", "k"},
		{"a lowercase bearer", "", "bearer k", "k"},
		{"extra whitespace", "", "Bearer   k  ", "k"},
		{"a bare scheme with no credential", "", "Bearer", ""},
		{"a scheme we do not use", "", "Basic k", ""},
		{"a blank key header falls back to bearer", "   ", "Bearer k", "k"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			req := httptest.NewRequest(http.MethodGet, "/", nil)
			if tc.key != "" {
				req.Header.Set(KeyHeader, tc.key)
			}
			if tc.auth != "" {
				req.Header.Set("Authorization", tc.auth)
			}
			if got := TokenFrom(req, KeyHeader); got != tc.want {
				t.Errorf("TokenFrom = %q, want %q", got, tc.want)
			}
		})
	}
}

// A key of the wrong length must be refused rather than compared byte by byte,
// which is the whole reason the comparison is constant time.
func TestRequireTokenRefusesADifferentLength(t *testing.T) {
	handler := RequireToken("correct-horse", http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		JSON(w, http.StatusOK, map[string]any{"ok": true})
	}))
	for _, key := range []string{"correct", "correct-horse-battery", "CORRECT-HORSE", ""} {
		rec := httptest.NewRecorder()
		req := httptest.NewRequest(http.MethodGet, "/", nil)
		if key != "" {
			req.Header.Set(KeyHeader, key)
		}
		handler.ServeHTTP(rec, req)
		if rec.Code != http.StatusUnauthorized {
			t.Errorf("key %q got %d, want 401", key, rec.Code)
		}
	}
}
