package host

import (
	"bytes"
	"encoding/json"
	"io"
	"net/http"
	"strconv"
)

// Streamed replies are where the meter goes blind.
//
// A whole response reports its token usage and the sniffer reads it, so nothing
// has to be asked for. A stream only reports usage if the engine decides to put
// it in the stream, and an OpenAI-compatible engine generally will not unless
// the request asks for it:
//
//	"stream_options": {"include_usage": true}
//
// So a host whose peers all stream could serve for hours and show nothing, and
// streaming is how interactive use arrives.
//
// The host asks on the caller's behalf. This is the one place a proxy in Bothy
// rewrites a request body, and it is deliberately narrow: two routes, only when
// the caller asked to stream, and never when the caller has already said
// anything about stream_options. An explicit choice is never overridden.
//
// The cost is that the body has to be read before it is forwarded, where the
// rest of the proxy streams it through untouched. maxInjectable bounds that: a
// body larger than this (a prompt carrying images, say) is forwarded as it is
// and simply lands in the meter as unmetered, which is what would have happened
// anyway.

const (
	// maxInjectable is how much of a request body will be read in order to add
	// two short fields to it. Past this the request is forwarded untouched.
	maxInjectable = 1 << 20

	streamOptionsField = "stream_options"
)

// streamingRoutes are the OpenAI routes that can stream, and the only ones this
// touches. The engine's native API has its own shape — Ollama spells the same
// idea differently and reports usage without being asked — and Bothy does not
// guess at a body it has not been taught.
var streamingRoutes = map[string]bool{
	"/v1/chat/completions": true,
	"/v1/completions":      true,
}

// wantsStreamUsage reports whether this request is one the host should ask the
// engine for usage on.
func wantsStreamUsage(r *http.Request) bool {
	return r.Method == http.MethodPost && streamingRoutes[r.URL.Path]
}

// addStreamUsage returns body with stream_options.include_usage set, and whether
// it changed anything. It is a pure function of the body, so the decision table
// is testable without a server.
func addStreamUsage(body []byte) ([]byte, bool) {
	var object map[string]json.RawMessage
	if err := json.Unmarshal(body, &object); err != nil {
		return body, false // not a JSON object, so not ours to touch
	}

	var stream bool
	if raw, ok := object["stream"]; !ok || json.Unmarshal(raw, &stream) != nil || !stream {
		return body, false // not a streamed request: nothing to ask for
	}
	if _, taken := object[streamOptionsField]; taken {
		return body, false // the caller has an opinion; leave it alone
	}
	object[streamOptionsField] = json.RawMessage(`{"include_usage":true}`)

	// Re-encoding loses the caller's key order and whitespace. For a request
	// body that is acceptable — an engine reads it as JSON rather than as text —
	// and it is the price of adding a field without a parser that can work on a
	// stream.
	rewritten, err := json.Marshal(object)
	if err != nil {
		return body, false
	}
	return rewritten, true
}

// readCloser pairs a replacement reader with the body it replaced, so that the
// original is still closed and its connection is not leaked.
type readCloser struct {
	io.Reader
	io.Closer
}

// injectStreamUsage reads and rewrites r.Body when the request is one that
// should ask for streamed usage. It reports whether the body changed.
func injectStreamUsage(r *http.Request) bool {
	if !wantsStreamUsage(r) {
		return false
	}
	original := r.Body
	if r.ContentLength > maxInjectable {
		return false // too big to buffer, so forward it as it stands
	}

	body, err := io.ReadAll(io.LimitReader(original, maxInjectable+1))
	if err != nil {
		// Part-read bodies cannot be put back together, so this request is
		// forwarded with what was read and the engine will report the error.
		r.Body = readCloser{io.MultiReader(bytes.NewReader(body), original), original}
		return false
	}
	if len(body) > maxInjectable {
		r.Body = readCloser{io.MultiReader(bytes.NewReader(body), original), original}
		return false
	}

	rewritten, changed := addStreamUsage(body)
	if !changed {
		r.Body = readCloser{io.MultiReader(bytes.NewReader(body), original), original}
		return false
	}

	r.Body = readCloser{bytes.NewReader(rewritten), original}
	r.ContentLength = int64(len(rewritten))
	r.Header.Set("Content-Length", strconv.Itoa(len(rewritten)))
	return true
}
