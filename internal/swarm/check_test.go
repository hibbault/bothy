//go:build swarm

package swarm

import (
	"context"
	"strings"
	"testing"
	"time"
)

// artifactSet writes texts to a temp directory through the real store, so the
// paths and digests a check sees are the ones a run would produce.
func artifactSet(t *testing.T, texts map[string]string) Artifacts {
	t.Helper()
	s := &store{dir: t.TempDir()}
	arts := Artifacts{}
	for node, text := range texts {
		a, err := s.put(node, []byte(text))
		if err != nil {
			t.Fatalf("store.put(%s): %v", node, err)
		}
		arts[node] = a
	}
	return arts
}

func TestExactCheck(t *testing.T) {
	arts := artifactSet(t, map[string]string{"a": "42"})

	t.Run("a match passes", func(t *testing.T) {
		detail, err := Check{Type: CheckExact, Want: "42"}.eval(context.Background(), "a", checkEnv{Artifacts: arts})
		if err != nil {
			t.Fatalf("want pass, got %v", err)
		}
		if detail != "matches want" {
			t.Errorf("detail = %q", detail)
		}
	})

	t.Run("surrounding whitespace is not a disagreement", func(t *testing.T) {
		// An engine that ends its reply with a newline has not answered
		// differently, and a check that says it has would be noise.
		if _, err := (Check{Type: CheckExact, Want: "42\n"}).eval(context.Background(), "a", checkEnv{Artifacts: arts}); err != nil {
			t.Fatalf("want pass, got %v", err)
		}
	})

	t.Run("a different answer fails, and says both", func(t *testing.T) {
		_, err := (Check{Type: CheckExact, Want: "41"}).eval(context.Background(), "a", checkEnv{Artifacts: arts})
		if err == nil {
			t.Fatal("want failure, got pass")
		}
		for _, want := range []string{`"42"`, `"41"`} {
			if !strings.Contains(err.Error(), want) {
				t.Errorf("error %q does not mention %s", err, want)
			}
		}
	})

	t.Run("a missing artifact is an error, not a pass", func(t *testing.T) {
		if _, err := (Check{Type: CheckExact, Want: "42"}).eval(context.Background(), "nope", checkEnv{Artifacts: arts}); err == nil {
			t.Fatal("want failure, got pass")
		}
	})
}

func TestCommandCheck(t *testing.T) {
	arts := artifactSet(t, map[string]string{"a": "the answer is 42"})

	run := func(t *testing.T, c Check, timeout time.Duration) (string, error) {
		t.Helper()
		return c.eval(context.Background(), "a", checkEnv{Artifacts: arts, Timeout: timeout})
	}

	t.Run("exit zero passes", func(t *testing.T) {
		detail, err := run(t, Check{Type: CheckCommand, Run: `test "$(cat "$BOTHY_ARTIFACT")" = "the answer is 42"`}, time.Second)
		if err != nil {
			t.Fatalf("want pass, got %v", err)
		}
		if detail != "command exited 0" {
			t.Errorf("detail = %q", detail)
		}
	})

	t.Run("the artifact is reachable by path, node and digest", func(t *testing.T) {
		// The whole point of the check is that the submitter can point their own
		// tooling at the result, so all three have to arrive.
		_, err := run(t, Check{Type: CheckCommand, Run: `test -s "$BOTHY_ARTIFACT" && test -n "$BOTHY_NODE" && test -n "$BOTHY_DIGEST" && test -d "$BOTHY_ARTIFACT_DIR"`}, time.Second)
		if err != nil {
			t.Fatalf("want pass, got %v", err)
		}
	})

	t.Run("a non-zero exit fails and names the code", func(t *testing.T) {
		_, err := run(t, Check{Type: CheckCommand, Run: "exit 3"}, time.Second)
		if err == nil {
			t.Fatal("want failure, got pass")
		}
		if !strings.Contains(err.Error(), "exited 3") {
			t.Errorf("error %q does not name the exit code", err)
		}
	})

	t.Run("a failure carries the output that explains it", func(t *testing.T) {
		_, err := run(t, Check{Type: CheckCommand, Run: "echo 'expected 42, got 41'; exit 1"}, time.Second)
		if err == nil {
			t.Fatal("want failure, got pass")
		}
		if !strings.Contains(err.Error(), "expected 42, got 41") {
			t.Errorf("error %q does not carry the command's output", err)
		}
	})

	t.Run("a check that never finishes does not pass", func(t *testing.T) {
		start := time.Now()
		_, err := run(t, Check{Type: CheckCommand, Run: "sleep 30"}, 100*time.Millisecond)
		if err == nil {
			t.Fatal("want failure, got pass")
		}
		if !strings.Contains(err.Error(), "timed out") {
			t.Errorf("error %q does not name the timeout", err)
		}
		if time.Since(start) > 5*time.Second {
			t.Errorf("waited %s for a 100ms timeout", time.Since(start))
		}
	})

	t.Run("an unrunnable command fails rather than passing", func(t *testing.T) {
		if _, err := run(t, Check{Type: CheckCommand, Run: ""}, time.Second); err == nil {
			t.Fatal("want failure, got pass")
		}
	})
}

func TestAgreementCheck(t *testing.T) {
	t.Run("a majority agreeing passes and names who", func(t *testing.T) {
		arts := artifactSet(t, map[string]string{"a": "42", "b": "42\n", "c": "41"})
		detail, err := Check{Type: CheckAgreement, Nodes: []string{"a", "b", "c"}}.eval(context.Background(), "", checkEnv{Artifacts: arts})
		if err != nil {
			t.Fatalf("want pass, got %v", err)
		}
		for _, want := range []string{"2 of 3 agree", "a", "b"} {
			if !strings.Contains(detail, want) {
				t.Errorf("detail %q does not mention %q", detail, want)
			}
		}
	})

	t.Run("no agreement fails, and reports the group sizes", func(t *testing.T) {
		arts := artifactSet(t, map[string]string{"a": "1", "b": "2", "c": "3"})
		_, err := (Check{Type: CheckAgreement, Nodes: []string{"a", "b", "c"}}).eval(context.Background(), "", checkEnv{Artifacts: arts})
		if err == nil {
			t.Fatal("want failure, got pass")
		}
		if !strings.Contains(err.Error(), "2 were needed") {
			t.Errorf("error %q does not say how many were needed", err)
		}
	})

	t.Run("a demanded count can be stricter than a majority", func(t *testing.T) {
		arts := artifactSet(t, map[string]string{"a": "42", "b": "42", "c": "41"})
		if _, err := (Check{Type: CheckAgreement, Nodes: []string{"a", "b", "c"}, Count: 3}).eval(context.Background(), "", checkEnv{Artifacts: arts}); err == nil {
			t.Fatal("want failure when all three are required, got pass")
		}
	})

	t.Run("fewer than the count agreeing is a failure, not a smaller pass", func(t *testing.T) {
		arts := artifactSet(t, map[string]string{"a": "42", "b": "41", "c": "40"})
		if _, err := (Check{Type: CheckAgreement, Nodes: []string{"a", "b", "c"}, Count: 2}).eval(context.Background(), "", checkEnv{Artifacts: arts}); err == nil {
			t.Fatal("want failure, got pass")
		}
	})

	t.Run("a node that produced nothing is an error", func(t *testing.T) {
		arts := artifactSet(t, map[string]string{"a": "42"})
		if _, err := (Check{Type: CheckAgreement, Nodes: []string{"a", "b"}}).eval(context.Background(), "", checkEnv{Artifacts: arts}); err == nil {
			t.Fatal("want failure, got pass")
		}
	})
}

// TestCheckRefusals pins the plan-time rules: a check that cannot mean what it
// says is rejected before anything runs, rather than at the point it matters.
func TestCheckRefusals(t *testing.T) {
	byID := map[string]Node{
		"a":     {ID: "a", Prompt: "p"},
		"b":     {ID: "b", Prompt: "p"},
		"third": {ID: "third", Prompt: "p"},
		"v":     {ID: "v", Kind: KindVerify, DependsOn: []string{"a"}},
	}

	cases := []struct {
		name    string
		check   Check
		self    string
		wantErr string
	}{
		{"unknown type", Check{Type: "vibes"}, "a", "unknown check type"},
		{"no type", Check{}, "a", "unknown check type"},
		{"command without a command", Check{Type: CheckCommand}, "a", "needs run"},
		{"command with an exact's field", Check{Type: CheckCommand, Run: "true", Want: "42"}, "a", "takes run and nothing else"},
		{"exact without want", Check{Type: CheckExact}, "a", "needs want"},
		{"exact with a command's field", Check{Type: CheckExact, Want: "42", Run: "true"}, "a", "takes want and nothing else"},
		{"agreement with one node", Check{Type: CheckAgreement, Nodes: []string{"a"}}, "v", "at least two nodes"},
		{"agreement naming a stranger", Check{Type: CheckAgreement, Nodes: []string{"a", "ghost"}}, "v", "not a node in this task"},
		{"agreement naming the node it is on", Check{Type: CheckAgreement, Nodes: []string{"a", "a"}}, "a", "the node the check is on"},
		{"agreement naming a node twice", Check{Type: CheckAgreement, Nodes: []string{"a", "b", "a"}}, "v", "twice"},
		{"agreement naming a verifier", Check{Type: CheckAgreement, Nodes: []string{"a", "v"}}, "third", "produces no artifact"},
		{"agreement with an impossible count", Check{Type: CheckAgreement, Nodes: []string{"a", "b"}, Count: 3}, "v", "cannot be reached"},
		{"agreement demanding one", Check{Type: CheckAgreement, Nodes: []string{"a", "b"}, Count: 1}, "v", "cannot be reached"},
		{"agreement with a command's field", Check{Type: CheckAgreement, Nodes: []string{"a", "b"}, Run: "true"}, "v", "takes nodes and count"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			err := tc.check.validate("check", tc.self, byID)
			if err == nil {
				t.Fatalf("want refusal, got none")
			}
			if !strings.Contains(err.Error(), tc.wantErr) {
				t.Errorf("error %q does not contain %q", err, tc.wantErr)
			}
		})
	}

	t.Run("a workable check is accepted", func(t *testing.T) {
		for _, c := range []Check{
			{Type: CheckCommand, Run: "true"},
			{Type: CheckExact, Want: "42"},
			{Type: CheckAgreement, Nodes: []string{"a", "b"}},
			{Type: CheckAgreement, Nodes: []string{"a", "b"}, Count: 2},
		} {
			if err := c.validate("check", "v", byID); err != nil {
				t.Errorf("%+v refused: %v", c, err)
			}
		}
	})
}
