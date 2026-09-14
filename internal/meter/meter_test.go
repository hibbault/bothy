package meter

import (
	"bytes"
	"errors"
	"fmt"
	"io"
	"strings"
	"testing"
	"time"
)

func TestExtractOpenAIUsage(t *testing.T) {
	body := []byte(`{"choices":[{"message":{"content":"hi"}}],"usage":{"prompt_tokens":11,"completion_tokens":7,"total_tokens":18}}`)
	usage, ok := Extract(body)
	if !ok {
		t.Fatal("expected usage to be found")
	}
	if usage.PromptTokens != 11 || usage.CompletionTokens != 7 || usage.TotalTokens != 18 {
		t.Fatalf("usage = %+v", usage)
	}
}

// Ollama's native shape has no nested usage object and no total, so the total has
// to be derived.
func TestExtractOllamaUsage(t *testing.T) {
	body := []byte(`{"model":"llama3.1:8b","done":true,"prompt_eval_count":9,"eval_count":4}`)
	usage, ok := Extract(body)
	if !ok {
		t.Fatal("expected usage to be found")
	}
	if usage.PromptTokens != 9 || usage.CompletionTokens != 4 || usage.TotalTokens != 13 {
		t.Fatalf("usage = %+v, want 9/4/13", usage)
	}
}

func TestExtractIgnoresObjectsWithNoCounts(t *testing.T) {
	for _, body := range []string{
		`{"choices":[{"delta":{"content":"hello"}}]}`,
		`{"model":"llama3.1:8b","done":false,"response":"hi"}`,
		`not json at all`,
		``,
	} {
		if usage, ok := Extract([]byte(body)); ok {
			t.Errorf("Extract(%q) = %+v, true; want no usage", body, usage)
		}
	}
}

// chunkReader hands out bytes in exactly the pieces given, so a test can split a
// stream anywhere — including mid-JSON, which is what a naive line parser gets
// wrong.
type chunkReader struct {
	chunks [][]byte
}

func (c *chunkReader) Read(p []byte) (int, error) {
	for len(c.chunks) > 0 {
		chunk := c.chunks[0]
		if len(chunk) == 0 {
			c.chunks = c.chunks[1:]
			continue
		}
		n := copy(p, chunk)
		if n < len(chunk) {
			c.chunks[0] = chunk[n:]
			return n, nil
		}
		c.chunks = c.chunks[1:]
		return n, nil
	}
	return 0, io.EOF
}

func (c *chunkReader) Close() error { return nil }

func TestSnifferPassesBytesThroughUnchanged(t *testing.T) {
	body := []byte(`{"usage":{"prompt_tokens":3,"completion_tokens":2,"total_tokens":5}}`)
	sniffer := NewSniffer(&chunkReader{chunks: [][]byte{body}}, "application/json")

	got, err := io.ReadAll(sniffer)
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(got, body) {
		t.Fatalf("sniffer altered the body:\n got %q\nwant %q", got, body)
	}
	usage, reported := sniffer.Usage()
	if !reported || usage.TotalTokens != 5 {
		t.Fatalf("usage = %+v, reported = %v", usage, reported)
	}
	if sniffer.Bytes() != int64(len(body)) {
		t.Fatalf("Bytes() = %d, want %d", sniffer.Bytes(), len(body))
	}
}

// The same stream is replayed split at every awkward boundary, because a sniffer
// that only worked on whole frames would miss the usage line in practice.
func TestSnifferHandlesAnySplitBoundary(t *testing.T) {
	body := strings.Join([]string{
		"data: {\"choices\":[{\"delta\":{\"content\":\"one\"}}]}\n\n",
		"data: {\"choices\":[{\"delta\":{\"content\":\"two\"}}]}\n\n",
		"data: {\"usage\":{\"prompt_tokens\":8,\"completion_tokens\":5,\"total_tokens\":13}}\n\n",
		"data: [DONE]\n\n",
	}, "")

	for _, size := range []int{1, 2, 3, 5, 7, 13, 40, 256, len(body)} {
		t.Run(fmt.Sprintf("chunk=%d", size), func(t *testing.T) {
			var chunks [][]byte
			for i := 0; i < len(body); i += size {
				end := i + size
				if end > len(body) {
					end = len(body)
				}
				chunks = append(chunks, []byte(body[i:end]))
			}
			sniffer := NewSniffer(&chunkReader{chunks: chunks}, "text/event-stream")

			got, err := io.ReadAll(sniffer)
			if err != nil {
				t.Fatal(err)
			}
			if string(got) != body {
				t.Fatal("sniffer altered the stream")
			}
			usage, reported := sniffer.Usage()
			if !reported || usage.PromptTokens != 8 || usage.CompletionTokens != 5 || usage.TotalTokens != 13 {
				t.Fatalf("usage = %+v, reported = %v", usage, reported)
			}
		})
	}
}

func TestSnifferMergesUsageFromLaterFrames(t *testing.T) {
	// An engine that streams partial counts must not lose them to a later zero.
	body := "data: {\"prompt_eval_count\":5,\"eval_count\":1}\n\n" +
		"data: {\"done\":true,\"prompt_eval_count\":5,\"eval_count\":9}\n\n"
	sniffer := NewSniffer(&chunkReader{chunks: [][]byte{[]byte(body)}}, "text/event-stream")
	if _, err := io.ReadAll(sniffer); err != nil {
		t.Fatal(err)
	}
	usage, reported := sniffer.Usage()
	if !reported || usage.PromptTokens != 5 || usage.CompletionTokens != 9 {
		t.Fatalf("usage = %+v, want the final counts", usage)
	}
}

func TestSnifferReportsNothingWhenTheEngineSaysNothing(t *testing.T) {
	body := `{"choices":[{"message":{"content":"hi"}}]}`
	sniffer := NewSniffer(&chunkReader{chunks: [][]byte{[]byte(body)}}, "application/json")
	if _, err := io.ReadAll(sniffer); err != nil {
		t.Fatal(err)
	}
	if _, reported := sniffer.Usage(); reported {
		t.Fatal("an engine that reports no usage must not be recorded as reporting zero")
	}
	if sniffer.Bytes() != int64(len(body)) {
		t.Fatalf("Bytes() = %d, want %d", sniffer.Bytes(), len(body))
	}
}

func TestMeterConcurrencyCap(t *testing.T) {
	m := New(Options{MaxConcurrent: 2})
	now := time.Now()

	if err := m.Begin("alice", now); err != nil {
		t.Fatalf("first request refused: %v", err)
	}
	if err := m.Begin("bob", now); err != nil {
		t.Fatalf("second request refused: %v", err)
	}
	err := m.Begin("alice", now)
	var limit *LimitError
	if !errors.As(err, &limit) || limit.Reason != ReasonConcurrency {
		t.Fatalf("third request = %v, want a concurrency LimitError", err)
	}

	m.End("alice", Usage{}, false, 0, now)
	if err := m.Begin("bob", now); err != nil {
		t.Fatalf("request refused after a slot was released: %v", err)
	}
}

func TestMeterRateLimitRefills(t *testing.T) {
	m := New(Options{RequestsPerMinute: 3})
	now := time.Now()

	for i := 0; i < 3; i++ {
		if err := m.Begin("alice", now); err != nil {
			t.Fatalf("request %d of the burst refused: %v", i+1, err)
		}
		m.End("alice", Usage{}, false, 0, now)
	}

	err := m.Begin("alice", now)
	var limit *LimitError
	if !errors.As(err, &limit) || limit.Reason != ReasonRate {
		t.Fatalf("fourth request = %v, want a rate LimitError", err)
	}
	if limit.RetryAfter <= 0 {
		t.Fatal("a rate limit must tell the peer how long to wait")
	}

	// A third of a minute later, one token has refilled.
	if err := m.Begin("alice", now.Add(20*time.Second)); err != nil {
		t.Fatalf("request refused after the bucket refilled: %v", err)
	}
}

func TestMeterRatesAreIsolatedPerPeer(t *testing.T) {
	m := New(Options{RequestsPerMinute: 1})
	now := time.Now()

	if err := m.Begin("alice", now); err != nil {
		t.Fatal(err)
	}
	m.End("alice", Usage{}, false, 0, now)
	if err := m.Begin("alice", now); err == nil {
		t.Fatal("alice should be rate limited")
	}
	if err := m.Begin("bob", now); err != nil {
		t.Fatalf("bob was punished for alice's usage: %v", err)
	}
}

func TestMeterCapacityFollowsLoad(t *testing.T) {
	m := New(Options{MaxConcurrent: 3})
	now := time.Now()

	if got := m.Capacity(); got != 3 {
		t.Fatalf("Capacity() = %d, want 3", got)
	}
	if err := m.Begin("alice", now); err != nil {
		t.Fatal(err)
	}
	if got := m.Capacity(); got != 2 {
		t.Fatalf("Capacity() = %d, want 2", got)
	}
	m.End("alice", Usage{}, false, 0, now)
	if got := m.Capacity(); got != 3 {
		t.Fatalf("Capacity() = %d after release, want 3", got)
	}
}

func TestMeterUncappedAdvertisesUnknownCapacity(t *testing.T) {
	m := New(Options{})
	if got := m.Capacity(); got != 0 {
		t.Fatalf("Capacity() = %d, want 0 for an uncapped host", got)
	}
	if err := m.Begin("alice", time.Now()); err != nil {
		t.Fatalf("an uncapped meter refused a request: %v", err)
	}
}

// Reported-but-zero differs from unreported, and the counters have to keep them
// apart, or "the engine said nothing" would look like "the engine said free".
func TestMeterDistinguishesUnmeteredFromFree(t *testing.T) {
	m := New(Options{})
	now := time.Now()

	if err := m.Begin("alice", now); err != nil {
		t.Fatal(err)
	}
	m.End("alice", Usage{PromptTokens: 10, CompletionTokens: 4, TotalTokens: 14}, true, 100, now)
	if err := m.Begin("alice", now); err != nil {
		t.Fatal(err)
	}
	m.End("alice", Usage{}, false, 50, now)

	rows := m.Snapshot()
	if len(rows) != 1 {
		t.Fatalf("got %d rows, want 1", len(rows))
	}
	row := rows[0]
	if row.Requests != 2 || row.PromptTokens != 10 || row.CompletionTokens != 4 {
		t.Fatalf("row = %+v", row)
	}
	if row.Unmetered != 1 {
		t.Fatalf("Unmetered = %d, want 1", row.Unmetered)
	}
	if row.ResponseBytes != 150 {
		t.Fatalf("ResponseBytes = %d, want 150", row.ResponseBytes)
	}
	if row.Limited != 0 {
		t.Fatalf("Limited = %d, want 0", row.Limited)
	}
}

func TestMeterSnapshotPutsHeaviestPeerFirst(t *testing.T) {
	m := New(Options{})
	now := time.Now()

	if err := m.Begin("small", now); err != nil {
		t.Fatal(err)
	}
	m.End("small", Usage{PromptTokens: 1, CompletionTokens: 1}, true, 0, now)
	if err := m.Begin("big", now); err != nil {
		t.Fatal(err)
	}
	m.End("big", Usage{PromptTokens: 900, CompletionTokens: 100}, true, 0, now)

	rows := m.Snapshot()
	if len(rows) != 2 || rows[0].Peer != "big" {
		t.Fatalf("rows = %+v, want big first", rows)
	}
}

func TestMeterCountsRefusals(t *testing.T) {
	m := New(Options{MaxConcurrent: 1})
	now := time.Now()

	if err := m.Begin("alice", now); err != nil {
		t.Fatal(err)
	}
	if err := m.Begin("alice", now); err == nil {
		t.Fatal("expected a refusal")
	}
	rows := m.Snapshot()
	if len(rows) != 1 || rows[0].Limited != 1 {
		t.Fatalf("rows = %+v, want one refused request recorded", rows)
	}
}
