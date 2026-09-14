//go:build swarm

package swarm

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"strings"
)

// Node kinds. A solver produces an artifact; a verifier judges one and may not
// produce. Keeping those apart is the reason a fleet of strangers is usable at
// all: production is anonymous and checkable, judgment is neither.
const (
	KindSolve  = "solve"
	KindVerify = "verify"
)

// Task is a problem: a goal, an acceptance criterion, and the nodes that get
// there.
//
// It is deliberately hard to write a bad one. Accept is not optional, unknown
// fields are refused rather than ignored, and anything a node declares that the
// runner cannot honour is refused rather than silently dropped.
type Task struct {
	// Goal is what the task is for. Human-facing; nothing checks it.
	Goal string `json:"goal"`
	// Model is the default model for every solve node.
	Model string `json:"model,omitempty"`
	// Solution names the node whose artifact is the answer. Optional when the
	// graph has exactly one sink; required when it has more.
	Solution string `json:"solution,omitempty"`
	// Accept is the acceptance criterion, and it is mandatory. If the submitter
	// cannot say what "solved" means, no amount of fleet will find it.
	Accept *Check `json:"accept,omitempty"`
	// Nodes are the units of work in no particular order: execution order comes
	// from depends_on, never from this list.
	Nodes []Node `json:"nodes,omitempty"`
}

// Node is one unit of work.
type Node struct {
	ID string `json:"id"`
	// Kind is solve (the default) or verify.
	Kind   string `json:"kind,omitempty"`
	Prompt string `json:"prompt,omitempty"`
	Model  string `json:"model,omitempty"`
	// Needs declares what this node requires of whichever machine serves it.
	// Parsed, validated, and refused in v0 — see Needs.
	Needs *Needs `json:"needs,omitempty"`
	// SameAs copies another solve node, so N independent attempts of one prompt
	// is a two-line task rather than N pasted prompts. Count is how many copies.
	SameAs string `json:"same_as,omitempty"`
	Count  int    `json:"count,omitempty"`
	// DependsOn are the node IDs that must succeed before this one runs.
	DependsOn []string `json:"depends_on,omitempty"`
	// Check judges this node's artifact, or the artifacts it depends on for an
	// agreement check. Optional: an attempt with no check is an attempt whose
	// only verdict is whatever its dependents reach.
	Check *Check `json:"check,omitempty"`
}

// Needs declares a node's requirements on the machine that serves it: a model
// name, and the digest of its weights.
//
// v0 refuses a node that sets this rather than ignoring it. A requirement that
// is parsed and then quietly dropped is worse than one that is rejected, because
// the task goes on looking like it constrained something it did not. Matching a
// node to a host that can serve it is stage v2 in docs/swarm.md.
type Needs struct {
	Model  string `json:"model,omitempty"`
	Digest string `json:"digest,omitempty"`
}

// Check judges an artifact. Cheap and trustworthy are both required; the rule
// that makes it so is at the top of docs/swarm.md.
type Check struct {
	// Type is command, exact or agreement.
	Type string `json:"type"`
	// Run is the shell command for a command check.
	Run string `json:"run,omitempty"`
	// Want is the expected text for an exact check.
	Want string `json:"want,omitempty"`
	// Nodes are the artifacts to compare for an agreement check.
	Nodes []string `json:"nodes,omitempty"`
	// Count is how many of Nodes must agree. Default is a majority.
	Count int `json:"count,omitempty"`
}

// LoadTask reads, expands and validates a task file, returning what will
// actually run.
func LoadTask(path string) (Task, error) {
	b, err := os.ReadFile(path)
	if err != nil {
		return Task{}, err
	}
	var t Task
	dec := json.NewDecoder(bytes.NewReader(b))
	// Unknown fields are refused. A typo in "accept" or "depends_on" would
	// otherwise silently drop the thing meant to constrain the run, and the task
	// would go on looking stricter than it is.
	dec.DisallowUnknownFields()
	if err := dec.Decode(&t); err != nil {
		return Task{}, fmt.Errorf("%s: %w", path, err)
	}
	var trailing any
	if err := dec.Decode(&trailing); !errors.Is(err, io.EOF) {
		return Task{}, fmt.Errorf("%s: unexpected content after the task object", path)
	}
	return t.plan()
}

// plan expands, validates and resolves a task into the graph that will run. It
// is the only way to get a Task worth executing.
func (t Task) plan() (Task, error) {
	expanded, err := t.expand()
	if err != nil {
		return Task{}, err
	}
	if err := expanded.validate(); err != nil {
		return Task{}, err
	}
	return expanded.resolveSolution()
}

// expand turns same_as nodes into their copies. It runs before validation so
// that everything downstream — id collisions, dependencies, checks naming
// nodes — is checked against the graph that actually runs, not the one that was
// written down.
func (t Task) expand() (Task, error) {
	declared := make(map[string]Node, len(t.Nodes))
	for _, n := range t.Nodes {
		if strings.TrimSpace(n.ID) == "" {
			return Task{}, errors.New("a node has no id")
		}
		if _, dup := declared[n.ID]; dup {
			return Task{}, fmt.Errorf("duplicate node id %q", n.ID)
		}
		declared[n.ID] = n
	}

	out := Task{Goal: t.Goal, Model: t.Model, Solution: t.Solution, Accept: t.Accept}
	for _, n := range t.Nodes {
		if n.SameAs == "" {
			out.Nodes = append(out.Nodes, n)
			continue
		}
		src, ok := declared[n.SameAs]
		if !ok {
			return Task{}, fmt.Errorf("node %q: same_as names %q, which is not a node in this task", n.ID, n.SameAs)
		}
		if src.SameAs != "" {
			return Task{}, fmt.Errorf("node %q: same_as cannot copy %q, which is itself a copy", n.ID, n.SameAs)
		}
		if kindOf(src) != KindSolve {
			return Task{}, fmt.Errorf("node %q: same_as must copy a solve node, and %q is %s", n.ID, src.ID, kindOf(src))
		}
		// Overriding part of what is being copied makes "the same attempt" mean
		// two things at once. depends_on is allowed through: independence of the
		// attempts is the point, but ordering a copy after something is a
		// separate decision.
		if n.Prompt != "" || n.Model != "" || n.Check != nil || n.Needs != nil {
			return Task{}, fmt.Errorf("node %q: a same_as node inherits its prompt, model, check and needs from %q; delete them, or drop same_as and declare the node in full", n.ID, n.SameAs)
		}
		count := n.Count
		if count <= 0 {
			count = 1
		}
		for i := 1; i <= count; i++ {
			c := src
			c.ID = n.ID
			c.SameAs = ""
			c.Count = 0
			c.DependsOn = n.DependsOn
			if count > 1 {
				c.ID = fmt.Sprintf("%s-%d", n.ID, i)
			}
			out.Nodes = append(out.Nodes, c)
		}
	}
	return out, nil
}

// validate checks everything the runner is about to rely on, and refuses rather
// than repairs.
func (t Task) validate() error {
	if strings.TrimSpace(t.Goal) == "" {
		return errors.New("a task needs a goal")
	}
	if t.Accept == nil || strings.TrimSpace(t.Accept.Type) == "" {
		return errors.New(`a task needs an accept criterion ("accept": {"type": ...}): without one there is nothing to check the work against, and work that cannot be checked cannot be farmed`)
	}
	if len(t.Nodes) == 0 {
		return errors.New("a task needs at least one node")
	}

	byID := make(map[string]Node, len(t.Nodes))
	for _, n := range t.Nodes {
		if _, dup := byID[n.ID]; dup {
			return fmt.Errorf("duplicate node id %q — same_as copies must not collide with declared ids", n.ID)
		}
		byID[n.ID] = n
	}

	for _, n := range t.Nodes {
		switch kind := kindOf(n); kind {
		case KindSolve:
			if strings.TrimSpace(n.Prompt) == "" {
				return fmt.Errorf("node %q: a solve node needs a prompt", n.ID)
			}
		case KindVerify:
			if strings.TrimSpace(n.Prompt) != "" {
				return fmt.Errorf("node %q: a verify node cannot have a prompt — a checker that produces is a producer, and the entire point of the split is that it is not one", n.ID)
			}
			if len(n.DependsOn) == 0 {
				return fmt.Errorf("node %q: a verify node needs depends_on, so that it has something to judge", n.ID)
			}
		default:
			return fmt.Errorf("node %q: unknown kind %q (want %s or %s)", n.ID, kind, KindSolve, KindVerify)
		}
		if n.Needs != nil {
			return fmt.Errorf("node %q: needs is not implemented (docs/swarm.md, stage v2) — delete it rather than have the runner silently ignore a requirement you wrote down", n.ID)
		}
		seen := make(map[string]bool, len(n.DependsOn))
		for _, d := range n.DependsOn {
			switch {
			case d == n.ID:
				return fmt.Errorf("node %q: depends_on names itself", n.ID)
			case seen[d]:
				return fmt.Errorf("node %q: depends_on names %q twice", n.ID, d)
			}
			if _, ok := byID[d]; !ok {
				return fmt.Errorf("node %q: depends_on names %q, which is not a node in this task", n.ID, d)
			}
			seen[d] = true
		}
	}

	if err := t.checkAcyclic(byID); err != nil {
		return err
	}

	for _, n := range t.Nodes {
		if n.Check == nil {
			if kindOf(n) == KindVerify {
				return fmt.Errorf("node %q: a verify node with no check does nothing", n.ID)
			}
			continue
		}
		if err := n.Check.validate(fmt.Sprintf("node %q", n.ID), n.ID, byID); err != nil {
			return err
		}
		// A check that judges one artifact has to be able to say which. For a
		// verifier that is a decision, not a detail, so it is made here rather
		// than at run time: -plan should catch it.
		if strings.TrimSpace(n.Check.Type) != CheckAgreement {
			if _, err := subjectFor(n, byID); err != nil {
				return err
			}
		}
	}
	return t.Accept.validate("accept", "", byID)
}

// checkAcyclic refuses a graph with a loop. Without this the runner would either
// deadlock or run a node before the thing it depends on; both are worse than an
// error naming the cycle.
func (t Task) checkAcyclic(byID map[string]Node) error {
	const (
		unvisited = iota
		onStack
		done
	)
	state := make(map[string]int, len(t.Nodes))
	var visit func(id string, path []string) error
	visit = func(id string, path []string) error {
		switch state[id] {
		case onStack:
			return fmt.Errorf("the graph has a cycle: %s", strings.Join(append(path, id), " → "))
		case done:
			return nil
		}
		state[id] = onStack
		for _, d := range byID[id].DependsOn {
			if err := visit(d, append(path, id)); err != nil {
				return err
			}
		}
		state[id] = done
		return nil
	}
	for _, n := range t.Nodes {
		if err := visit(n.ID, nil); err != nil {
			return err
		}
	}
	return nil
}

// resolveSolution decides which artifact the accept criterion judges.
//
// It refuses to guess, and it refuses to run work whose result is thrown away: a
// node nothing depends on and which is not the answer has produced an artifact
// somebody asked for and nobody will read.
func (t Task) resolveSolution() (Task, error) {
	byID := make(map[string]Node, len(t.Nodes))
	dependents := make(map[string]int, len(t.Nodes))
	for _, n := range t.Nodes {
		byID[n.ID] = n
		for _, d := range n.DependsOn {
			dependents[d]++
		}
	}
	var sinks []string
	for _, n := range t.Nodes {
		if dependents[n.ID] == 0 {
			sinks = append(sinks, n.ID)
		}
	}

	// An agreement accept compares the artifacts it names and needs no subject of
	// its own, so several unanswered nodes is normal — as with three attempts and
	// no join. A solution is then only a label, and may be left out.
	if strings.TrimSpace(t.Accept.Type) == CheckAgreement {
		// One sink is still worth naming for the report; several is not a
		// problem, so it is left open rather than refused.
		if t.Solution == "" && len(sinks) == 1 {
			t.Solution = sinks[0]
		}
		if t.Solution != "" {
			if _, ok := byID[t.Solution]; !ok {
				return Task{}, fmt.Errorf("solution names %q, which is not a node in this task", t.Solution)
			}
		}
		return t, nil
	}

	if t.Solution == "" {
		switch len(sinks) {
		case 1:
			t.Solution = sinks[0]
		case 0:
			return Task{}, errors.New("every node depends on another, so there is no answer to accept")
		default:
			return Task{}, fmt.Errorf("the graph has %d sinks (%s), so which one is the answer is ambiguous; name it with \"solution\"", len(sinks), strings.Join(sinks, ", "))
		}
	}

	sol, ok := byID[t.Solution]
	if !ok {
		return Task{}, fmt.Errorf("solution names %q, which is not a node in this task", t.Solution)
	}
	// Command and exact judge one artifact, and which one follows the same rule
	// as a node's own check — so a verifier can be the answer, judged through the
	// attempt it verified.
	if _, err := subjectFor(sol, byID); err != nil {
		return Task{}, fmt.Errorf("solution: %w", err)
	}
	for _, sink := range sinks {
		if sink != t.Solution {
			return Task{}, fmt.Errorf("node %q produces an artifact nothing uses; make %q depend on it, or delete it", sink, t.Solution)
		}
	}
	return t, nil
}

// kindOf returns a node's kind, defaulting to solve.
func kindOf(n Node) string {
	if k := strings.TrimSpace(n.Kind); k != "" {
		return k
	}
	return KindSolve
}

// nodesByID indexes a planned task's nodes.
func nodesByID(t Task) map[string]Node {
	byID := make(map[string]Node, len(t.Nodes))
	for _, n := range t.Nodes {
		byID[n.ID] = n
	}
	return byID
}

// topoOrder returns node IDs such that every node follows everything it depends
// on. Ties keep the declared order, so the same task file always runs the same
// way.
func topoOrder(t Task) []string {
	remaining := make(map[string]int, len(t.Nodes))
	order := make([]string, 0, len(t.Nodes))
	for _, n := range t.Nodes {
		remaining[n.ID] = len(n.DependsOn)
	}
	emitted := make(map[string]bool, len(t.Nodes))
	for len(order) < len(t.Nodes) {
		progressed := false
		for _, n := range t.Nodes {
			if emitted[n.ID] || remaining[n.ID] != 0 {
				continue
			}
			emitted[n.ID] = true
			order = append(order, n.ID)
			progressed = true
			for _, m := range t.Nodes {
				for _, d := range m.DependsOn {
					if d == n.ID {
						remaining[m.ID]--
					}
				}
			}
		}
		if !progressed {
			// Unreachable once checkAcyclic has passed; returning what we have
			// beats looping forever if that ever stops being true.
			break
		}
	}
	return order
}
