//go:build swarm

package swarm

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"os"
	"path/filepath"
	"strings"
	"time"
)

// Statuses a node can end in.
const (
	StatusOK      = "ok"
	StatusFailed  = "failed"
	StatusSkipped = "skipped"
)

// Options tune one run.
type Options struct {
	// Dir holds artifacts and the report. Nothing outside it is written.
	Dir string
	// WorkDir is where check commands run.
	WorkDir string
	// Model is the fallback when neither the node nor the task names one.
	Model string
	// CheckTimeout bounds one check; NodeTimeout bounds one attempt. A run with
	// no bound is a process that cannot be stopped from inside.
	CheckTimeout time.Duration
	NodeTimeout  time.Duration
	// Log gets progress, which is on stderr, since stdout carries the report.
	Log *slog.Logger
}

// Outcome is what happened to one node.
type Outcome struct {
	Node       string `json:"node"`
	Kind       string `json:"kind"`
	Status     string `json:"status"`
	Digest     string `json:"digest,omitempty"`
	Check      string `json:"check,omitempty"`
	Error      string `json:"error,omitempty"`
	DurationMS int64  `json:"duration_ms"`
}

// Report is a whole run, written to disk whether or not it succeeded.
type Report struct {
	Goal     string    `json:"goal"`
	Model    string    `json:"model,omitempty"`
	Solution string    `json:"solution,omitempty"`
	Accepted bool      `json:"accepted"`
	Digest   string    `json:"digest,omitempty"`
	Verdict  string    `json:"verdict,omitempty"`
	Nodes    []Outcome `json:"nodes"`
	Error    string    `json:"error,omitempty"`
}

// run executes a planned task and returns what happened.
//
// v0 stops at the first failed node and does not retry. Retrying is a policy
// question — how many attempts, whose budget, which host — and a policy invented
// before the loop has been shown to work is a guess with extra steps.
func run(ctx context.Context, task Task, chat *Chat, opts Options) *Report {
	byID := nodesByID(task)
	arts := &store{dir: filepath.Join(opts.Dir, "artifacts")}
	seen := Artifacts{}
	rep := &Report{Goal: task.Goal, Model: task.Model, Solution: task.Solution}

	order := topoOrder(task)
	for i, id := range order {
		n := byID[id]
		out := Outcome{Node: id, Kind: kindOf(n)}
		start := time.Now()
		digest, detail, err := runNode(ctx, n, byID, task, chat, arts, seen, opts)
		out.DurationMS = time.Since(start).Milliseconds()
		if err != nil {
			out.Status, out.Error = StatusFailed, err.Error()
			rep.Nodes = append(rep.Nodes, out)
			for _, rest := range order[i+1:] {
				rep.Nodes = append(rep.Nodes, Outcome{Node: rest, Kind: kindOf(byID[rest]), Status: StatusSkipped})
			}
			rep.Error = fmt.Sprintf("node %q failed: %v", id, err)
			if opts.Log != nil {
				opts.Log.Warn("node failed", "node", id, "err", err)
			}
			return rep
		}
		out.Status, out.Digest, out.Check = StatusOK, digest, detail
		rep.Nodes = append(rep.Nodes, out)
		if opts.Log != nil {
			opts.Log.Info("node done", "node", id, "kind", out.Kind, "digest", digest, "check", detail)
		}
	}

	// Which artifact accept judges follows the same rule a node's own check
	// uses, so a verifier can be the answer via the attempt it verified.
	subject := task.Solution
	if strings.TrimSpace(task.Accept.Type) != CheckAgreement {
		s, err := subjectFor(byID[task.Solution], byID)
		if err != nil {
			rep.Error = fmt.Sprintf("accept: %v", err)
			return rep
		}
		subject = s
		rep.Digest = seen[subject].Digest
	}
	detail, err := evalSubject(ctx, task.Accept, subject, seen, opts)
	if err != nil {
		rep.Error = fmt.Sprintf("accept: %v", err)
		if opts.Log != nil {
			opts.Log.Warn("not accepted", "err", err)
		}
		return rep
	}
	rep.Accepted, rep.Verdict = true, detail
	return rep
}

// runNode produces a node's artifact, judges it, or both. It returns the digest
// of whatever the node left behind — empty for a verifier, which produces
// nothing — and the verdict of its check.
func runNode(ctx context.Context, n Node, byID map[string]Node, task Task, chat *Chat, store *store, arts Artifacts, opts Options) (string, string, error) {
	// The subject is only meaningful for command and exact; an agreement check
	// names its own nodes.
	var subject string
	if n.Check != nil && strings.TrimSpace(n.Check.Type) != CheckAgreement {
		s, err := subjectFor(n, byID)
		if err != nil {
			return "", "", err
		}
		subject = s
	}

	if kindOf(n) == KindSolve {
		model := firstNonEmpty(n.Model, task.Model, opts.Model)
		if model == "" {
			return "", "", fmt.Errorf("node %q: no model — set it on the task, on the node, or pass -model", n.ID)
		}
		nodeCtx, cancel := context.WithTimeout(ctx, opts.NodeTimeout)
		defer cancel()
		text, err := chat.Complete(nodeCtx, model, n.Prompt)
		if err != nil {
			return "", "", err
		}
		art, err := store.put(n.ID, []byte(text))
		if err != nil {
			return "", "", err
		}
		arts[n.ID] = art
	}

	if n.Check == nil {
		return arts[n.ID].Digest, "", nil
	}
	detail, err := evalSubject(ctx, n.Check, subject, arts, opts)
	if err != nil {
		return "", "", err
	}
	return arts[n.ID].Digest, detail, nil
}

// evalSubject runs a check against the artifact it judges.
func evalSubject(ctx context.Context, c *Check, subject string, arts Artifacts, opts Options) (string, error) {
	if c == nil {
		return "", errors.New("no check")
	}
	return c.eval(ctx, subject, checkEnv{
		Artifacts: arts,
		WorkDir:   opts.WorkDir,
		Timeout:   opts.CheckTimeout,
	})
}

// subjectFor decides which artifact a check with a single subject judges.
//
// For a solver that is its own. For a verifier it is the artifact of the one
// solve node it depends on — which is how "re-run the check against what the
// attempt produced" is expressed. With several dependencies the answer is
// ambiguous, and guessing would make the verdict mean nothing in particular.
func subjectFor(n Node, byID map[string]Node) (string, error) {
	if kindOf(n) == KindSolve {
		return n.ID, nil
	}
	var solvers []string
	for _, d := range n.DependsOn {
		if kindOf(byID[d]) == KindSolve {
			solvers = append(solvers, d)
		}
	}
	switch len(solvers) {
	case 1:
		return solvers[0], nil
	case 0:
		return "", fmt.Errorf("node %q: a verify node depends on no solve node, so it has nothing to judge", n.ID)
	default:
		return "", fmt.Errorf("node %q: depends on %d solve nodes, so which artifact to judge is ambiguous — use an agreement check", n.ID, len(solvers))
	}
}

// store keeps artifacts under their own digest, the way models are identified.
// The digest is not decoration: it is what lets a report say which bytes were
// judged, and what a later run can fetch by.
type store struct{ dir string }

func (s *store) put(node string, b []byte) (Artifact, error) {
	sum := sha256.Sum256(b)
	hexSum := hex.EncodeToString(sum[:])
	if err := os.MkdirAll(s.dir, 0o755); err != nil {
		return Artifact{}, err
	}
	path := filepath.Join(s.dir, hexSum)
	// Temp file and rename, so a half-written artifact is never visible under a
	// digest that claims to be complete.
	tmp, err := os.CreateTemp(s.dir, ".partial-*")
	if err != nil {
		return Artifact{}, err
	}
	tmpName := tmp.Name()
	if _, err := tmp.Write(b); err != nil {
		tmp.Close()
		os.Remove(tmpName)
		return Artifact{}, err
	}
	if err := tmp.Close(); err != nil {
		os.Remove(tmpName)
		return Artifact{}, err
	}
	if err := os.Rename(tmpName, path); err != nil {
		os.Remove(tmpName)
		return Artifact{}, err
	}
	return Artifact{Node: node, Digest: "sha256:" + hexSum, Path: path, Bytes: b}, nil
}

// writeReport puts the report next to the artifacts, so a failed run leaves
// something to read rather than only a terminal that has scrolled.
func writeReport(dir string, rep *Report) error {
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return err
	}
	b, err := json.MarshalIndent(rep, "", "  ")
	if err != nil {
		return err
	}
	return os.WriteFile(filepath.Join(dir, "report.json"), append(b, '\n'), 0o644)
}

// firstNonEmpty returns the first argument that is not blank.
func firstNonEmpty(vals ...string) string {
	for _, v := range vals {
		if strings.TrimSpace(v) != "" {
			return strings.TrimSpace(v)
		}
	}
	return ""
}
