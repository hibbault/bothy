//go:build swarm

package swarm

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"log/slog"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"net/http/httptest"

	"github.com/hibbault/bothy/internal/mockengine"
	"github.com/hibbault/bothy/internal/model"
)

// mockChat runs the same mock engine the rest of the repo's tests use, in
// process, so a swarm run needs no GPU and no network. The mock's replies are
// deterministic and name the digest that produced them, which is what makes
// agreement testable at all.
func mockChat(t *testing.T) *Chat {
	t.Helper()
	srv := httptest.NewServer((&mockengine.Server{
		Config: mockengine.Config{
			Name:   "mock",
			Models: []model.Model{{Name: "llama3.1:8b", Digest: "sha256:1111111111111111111111111111111111111111111111111111111111111111"}},
		},
		Log: slog.New(slog.NewTextHandler(io.Discard, nil)),
	}).Handler())
	t.Cleanup(srv.Close)
	return &Chat{BaseURL: srv.URL}
}

func testOptions(t *testing.T) Options {
	t.Helper()
	return Options{
		Dir:          filepath.Join(t.TempDir(), "bothy"),
		WorkDir:      t.TempDir(),
		Model:        "llama3.1:8b",
		CheckTimeout: 20 * time.Second,
		NodeTimeout:  20 * time.Second,
	}
}

func TestBestOfNIsAccepted(t *testing.T) {
	// The shape the whole design is for: three identical prompts, one shared
	// check on each attempt, and a join that can only pass if they agree.
	task := mustPlan(t, `{"goal":"three attempts at one prompt","model":"llama3.1:8b",
		"accept":{"type":"agreement","nodes":["attempt-1","attempt-2-1","attempt-2-2"]},
		"nodes":[
		  {"id":"attempt-1","prompt":"name the capital of France","check":{"type":"command","run":"grep -q 'you-said' \"$BOTHY_ARTIFACT\""}},
		  {"id":"attempt-2","same_as":"attempt-1","count":2},
		  {"id":"join","kind":"verify","depends_on":["attempt-1","attempt-2-1","attempt-2-2"],
		   "check":{"type":"agreement","nodes":["attempt-1","attempt-2-1","attempt-2-2"]}}]}`)

	opts := testOptions(t)
	rep := run(context.Background(), task, mockChat(t), opts)

	if !rep.Accepted {
		t.Fatalf("not accepted: %s", rep.Error)
	}
	if len(rep.Nodes) != 4 {
		t.Fatalf("got %d outcomes, want 4 (%+v)", len(rep.Nodes), rep.Nodes)
	}
	for _, o := range rep.Nodes {
		if o.Status != StatusOK {
			t.Errorf("node %s: status %s (%s)", o.Node, o.Status, o.Error)
		}
		if o.Kind == KindSolve && o.Digest == "" {
			t.Errorf("node %s produced no digest", o.Node)
		}
	}
	if !strings.Contains(rep.Verdict, "3 of 3 agree") {
		t.Errorf("verdict = %q", rep.Verdict)
	}

	// The artifacts have to be on disk and readable by path: a check gets the
	// path, not the text, so a store that only keeps bytes in memory would make
	// command checks impossible.
	for _, o := range rep.Nodes {
		if o.Digest == "" {
			continue
		}
		path := filepath.Join(opts.Dir, "artifacts", strings.TrimPrefix(o.Digest, "sha256:"))
		b, err := os.ReadFile(path)
		if err != nil {
			t.Fatalf("artifact for %s: %v", o.Node, err)
		}
		if !strings.Contains(string(b), "you-said") {
			t.Errorf("artifact for %s does not look like the engine's reply: %q", o.Node, b)
		}
	}
}

func TestIdenticalAttemptsShareOneArtifact(t *testing.T) {
	// Content addressing is not decoration: three identical replies are one file,
	// and the digest is how a report says which bytes were judged.
	// Two attempts and no join: the accept compares them, so there is nothing to
	// resolve as "the answer" and nothing to guess at.
	task := mustPlan(t, `{"goal":"g","accept":{"type":"agreement","nodes":["a","b"]},
		"nodes":[{"id":"a","prompt":"same"},{"id":"b","same_as":"a"}]}`)
	opts := testOptions(t)
	rep := run(context.Background(), task, mockChat(t), opts)
	if !rep.Accepted {
		t.Fatalf("not accepted: %s", rep.Error)
	}
	if rep.Nodes[0].Digest != rep.Nodes[1].Digest {
		t.Errorf("identical replies got different digests: %s vs %s", rep.Nodes[0].Digest, rep.Nodes[1].Digest)
	}
	entries, err := os.ReadDir(filepath.Join(opts.Dir, "artifacts"))
	if err != nil {
		t.Fatal(err)
	}
	if len(entries) != 1 {
		t.Errorf("got %d artifact files for one distinct reply, want 1", len(entries))
	}
}

func TestAFailedCheckStopsTheRunAndSkipsTheRest(t *testing.T) {
	task := mustPlan(t, `{"goal":"g","model":"llama3.1:8b",
		"accept":{"type":"exact","want":"42"},
		"nodes":[{"id":"a","prompt":"p","check":{"type":"exact","want":"definitely not the reply"}},
		         {"id":"b","prompt":"q","depends_on":["a"]}]}`)

	rep := run(context.Background(), task, mockChat(t), testOptions(t))
	if rep.Accepted {
		t.Fatal("a failed check was accepted")
	}
	if rep.Nodes[0].Status != StatusFailed {
		t.Errorf("node a: status %s, want failed", rep.Nodes[0].Status)
	}
	if rep.Nodes[1].Status != StatusSkipped {
		t.Errorf("node b: status %s, want skipped — nothing should run after a failure", rep.Nodes[1].Status)
	}
	if !strings.Contains(rep.Error, `node "a" failed`) {
		t.Errorf("error = %q", rep.Error)
	}
	if !strings.Contains(rep.Nodes[0].Error, "exact") && !strings.Contains(rep.Nodes[0].Error, "artifact is") {
		t.Errorf("the failure does not say what the check wanted: %q", rep.Nodes[0].Error)
	}
}

func TestAVerifierCanBeTheAnswer(t *testing.T) {
	// The verifier produces nothing, so "the answer" is the attempt's artifact —
	// judged again through the verifier. This is the re-run-the-check path.
	task := mustPlan(t, `{"goal":"g","model":"llama3.1:8b","solution":"check",
		"accept":{"type":"command","run":"grep -q 'digest=sha256:1111' \"$BOTHY_ARTIFACT\""},
		"nodes":[{"id":"attempt","prompt":"say something"},
		         {"id":"check","kind":"verify","depends_on":["attempt"],
		          "check":{"type":"command","run":"test -s \"$BOTHY_ARTIFACT\""}}]}`)

	rep := run(context.Background(), task, mockChat(t), testOptions(t))
	if !rep.Accepted {
		t.Fatalf("not accepted: %s", rep.Error)
	}
	if rep.Digest == "" {
		t.Error("no digest: the accepted artifact should be named")
	}
	for _, o := range rep.Nodes {
		if o.Node == "check" && o.Digest != "" {
			t.Error("a verifier should not claim to have produced an artifact")
		}
	}
}

func TestAnAcceptThatFailsIsReported(t *testing.T) {
	task := mustPlan(t, `{"goal":"g","model":"llama3.1:8b",
		"accept":{"type":"command","run":"grep -q 'nothing says this' \"$BOTHY_ARTIFACT\""},
		"nodes":[{"id":"a","prompt":"p","check":{"type":"command","run":"true"}}]}`)

	rep := run(context.Background(), task, mockChat(t), testOptions(t))
	if rep.Accepted {
		t.Fatal("accepted a task whose accept criterion fails")
	}
	if !strings.Contains(rep.Error, "accept:") {
		t.Errorf("error = %q, want it to name the accept step", rep.Error)
	}
	if rep.Nodes[0].Status != StatusOK {
		t.Errorf("the attempt itself passed its own check, so it should be ok, got %s", rep.Nodes[0].Status)
	}
}

func TestANodeWithNoModelIsRefused(t *testing.T) {
	task := mustPlan(t, `{"goal":"g","accept":{"type":"exact","want":"x"},
		"nodes":[{"id":"a","prompt":"p"}]}`)
	opts := testOptions(t)
	opts.Model = ""

	rep := run(context.Background(), task, mockChat(t), opts)
	if rep.Accepted {
		t.Fatal("a run with no model was accepted")
	}
	if !strings.Contains(rep.Error, "no model") {
		t.Errorf("error = %q", rep.Error)
	}
}

func TestStoreIsContentAddressed(t *testing.T) {
	s := &store{dir: t.TempDir()}
	first, err := s.put("a", []byte("same bytes"))
	if err != nil {
		t.Fatal(err)
	}
	second, err := s.put("b", []byte("same bytes"))
	if err != nil {
		t.Fatal(err)
	}
	third, err := s.put("c", []byte("different"))
	if err != nil {
		t.Fatal(err)
	}
	if first.Digest != second.Digest || first.Path != second.Path {
		t.Errorf("the same bytes landed twice: %s@%s vs %s@%s", first.Digest, first.Path, second.Digest, second.Path)
	}
	if third.Digest == first.Digest {
		t.Error("different bytes share a digest")
	}
	if !strings.HasPrefix(first.Digest, "sha256:") || len(first.Digest) != len("sha256:")+64 {
		t.Errorf("digest %q is not a sha256", first.Digest)
	}
	b, err := os.ReadFile(third.Path)
	if err != nil {
		t.Fatal(err)
	}
	if string(b) != "different" {
		t.Errorf("stored %q", b)
	}
	// A half-written artifact must never appear under a digest claiming to be
	// complete, so the temp files the store uses have to be cleaned up.
	entries, err := os.ReadDir(s.dir)
	if err != nil {
		t.Fatal(err)
	}
	for _, e := range entries {
		if strings.HasPrefix(e.Name(), ".partial-") {
			t.Errorf("left a partial artifact behind: %s", e.Name())
		}
	}
}

func TestReportIsWrittenEvenWhenTheRunFailed(t *testing.T) {
	task := mustPlan(t, `{"goal":"g","model":"llama3.1:8b",
		"accept":{"type":"exact","want":"42"},
		"nodes":[{"id":"a","prompt":"p","check":{"type":"exact","want":"nope"}}]}`)
	opts := testOptions(t)
	rep := run(context.Background(), task, mockChat(t), opts)

	if err := writeReport(opts.Dir, rep); err != nil {
		t.Fatal(err)
	}
	b, err := os.ReadFile(filepath.Join(opts.Dir, "report.json"))
	if err != nil {
		t.Fatal(err)
	}
	var back Report
	if err := json.Unmarshal(b, &back); err != nil {
		t.Fatal(err)
	}
	if back.Accepted || back.Goal != "g" || back.Error == "" {
		t.Errorf("the report lost something: %+v", back)
	}
	if len(back.Nodes) != 1 || back.Nodes[0].Status != StatusFailed {
		t.Errorf("the report's nodes are wrong: %+v", back.Nodes)
	}
}

func TestPrintPlanDescribesWhatWouldRun(t *testing.T) {
	task := mustPlan(t, `{"goal":"make the failing test pass","model":"llama3.1:8b",
		"accept":{"type":"command","run":"go test ./..."},
		"nodes":[{"id":"attempt","prompt":"fix it","check":{"type":"command","run":"go test ./..."}}]}`)

	var buf bytes.Buffer
	printPlan(&buf, task, "")
	out := buf.String()
	for _, want := range []string{
		"make the failing test pass",
		"llama3.1:8b",
		"command: go test ./...",
		"solution: attempt",
		"1 nodes, in the order they will run",
		"nothing has run",
	} {
		if !strings.Contains(out, want) {
			t.Errorf("plan does not mention %q:\n%s", want, out)
		}
	}
}

func TestSolveRefusesWithoutATaskFile(t *testing.T) {
	log := slog.New(slog.NewTextHandler(io.Discard, nil))
	if err := Run(context.Background(), log, nil); err == nil {
		t.Fatal("solve with no -task was accepted")
	}
	if err := Run(context.Background(), log, []string{"-task", filepath.Join(t.TempDir(), "missing.json")}); err == nil {
		t.Fatal("solve with a missing task file was accepted")
	}
}
