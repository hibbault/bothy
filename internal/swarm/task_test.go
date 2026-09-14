//go:build swarm

package swarm

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// plan writes a task file and loads it, which is the only way a task reaches the
// runner.
func plan(t *testing.T, src string) (Task, error) {
	t.Helper()
	path := filepath.Join(t.TempDir(), "task.json")
	if err := os.WriteFile(path, []byte(src), 0o644); err != nil {
		t.Fatal(err)
	}
	return LoadTask(path)
}

func mustPlan(t *testing.T, src string) Task {
	t.Helper()
	task, err := plan(t, src)
	if err != nil {
		t.Fatalf("task refused: %v", err)
	}
	return task
}

const nodeIDs = `[{"id":"a","prompt":"p","check":{"type":"exact","want":"42"}}]`

// TestTaskRefusals is the interesting table: every one of these is a task that
// looks like it asks for something and does not.
func TestTaskRefusals(t *testing.T) {
	cases := []struct {
		name    string
		src     string
		wantErr string
	}{
		{
			name:    "no goal",
			src:     `{"accept":{"type":"exact","want":"42"},"nodes":` + nodeIDs + `}`,
			wantErr: "needs a goal",
		},
		{
			name: "no accept criterion",
			src:  `{"goal":"g","nodes":` + nodeIDs + `}`,
			// The headline rule: without an accept there is nothing to check the
			// work against, so it cannot be farmed.
			wantErr: "needs an accept criterion",
		},
		{
			name:    "an accept with no type",
			src:     `{"goal":"g","accept":{},"nodes":` + nodeIDs + `}`,
			wantErr: "needs an accept criterion",
		},
		{
			name:    "no nodes",
			src:     `{"goal":"g","accept":{"type":"exact","want":"42"}}`,
			wantErr: "at least one node",
		},
		{
			name:    "an accept that is not a known check",
			src:     `{"goal":"g","accept":{"type":"vibes"},"nodes":` + nodeIDs + `}`,
			wantErr: "unknown check type",
		},
		{
			name:    "an accept with nothing to check against",
			src:     `{"goal":"g","accept":{"type":"command"},"nodes":` + nodeIDs + `}`,
			wantErr: "needs run",
		},
		{
			name:    "a misspelled field",
			src:     `{"goal":"g","accept":{"type":"exact","want":"42"},"nodes":` + nodeIDs + `,"sovle":true}`,
			wantErr: "unknown field",
		},
		{
			name:    "two task objects",
			src:     `{"goal":"g","accept":{"type":"exact","want":"42"},"nodes":` + nodeIDs + `} {}`,
			wantErr: "unexpected content",
		},
		{
			name:    "a node with no id",
			src:     `{"goal":"g","accept":{"type":"exact","want":"42"},"nodes":[{"prompt":"p"}]}`,
			wantErr: "no id",
		},
		{
			name: "duplicate node ids",
			src: `{"goal":"g","accept":{"type":"exact","want":"42"},
			       "nodes":[{"id":"a","prompt":"p"},{"id":"a","prompt":"q"}]}`,
			wantErr: "duplicate node id",
		},
		{
			name:    "a solve node with no prompt",
			src:     `{"goal":"g","accept":{"type":"exact","want":"42"},"nodes":[{"id":"a"}]}`,
			wantErr: "needs a prompt",
		},
		{
			name:    "an unknown node kind",
			src:     `{"goal":"g","accept":{"type":"exact","want":"42"},"nodes":[{"id":"a","kind":"think","prompt":"p"}]}`,
			wantErr: "unknown kind",
		},
		{
			name: "a verify node that produces",
			src: `{"goal":"g","accept":{"type":"exact","want":"42"},
			       "nodes":[{"id":"a","prompt":"p"},{"id":"v","kind":"verify","prompt":"also solve it","depends_on":["a"],"check":{"type":"exact","want":"42"}}]}`,
			// The split between producing and checking is the design; a checker
			// that produces is a producer wearing a verifier's name.
			wantErr: "cannot have a prompt",
		},
		{
			name: "a verify node with nothing to judge",
			src: `{"goal":"g","accept":{"type":"exact","want":"42"},
			       "nodes":[{"id":"a","prompt":"p"},{"id":"v","kind":"verify","check":{"type":"exact","want":"42"}}]}`,
			wantErr: "needs depends_on",
		},
		{
			name: "a verify node with no check",
			src: `{"goal":"g","accept":{"type":"exact","want":"42"},
			       "nodes":[{"id":"a","prompt":"p"},{"id":"v","kind":"verify","depends_on":["a"]}]}`,
			wantErr: "with no check does nothing",
		},
		{
			name: "a dependency on nothing",
			src: `{"goal":"g","accept":{"type":"exact","want":"42"},
			       "nodes":[{"id":"a","prompt":"p","depends_on":["ghost"]}]}`,
			wantErr: "not a node in this task",
		},
		{
			name: "a node that depends on itself",
			src: `{"goal":"g","accept":{"type":"exact","want":"42"},
			       "nodes":[{"id":"a","prompt":"p","depends_on":["a"]}]}`,
			wantErr: "depends_on names itself",
		},
		{
			name: "a cycle",
			src: `{"goal":"g","accept":{"type":"exact","want":"42"},
			       "nodes":[{"id":"a","prompt":"p","depends_on":["b"]},{"id":"b","prompt":"p","depends_on":["a"]}]}`,
			wantErr: "cycle",
		},
		{
			name: "a requirement the runner cannot honour",
			src: `{"goal":"g","accept":{"type":"exact","want":"42"},
			       "nodes":[{"id":"a","prompt":"p","needs":{"digest":"sha256:11"}}]}`,
			// Refused rather than ignored: a task that looks constrained and is
			// not is worse than one that says so.
			wantErr: "needs is not implemented",
		},
		{
			name: "same_as pointing at nothing",
			src: `{"goal":"g","accept":{"type":"exact","want":"42"},
			       "nodes":[{"id":"a","prompt":"p"},{"id":"b","same_as":"ghost"}]}`,
			wantErr: "same_as names",
		},
		{
			name: "same_as pointing at a verifier",
			src: `{"goal":"g","accept":{"type":"agreement","nodes":["a","b"]},
			       "nodes":[{"id":"a","prompt":"p"},{"id":"v","kind":"verify","depends_on":["a"],"check":{"type":"agreement","nodes":["a","b"]}},{"id":"b","same_as":"v"}]}`,
			wantErr: "must copy a solve node",
		},
		{
			name: "same_as rewriting half of what it copies",
			src: `{"goal":"g","accept":{"type":"exact","want":"42"},
			       "nodes":[{"id":"a","prompt":"p"},{"id":"b","same_as":"a","prompt":"something else"}]}`,
			wantErr: "inherits its prompt",
		},
		{
			name: "same_as of a same_as",
			src: `{"goal":"g","accept":{"type":"exact","want":"42"},
			       "nodes":[{"id":"a","prompt":"p"},{"id":"b","same_as":"a"},{"id":"c","same_as":"b"}]}`,
			wantErr: "itself a copy",
		},
		{
			name: "a verifier asked to judge an unambiguous thing with two candidates",
			src: `{"goal":"g","accept":{"type":"exact","want":"42"},
			       "nodes":[{"id":"a","prompt":"p"},{"id":"b","prompt":"q"},
			                {"id":"v","kind":"verify","depends_on":["a","b"],"check":{"type":"exact","want":"42"}}]}`,
			wantErr: "ambiguous",
		},
		{
			name: "a verifier judging a verifier",
			src: `{"goal":"g","accept":{"type":"exact","want":"42"},
			       "nodes":[{"id":"a","prompt":"p"},
			                {"id":"v","kind":"verify","depends_on":["a"],"check":{"type":"exact","want":"42"}},
			                {"id":"w","kind":"verify","depends_on":["v"],"check":{"type":"exact","want":"42"}}]}`,
			wantErr: "nothing to judge",
		},
		{
			name: "two possible answers and no way to tell",
			src: `{"goal":"g","accept":{"type":"exact","want":"42"},
			       "nodes":[{"id":"a","prompt":"p"},{"id":"b","prompt":"q"}]}`,
			wantErr: "ambiguous",
		},
		{
			name: "a solution that is not a node",
			src: `{"goal":"g","accept":{"type":"exact","want":"42"},"solution":"ghost",
			       "nodes":` + nodeIDs + `}`,
			wantErr: "not a node in this task",
		},
		{
			name: "a solution whose artifact cannot be judged by the accept check",
			src: `{"goal":"g","accept":{"type":"exact","want":"42"},"solution":"v",
			       "nodes":[{"id":"a","prompt":"p"},{"id":"b","prompt":"q"},
			                {"id":"v","kind":"verify","depends_on":["a","b"],"check":{"type":"agreement","nodes":["a","b"]}}]}`,
			wantErr: "ambiguous",
		},
		{
			name: "work whose result is thrown away",
			src: `{"goal":"g","accept":{"type":"exact","want":"42"},"solution":"a",
			       "nodes":[{"id":"a","prompt":"p"},{"id":"b","prompt":"q"}]}`,
			wantErr: "produces an artifact nothing uses",
		},
		{
			name:    "a task file that is not JSON",
			src:     `not json at all`,
			wantErr: "invalid character",
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			_, err := plan(t, tc.src)
			if err == nil {
				t.Fatal("want a refusal, got a task")
			}
			if !strings.Contains(err.Error(), tc.wantErr) {
				t.Errorf("refusal %q does not contain %q", err, tc.wantErr)
			}
		})
	}
}

func TestPlanResolvesTheSingleSink(t *testing.T) {
	task := mustPlan(t, `{"goal":"g","accept":{"type":"exact","want":"42"},"nodes":`+nodeIDs+`}`)
	if task.Solution != "a" {
		t.Errorf("solution = %q, want a", task.Solution)
	}
}

func TestPlanAcceptsAVerifierAsTheAnswerWhenAcceptIsAgreement(t *testing.T) {
	// The best-of-N shape: the join checks the attempts, the accept checks the
	// agreement, and no single artifact is "the answer".
	task := mustPlan(t, `{"goal":"g","model":"m",
		"accept":{"type":"agreement","nodes":["a","b-1","b-2"]},
		"nodes":[{"id":"a","prompt":"p"},
		         {"id":"b","same_as":"a","count":2},
		         {"id":"join","kind":"verify","depends_on":["a","b-1","b-2"],
		          "check":{"type":"agreement","nodes":["a","b-1","b-2"]}}]}`)
	if task.Solution != "join" {
		t.Errorf("solution = %q, want join", task.Solution)
	}
}

func TestSameAsExpansion(t *testing.T) {
	task := mustPlan(t, `{"goal":"g","accept":{"type":"agreement","nodes":["a","b-1","b-2","b-3"]},
		"nodes":[{"id":"a","prompt":"the prompt","model":"m1"},
		         {"id":"b","same_as":"a","count":3},
		         {"id":"join","kind":"verify","depends_on":["a","b-1","b-2","b-3"],
		          "check":{"type":"agreement","nodes":["a","b-1","b-2","b-3"]}}]}`)

	var got []string
	for _, n := range task.Nodes {
		got = append(got, n.ID)
	}
	want := []string{"a", "b-1", "b-2", "b-3", "join"}
	if strings.Join(got, ",") != strings.Join(want, ",") {
		t.Fatalf("nodes = %v, want %v", got, want)
	}
	byID := nodesByID(task)
	for _, id := range []string{"b-1", "b-2", "b-3"} {
		if n := byID[id]; n.Prompt != "the prompt" || n.Model != "m1" || kindOf(n) != KindSolve {
			t.Errorf("%s did not inherit the node it copies: %+v", id, n)
		}
	}
	if byID["b-3"].SameAs != "" {
		t.Error("a copy still claims to be a copy, which would confuse a second expansion")
	}
}

func TestTopoOrderPutsDependenciesFirst(t *testing.T) {
	task := mustPlan(t, `{"goal":"g","accept":{"type":"exact","want":"42"},
		"nodes":[{"id":"last","prompt":"p","depends_on":["middle"]},
		         {"id":"middle","prompt":"p","depends_on":["first"]},
		         {"id":"first","prompt":"p"}]}`)

	pos := map[string]int{}
	for i, id := range topoOrder(task) {
		pos[id] = i
	}
	if !(pos["first"] < pos["middle"] && pos["middle"] < pos["last"]) {
		t.Errorf("order = %v, want first before middle before last", topoOrder(task))
	}
}

func TestKindDefaultsToSolve(t *testing.T) {
	if kindOf(Node{ID: "a"}) != KindSolve {
		t.Error("a node with no kind should be a solver")
	}
	if kindOf(Node{ID: "a", Kind: "  "}) != KindSolve {
		t.Error("a blank kind should be a solver")
	}
}
