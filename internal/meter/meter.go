// Package meter counts what each peer uses.
//
// This is the prerequisite for everything else. You cannot fairly share, limit
// or charge for capacity you do not count, and a host that cannot say who is
// using it cannot answer the only question that matters when someone pins the
// GPU: who did that?
//
// Counting happens here rather than in the engine, because the engine is a black
// box we proxy to and may be any of Ollama, vLLM or llama.cpp.
package meter

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"sort"
	"strings"
	"sync"
	"time"
)

// Usage is what one response cost, as reported by the engine.
type Usage struct {
	PromptTokens     int `json:"prompt_tokens"`
	CompletionTokens int `json:"completion_tokens"`
	TotalTokens      int `json:"total_tokens"`
}

// Extract reads token usage out of a single JSON object.
//
// It understands both shapes a host might be proxying: the OpenAI nested "usage"
// object, and Ollama's flat prompt_eval_count / eval_count. An engine that
// reports nothing yields nothing, which callers record honestly as unmetered
// rather than guessing at a number.
func Extract(data []byte) (Usage, bool) {
	if len(data) == 0 {
		return Usage{}, false
	}
	var probe struct {
		Usage *struct {
			PromptTokens     int `json:"prompt_tokens"`
			CompletionTokens int `json:"completion_tokens"`
			TotalTokens      int `json:"total_tokens"`
		} `json:"usage"`
		PromptEvalCount  int `json:"prompt_eval_count"`
		EvalCount        int `json:"eval_count"`
		PromptTokens     int `json:"prompt_tokens"`
		CompletionTokens int `json:"completion_tokens"`
		TotalTokens      int `json:"total_tokens"`
	}
	if err := json.Unmarshal(data, &probe); err != nil {
		return Usage{}, false
	}

	var usage Usage
	switch {
	case probe.Usage != nil:
		usage.PromptTokens = probe.Usage.PromptTokens
		usage.CompletionTokens = probe.Usage.CompletionTokens
		usage.TotalTokens = probe.Usage.TotalTokens
	case probe.PromptEvalCount != 0 || probe.EvalCount != 0:
		// Ollama's native shape.
		usage.PromptTokens = probe.PromptEvalCount
		usage.CompletionTokens = probe.EvalCount
	case probe.PromptTokens != 0 || probe.CompletionTokens != 0 || probe.TotalTokens != 0:
		usage.PromptTokens = probe.PromptTokens
		usage.CompletionTokens = probe.CompletionTokens
		usage.TotalTokens = probe.TotalTokens
	default:
		return Usage{}, false
	}
	if usage.TotalTokens == 0 {
		usage.TotalTokens = usage.PromptTokens + usage.CompletionTokens
	}
	return usage, true
}

const (
	maxPending = 256 << 10
	maxBody    = 8 << 20
)

// Sniffer wraps an upstream response body, passing every byte through untouched
// while reading usage out of the stream.
//
// The two shapes need different treatment. A whole JSON body is parsed once at
// the end. A server-sent-events stream is parsed frame by frame as it goes,
// holding nothing back — a sniffer that buffered a stream would undo the entire
// point of streaming, so it never delays a byte on the way to the client.
type Sniffer struct {
	rc       io.ReadCloser
	stream   bool
	pending  []byte
	body     []byte
	overflow bool
	done     bool
	usage    Usage
	reported bool
	bytes    int64
}

// NewSniffer wraps body. contentType decides whether it is read as a stream.
func NewSniffer(body io.ReadCloser, contentType string) *Sniffer {
	return &Sniffer{rc: body, stream: strings.HasPrefix(contentType, "text/event-stream")}
}

func (s *Sniffer) Read(p []byte) (int, error) {
	n, err := s.rc.Read(p)
	if n > 0 {
		s.bytes += int64(n)
		switch {
		case s.stream:
			s.scan(p[:n])
		case s.overflow:
			// Body too large to inspect; bytes still pass through.
		default:
			if len(s.body)+n > maxBody {
				s.overflow = true
				s.body = nil
			} else {
				s.body = append(s.body, p[:n]...)
			}
		}
	}
	if err != nil && !s.stream && !s.done {
		s.done = true
		if !s.overflow {
			if usage, ok := Extract(s.body); ok {
				s.usage, s.reported = usage, true
			}
		}
		s.body = nil
	}
	return n, err
}

// scan pulls complete data lines out of the stream, leaving the bytes untouched.
func (s *Sniffer) scan(chunk []byte) {
	s.pending = append(s.pending, chunk...)
	for {
		i := bytes.IndexByte(s.pending, '\n')
		if i < 0 {
			break
		}
		s.line(s.pending[:i])
		s.pending = s.pending[i+1:]
	}
	if len(s.pending) > maxPending {
		s.pending = s.pending[:0] // malformed stream; stop trying to parse it
	}
}

func (s *Sniffer) line(raw []byte) {
	data, ok := bytes.CutPrefix(bytes.TrimSpace(raw), []byte("data:"))
	if !ok {
		return
	}
	data = bytes.TrimSpace(data)
	if len(data) == 0 || bytes.Equal(data, []byte("[DONE]")) {
		return
	}
	if usage, ok := Extract(data); ok {
		s.merge(usage)
	}
}

// merge keeps the latest non-zero numbers: engines send zeroes early and the
// running totals in the final frame.
func (s *Sniffer) merge(usage Usage) {
	if usage.PromptTokens != 0 {
		s.usage.PromptTokens = usage.PromptTokens
	}
	if usage.CompletionTokens != 0 {
		s.usage.CompletionTokens = usage.CompletionTokens
	}
	if usage.TotalTokens != 0 {
		s.usage.TotalTokens = usage.TotalTokens
	}
	s.reported = true
}

// Usage reports what the response cost. Reported is false when the engine said
// nothing, so callers can distinguish "small" from "unknown".
func (s *Sniffer) Usage() (Usage, bool) { return s.usage, s.reported }

// Bytes reports how many response bytes passed through.
func (s *Sniffer) Bytes() int64 { return s.bytes }

func (s *Sniffer) Close() error { return s.rc.Close() }

// Reasons a request can be refused.
const (
	ReasonConcurrency = "concurrency"
	ReasonRate        = "rate"
)

// LimitError says why a request was refused. Concurrency is about the host,
// rate is about the peer.
type LimitError struct {
	Peer       string
	Reason     string
	RetryAfter time.Duration
}

func (e *LimitError) Error() string {
	switch e.Reason {
	case ReasonConcurrency:
		return "host is already serving its maximum concurrent requests; retry shortly"
	case ReasonRate:
		return fmt.Sprintf("peer %q exceeded its request rate; retry in %s",
			e.Peer, e.RetryAfter.Round(time.Second))
	default:
		return "request refused by the host limiter"
	}
}

// Counter is what one peer has used since the process started.
type Counter struct {
	Requests         int64     `json:"requests"`
	Limited          int64     `json:"limited"`
	PromptTokens     int64     `json:"prompt_tokens"`
	CompletionTokens int64     `json:"completion_tokens"`
	ResponseBytes    int64     `json:"response_bytes"`
	Unmetered        int64     `json:"unmetered_responses"`
	LastSeen         time.Time `json:"last_seen"`
}

// PeerUsage is one row of the usage report.
type PeerUsage struct {
	Peer     string `json:"peer"`
	InFlight int    `json:"in_flight"`
	Counter
}

// Options configures the limiter.
type Options struct {
	// MaxConcurrent caps requests in flight across the whole host. A GPU
	// serialises work anyway, so this is the limit that actually protects it.
	// Zero means no cap.
	MaxConcurrent int
	// RequestsPerMinute caps one peer's request rate, with a burst of the same
	// size. Zero means no cap.
	RequestsPerMinute int
}

// Meter tracks per-peer usage and enforces the host's limits.
type Meter struct {
	opts Options

	mu       sync.Mutex
	peers    map[string]*peerState
	inFlight int
}

type peerState struct {
	usage    Counter
	inFlight int
	tokens   float64
	refill   time.Time
}

// New returns a meter with the given limits.
func New(opts Options) *Meter {
	return &Meter{opts: opts, peers: make(map[string]*peerState)}
}

// Begin reserves capacity for one request. Every successful Begin must be paired
// with exactly one End.
func (m *Meter) Begin(peer string, now time.Time) error {
	m.mu.Lock()
	defer m.mu.Unlock()

	state := m.peer(peer)
	if m.opts.MaxConcurrent > 0 && m.inFlight >= m.opts.MaxConcurrent {
		state.usage.Limited++
		return &LimitError{Peer: peer, Reason: ReasonConcurrency}
	}
	if !m.takeToken(state, now) {
		state.usage.Limited++
		return &LimitError{Peer: peer, Reason: ReasonRate, RetryAfter: m.retryAfter(state)}
	}
	m.inFlight++
	state.inFlight++
	return nil
}

// End releases the slot Begin reserved and records what the request cost.
func (m *Meter) End(peer string, usage Usage, reported bool, responseBytes int64, now time.Time) {
	m.mu.Lock()
	defer m.mu.Unlock()

	state := m.peer(peer)
	state.usage.Requests++
	if reported {
		state.usage.PromptTokens += int64(usage.PromptTokens)
		state.usage.CompletionTokens += int64(usage.CompletionTokens)
	} else {
		state.usage.Unmetered++
	}
	state.usage.ResponseBytes += responseBytes
	state.usage.LastSeen = now
	if state.inFlight > 0 {
		state.inFlight--
	}
	if m.inFlight > 0 {
		m.inFlight--
	}
}

// Capacity reports how many requests the host could take right now. This is what
// gets advertised, so clients route to whoever is least busy. Zero means either
// fully busy or uncapped-and-therefore-unknown.
func (m *Meter) Capacity() int {
	m.mu.Lock()
	defer m.mu.Unlock()
	if m.opts.MaxConcurrent <= 0 {
		return 0
	}
	free := m.opts.MaxConcurrent - m.inFlight
	if free < 0 {
		free = 0
	}
	return free
}

// InFlight reports how many requests are being served right now.
func (m *Meter) InFlight() int {
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.inFlight
}

// Snapshot returns one row per peer, heaviest user first.
func (m *Meter) Snapshot() []PeerUsage {
	m.mu.Lock()
	defer m.mu.Unlock()

	out := make([]PeerUsage, 0, len(m.peers))
	for name, state := range m.peers {
		out = append(out, PeerUsage{Peer: name, InFlight: state.inFlight, Counter: state.usage})
	}
	sort.Slice(out, func(i, j int) bool {
		left := out[i].PromptTokens + out[i].CompletionTokens
		right := out[j].PromptTokens + out[j].CompletionTokens
		if left != right {
			return left > right
		}
		return out[i].Peer < out[j].Peer
	})
	return out
}

func (m *Meter) peer(name string) *peerState {
	state, ok := m.peers[name]
	if !ok {
		state = &peerState{}
		m.peers[name] = state
	}
	return state
}

// takeToken applies a token bucket: a peer may spend a minute's worth of
// requests as a burst, then refills continuously.
func (m *Meter) takeToken(state *peerState, now time.Time) bool {
	if m.opts.RequestsPerMinute <= 0 {
		return true
	}
	rate := float64(m.opts.RequestsPerMinute)
	if state.refill.IsZero() {
		state.tokens = rate
	} else {
		state.tokens += now.Sub(state.refill).Minutes() * rate
		if state.tokens > rate {
			state.tokens = rate
		}
	}
	state.refill = now
	if state.tokens < 1 {
		return false
	}
	state.tokens--
	return true
}

// retryAfter estimates how long until the peer's bucket holds one request again.
func (m *Meter) retryAfter(state *peerState) time.Duration {
	if m.opts.RequestsPerMinute <= 0 || state.tokens >= 1 {
		return 0
	}
	minutes := (1 - state.tokens) / float64(m.opts.RequestsPerMinute)
	return time.Duration(minutes * float64(time.Minute))
}
