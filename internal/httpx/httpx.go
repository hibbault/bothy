// Package httpx holds the small HTTP helpers every Bothy service shares: peer
// token auth, JSON responses, and request logging.
package httpx

import (
	"crypto/subtle"
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
	"strings"
	"time"
)

// KeyHeader lets a peer present its key without pretending it is a Bearer token.
const KeyHeader = "X-Bothy-Key"

// TokenFrom extracts a peer key from the X-Bothy-Key header or from
// Authorization: Bearer.
func TokenFrom(r *http.Request, header string) string {
	if h := strings.TrimSpace(r.Header.Get(header)); h != "" {
		return h
	}
	a := strings.TrimSpace(r.Header.Get("Authorization"))
	for _, prefix := range []string{"Bearer ", "bearer "} {
		if after, ok := strings.CutPrefix(a, prefix); ok {
			return strings.TrimSpace(after)
		}
	}
	return ""
}

// RequireToken rejects requests that don't carry token. An empty token leaves
// the handler open — callers should warn loudly when that happens, because on a
// reachable port it means anyone can spend your GPU.
func RequireToken(token string, next http.Handler) http.Handler {
	if token == "" {
		return next
	}
	want := []byte(token)
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		got := []byte(TokenFrom(r, KeyHeader))
		if subtle.ConstantTimeCompare(got, want) != 1 {
			Error(w, http.StatusUnauthorized, "missing or invalid key")
			return
		}
		next.ServeHTTP(w, r)
	})
}

// JSON writes v as a JSON response.
func JSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	if v == nil {
		return
	}
	enc := json.NewEncoder(w)
	enc.SetIndent("", "  ")
	_ = enc.Encode(v)
}

// Error writes an OpenAI-shaped JSON error, so clients that already parse
// `error.message` show something useful instead of a blank failure.
func Error(w http.ResponseWriter, status int, msg string) {
	JSON(w, status, map[string]any{
		"error": map[string]any{
			"message": msg,
			"type":    "bothy_error",
		},
	})
}

// Snippet reads up to n bytes of a body for logging without buffering all of it.
func Snippet(rc io.Reader, n int) string {
	b, _ := io.ReadAll(io.LimitReader(rc, int64(n)))
	return strings.TrimSpace(string(b))
}

// LogRequests logs one line per request with its status and duration.
func LogRequests(log *slog.Logger, next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		start := time.Now()
		rec := &recorder{ResponseWriter: w, status: http.StatusOK}
		next.ServeHTTP(rec, r)
		log.Info("request",
			"method", r.Method,
			"path", r.URL.Path,
			"status", rec.status,
			"bytes", rec.bytes,
			"duration", time.Since(start).Round(time.Millisecond).String(),
		)
	})
}

// recorder captures the status code for logging. It implements Flusher and
// Unwrap so wrapping does not break streaming responses.
type recorder struct {
	http.ResponseWriter
	status int
	bytes  int
}

func (r *recorder) WriteHeader(status int) {
	r.status = status
	r.ResponseWriter.WriteHeader(status)
}

func (r *recorder) Write(b []byte) (int, error) {
	n, err := r.ResponseWriter.Write(b)
	r.bytes += n
	return n, err
}

func (r *recorder) Flush() {
	if f, ok := r.ResponseWriter.(http.Flusher); ok {
		f.Flush()
	}
}

func (r *recorder) Unwrap() http.ResponseWriter { return r.ResponseWriter }
