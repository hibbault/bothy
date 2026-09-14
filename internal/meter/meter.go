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
	ReasonQuota       = "quota"
)

// LimitError says why a request was refused. Concurrency is about the host;
// rate and quota are about the peer.
type LimitError struct {
	Peer       string
	Reason     string
	RetryAfter time.Duration
	// Quota and Window are set for ReasonQuota, so that a refusal can say what
	// the budget was rather than only that it is gone.
	Quota  int
	Window time.Duration
}

func (e *LimitError) Error() string {
	switch e.Reason {
	case ReasonConcurrency:
		return "the host is serving as many peer requests as it allows right now; retry shortly"
	case ReasonRate:
		return fmt.Sprintf("peer %q exceeded its request rate; retry in %s",
			e.Peer, e.RetryAfter.Round(time.Second))
	case ReasonQuota:
		return fmt.Sprintf("peer %q has used its budget of %d requests per %s; retry in %s",
			e.Peer, e.Quota, e.Window, e.RetryAfter.Round(time.Second))
	default:
		return "request refused by the host limiter"
	}
}

// Quota is a request budget over a window: a peer may make Requests requests,
// and then waits for the window to turn over.
//
// It is a budget rather than a rate, which is the difference between slowing
// somebody down and stopping them. A rate limit of 30 a minute permits 43,200
// requests a day, for ever; a quota of 200 an hour permits 200, and then stops.
//
// It counts requests and not tokens, which is a limitation rather than a
// preference. Tokens are known only after a response has been produced, so a
// token budget can be enforced only retrospectively — and an engine that reports
// no usage, which is allowed, would evade it entirely. Requests are counted
// before the work starts, so a request budget always binds. The tokens are still
// there in the usage report for the owner to judge by.
type Quota struct {
	Requests int
	Window   time.Duration
}

// Enabled reports whether a quota is actually configured.
func (q Quota) Enabled() bool { return q.Requests > 0 && q.Window > 0 }

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
	// QuotaUsed and QuotaReset appear only when a quota is configured: how much
	// of the current window this peer has spent, and when it turns over. Without
	// them an owner watching a peer stop has no way to tell a budget from a
	// crash.
	QuotaUsed  int    `json:"quota_used,omitempty"`
	QuotaReset string `json:"quota_reset,omitempty"`
}

// Options configures the limiter.
type Options struct {
	// MaxConcurrent caps requests in flight across the whole host. A GPU
	// serialises work anyway, so this is the limit that actually protects it.
	// Zero means no cap.
	MaxConcurrent int
	// OwnerReserve is how many slots are kept for the machine's owner out of
	// MaxConcurrent. Peers are capped at MaxConcurrent - OwnerReserve, so the
	// person paying for the electricity always has headroom and never queues
	// behind strangers.
	//
	// This is a guarantee of headroom, not a reading of what the owner is doing.
	// Their own traffic never passes through here — they talk to their engine
	// directly — and no portable engine API reports whether it is busy, so there
	// is nothing to detect. A reservation is the honest shape available.
	OwnerReserve int
	// PeerQuota caps one peer's requests over a window. Zero means no budget.
	PeerQuota Quota
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
	// windowStart is when this peer's current budget window began, and
	// windowUsed is what it has spent in it. The window starts at the peer's own
	// first admitted request rather than on a shared clock, so budgets do not all
	// turn over at once and "200 an hour" means an hour from when you started.
	windowStart time.Time
	windowUsed  int
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
	// Decide before consuming. A request refused by one limit must not spend
	// another limit's allowance, or a peer turned away by a full host would also
	// lose a request of its budget for work that was never done.
	if err := m.checkLimits(peer, state, now); err != nil {
		state.usage.Limited++
		return err
	}
	m.consume(state, now)
	m.inFlight++
	state.inFlight++
	return nil
}

// checkLimits decides whether a request may start, without spending anything.
//
// The order decides which refusal a peer is told about when more than one
// applies. The host's own capacity comes first because it is not the peer's
// doing, then the peer's budget, which is the more final of the two answers,
// then its rate.
func (m *Meter) checkLimits(peer string, state *peerState, now time.Time) error {
	if m.opts.MaxConcurrent > 0 && m.inFlight >= m.peerSlots() {
		return &LimitError{Peer: peer, Reason: ReasonConcurrency}
	}
	if q := m.opts.PeerQuota; q.Enabled() {
		if !m.windowOpen(state, now) && state.windowUsed >= q.Requests {
			return &LimitError{
				Peer: peer, Reason: ReasonQuota,
				RetryAfter: state.windowStart.Add(q.Window).Sub(now),
				Quota:      q.Requests, Window: q.Window,
			}
		}
	}
	if !m.hasToken(state, now) {
		return &LimitError{Peer: peer, Reason: ReasonRate, RetryAfter: m.retryAfterAt(state, now)}
	}
	return nil
}

// windowOpen reports whether the peer's budget window has turned over, in which
// case its spend resets.
func (m *Meter) windowOpen(state *peerState, now time.Time) bool {
	q := m.opts.PeerQuota
	return state.windowStart.IsZero() || now.Sub(state.windowStart) >= q.Window
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

// Capacity reports how many requests this host could take from peers right now.
// This is what gets advertised, so clients route to whoever is least busy. Zero
// means either fully busy or uncapped-and-therefore-unknown.
//
// It reports *peer* capacity, so a reserved slot is not counted and does not get
// advertised. That is what makes the reservation propagate without a client
// needing to understand it: a host with one slot free for its owner looks full
// to everybody else.
func (m *Meter) Capacity() int {
	m.mu.Lock()
	defer m.mu.Unlock()
	if m.opts.MaxConcurrent <= 0 {
		return 0
	}
	free := m.peerSlots() - m.inFlight
	if free < 0 {
		free = 0
	}
	return free
}

// Quota reports the per-peer budget this meter enforces, if any.
func (m *Meter) Quota() Quota {
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.opts.PeerQuota
}

// PeerSlots reports how many requests peers may have in flight at once, which is
// the cap less the slots kept for the owner. Zero means uncapped.
func (m *Meter) PeerSlots() int {
	m.mu.Lock()
	defer m.mu.Unlock()
	if m.opts.MaxConcurrent <= 0 {
		return 0
	}
	return m.peerSlots()
}

// peerSlots is the concurrency available to peers. Callers hold the lock.
//
// A reserve that swallows the whole cap yields zero rather than a negative, and
// the caller's `MaxConcurrent > 0` test means "no slots" rather than "no limit":
// a misconfiguration must fail closed.
func (m *Meter) peerSlots() int {
	slots := m.opts.MaxConcurrent - m.opts.OwnerReserve
	if slots < 0 {
		return 0
	}
	return slots
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

	quota := m.opts.PeerQuota
	out := make([]PeerUsage, 0, len(m.peers))
	for name, state := range m.peers {
		row := PeerUsage{Peer: name, InFlight: state.inFlight, Counter: state.usage}
		if quota.Enabled() {
			// A window that has already turned over is reported as spent-nothing
			// rather than as whatever it held when it last ran.
			if state.windowStart.IsZero() {
				row.QuotaReset = ""
			} else {
				reset := state.windowStart.Add(quota.Window)
				if !reset.After(time.Now()) {
					row.QuotaReset = ""
				} else {
					row.QuotaUsed = state.windowUsed
					row.QuotaReset = reset.UTC().Format(time.RFC3339)
				}
			}
		}
		out = append(out, row)
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

// consume spends what checkLimits allowed: one rate token and one request of the
// peer's budget.
func (m *Meter) consume(state *peerState, now time.Time) {
	if m.opts.RequestsPerMinute > 0 {
		state.tokens = m.available(state, now) - 1
		state.refill = now
	}
	if q := m.opts.PeerQuota; q.Enabled() {
		if m.windowOpen(state, now) {
			state.windowStart = now
			state.windowUsed = 0
		}
		state.windowUsed++
	}
}

// available is how many tokens the peer's bucket would hold at now, without
// spending any. A token bucket lets a peer spend a minute's worth of requests as
// a burst, then refills continuously.
func (m *Meter) available(state *peerState, now time.Time) float64 {
	if m.opts.RequestsPerMinute <= 0 {
		return 1
	}
	rate := float64(m.opts.RequestsPerMinute)
	if state.refill.IsZero() {
		return rate
	}
	tokens := state.tokens + now.Sub(state.refill).Minutes()*rate
	if tokens > rate {
		tokens = rate
	}
	return tokens
}

func (m *Meter) hasToken(state *peerState, now time.Time) bool {
	return m.opts.RequestsPerMinute <= 0 || m.available(state, now) >= 1
}

// retryAfterAt estimates how long until the peer's bucket holds one request
// again.
func (m *Meter) retryAfterAt(state *peerState, now time.Time) time.Duration {
	if m.opts.RequestsPerMinute <= 0 {
		return 0
	}
	tokens := m.available(state, now)
	if tokens >= 1 {
		return 0
	}
	minutes := (1 - tokens) / float64(m.opts.RequestsPerMinute)
	return time.Duration(minutes * float64(time.Minute))
}
