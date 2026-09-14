//go:build swarm

package swarm

import (
	"context"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"sort"
	"strings"
	"time"
)

// Check types. All three answer "did this work" without asking a model, which is
// the only kind of answer worth having here.
const (
	CheckCommand   = "command"
	CheckExact     = "exact"
	CheckAgreement = "agreement"
)

const checkTypes = "command, exact, agreement"

// outputTail is how much of a failed command's output is kept for the report.
// Enough to see the failure, not enough to fill a terminal.
const outputTail = 2000

// validate refuses a check it cannot run, or cannot run as written.
func (c Check) validate(where, self string, byID map[string]Node) error {
	switch strings.TrimSpace(c.Type) {
	case CheckCommand:
		if strings.TrimSpace(c.Run) == "" {
			return fmt.Errorf("%s: a command check needs run", where)
		}
		if c.Want != "" || len(c.Nodes) > 0 || c.Count != 0 {
			return fmt.Errorf("%s: a command check takes run and nothing else; want belongs to exact, nodes and count to agreement", where)
		}
	case CheckExact:
		if c.Want == "" {
			return fmt.Errorf("%s: an exact check needs want — an empty want would match everything", where)
		}
		if c.Run != "" || len(c.Nodes) > 0 || c.Count != 0 {
			return fmt.Errorf("%s: an exact check takes want and nothing else; run belongs to command, nodes and count to agreement", where)
		}
	case CheckAgreement:
		if c.Run != "" || c.Want != "" {
			return fmt.Errorf("%s: an agreement check takes nodes and count and nothing else; run belongs to command, want to exact", where)
		}
		if len(c.Nodes) < 2 {
			return fmt.Errorf("%s: an agreement check needs at least two nodes to compare and got %d", where, len(c.Nodes))
		}
		seen := make(map[string]bool, len(c.Nodes))
		for _, id := range c.Nodes {
			if id == self {
				return fmt.Errorf("%s: agreement names %q, which is the node the check is on", where, id)
			}
			n, ok := byID[id]
			if !ok {
				return fmt.Errorf("%s: agreement names %q, which is not a node in this task", where, id)
			}
			if kindOf(n) != KindSolve {
				return fmt.Errorf("%s: agreement names %q, which is a %s node and produces no artifact to compare", where, id, kindOf(n))
			}
			if seen[id] {
				return fmt.Errorf("%s: agreement names %q twice", where, id)
			}
			seen[id] = true
		}
		if c.Count != 0 && (c.Count < 2 || c.Count > len(c.Nodes)) {
			return fmt.Errorf("%s: count %d cannot be reached by %d nodes (leave it out for a majority)", where, c.Count, len(c.Nodes))
		}
	default:
		return fmt.Errorf("%s: unknown check type %q (want %s)", where, strings.TrimSpace(c.Type), checkTypes)
	}
	return nil
}

// Artifact is one node's output.
//
// Text, for now: v0 farms answers, not patches. Code artifacts are what stage v1
// needs, and the content-addressing is already the shape they need.
type Artifact struct {
	Node   string
	Digest string
	Path   string
	Bytes  []byte
}

// Artifacts is everything produced so far, by node id.
type Artifacts map[string]Artifact

// checkEnv is what a check needs beyond the artifact it judges.
type checkEnv struct {
	Artifacts Artifacts
	WorkDir   string
	Timeout   time.Duration
}

// eval runs the check against subject — the node whose artifact is being judged
// — and returns why it passed. A non-nil error means it did not, and says why.
func (c Check) eval(ctx context.Context, subject string, env checkEnv) (string, error) {
	switch strings.TrimSpace(c.Type) {
	case CheckCommand:
		return c.evalCommand(ctx, subject, env)
	case CheckExact:
		return c.evalExact(subject, env)
	case CheckAgreement:
		return c.evalAgreement(env)
	default:
		return "", fmt.Errorf("unknown check type %q", c.Type)
	}
}

// evalCommand runs the submitter's command with the artifact's path in the
// environment, and takes its exit code as the verdict. This is the check the
// whole design leans on: it is the one oracle that is cheap, reproducible, and
// already lying around in the repository being worked on.
func (c Check) evalCommand(ctx context.Context, subject string, env checkEnv) (string, error) {
	// Refused here as well as at plan time: a check that runs nothing exits 0,
	// and a check that passes by doing nothing is the failure mode this whole
	// design exists to avoid.
	if strings.TrimSpace(c.Run) == "" {
		return "", errors.New("the check has no command to run")
	}
	a, ok := env.Artifacts[subject]
	if !ok {
		return "", fmt.Errorf("no artifact to check for %q", subject)
	}
	timeout := env.Timeout
	if timeout <= 0 {
		timeout = time.Minute
	}
	ctx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()

	name, args := shell(c.Run)
	cmd := exec.CommandContext(ctx, name, args...)
	// Cancel is overridden so that a timeout takes the check's children too; see
	// kill_unix.go for why the default is not enough.
	cmd.Cancel = func() error { return killCommand(cmd) }
	prepareCommand(cmd)
	cmd.Dir = env.WorkDir
	cmd.Env = append(os.Environ(),
		"BOTHY_ARTIFACT="+a.Path,
		"BOTHY_ARTIFACT_DIR="+filepath.Dir(a.Path),
		"BOTHY_NODE="+subject,
		"BOTHY_DIGEST="+a.Digest,
	)
	out, err := cmd.CombinedOutput()
	if err == nil {
		return "command exited 0", nil
	}
	if errors.Is(ctx.Err(), context.DeadlineExceeded) {
		// Deliberately not killed-as-failed-only: a check that cannot finish in
		// time has not passed, and saying so is the honest answer.
		return "", fmt.Errorf("command timed out after %s\n%s", timeout, tail(out))
	}
	var exit *exec.ExitError
	if errors.As(err, &exit) {
		return "", fmt.Errorf("command exited %d\n%s", exit.ExitCode(), tail(out))
	}
	return "", fmt.Errorf("could not run the command: %w", err)
}

// evalExact compares the artifact with the wanted text.
func (c Check) evalExact(subject string, env checkEnv) (string, error) {
	a, ok := env.Artifacts[subject]
	if !ok {
		return "", fmt.Errorf("no artifact to check for %q", subject)
	}
	got, want := normalize(a.Bytes), normalize([]byte(c.Want))
	if got == want {
		return "matches want", nil
	}
	return "", fmt.Errorf("artifact is %q, want %q", shorten(got), shorten(want))
}

// evalAgreement passes when enough of the named artifacts are identical.
//
// It is a check on determinism, not on quality: two answers agreeing says they
// agree, and nothing more. That makes it fit for a computed value, an identifier
// or a classification, and unfit for prose — which is why it is a count and not
// a vote, and why the count has to be met by actual agreement rather than by
// identities. Majority-of-N across free identities is worth nothing.
func (c Check) evalAgreement(env checkEnv) (string, error) {
	groups := make(map[string][]string, len(c.Nodes))
	for _, id := range c.Nodes {
		a, ok := env.Artifacts[id]
		if !ok {
			return "", fmt.Errorf("agreement names %q, which produced no artifact", id)
		}
		key := normalize(a.Bytes)
		groups[key] = append(groups[key], id)
	}
	need := c.Count
	if need == 0 {
		need = len(c.Nodes)/2 + 1
	}
	sizes := make([]int, 0, len(groups))
	for _, ids := range groups {
		if len(ids) >= need {
			sort.Strings(ids)
			return fmt.Sprintf("%d of %d agree (%s)", len(ids), len(c.Nodes), strings.Join(ids, ", ")), nil
		}
		sizes = append(sizes, len(ids))
	}
	sort.Sort(sort.Reverse(sort.IntSlice(sizes)))
	parts := make([]string, 0, len(sizes))
	for _, n := range sizes {
		parts = append(parts, fmt.Sprintf("%d", n))
	}
	return "", fmt.Errorf("only %s of %d nodes agree in groups; %d were needed", strings.Join(parts, "+"), len(c.Nodes), need)
}

// shell wraps a check command so it runs the way the submitter would have typed
// it. A check is a shell command on purpose: one line to write, no opinion from
// Bothy about what a task runner should look like.
func shell(run string) (string, []string) {
	if runtime.GOOS == "windows" {
		return "cmd", []string{"/c", run}
	}
	return "sh", []string{"-c", run}
}

// normalize is the comparison used by exact and agreement: line endings
// ironed out and surrounding whitespace dropped, so that a trailing newline is
// not a disagreement. Nothing else is touched — case, punctuation and wording
// all still count.
func normalize(b []byte) string {
	return strings.TrimSpace(strings.ReplaceAll(string(b), "\r\n", "\n"))
}

// tail returns the last outputTail bytes of a command's output.
func tail(out []byte) string {
	s := strings.TrimSpace(string(out))
	if len(s) > outputTail {
		s = "…" + s[len(s)-outputTail:]
	}
	return s
}

// shorten truncates a string for a one-line message.
func shorten(s string) string {
	const max = 120
	s = strings.ReplaceAll(s, "\n", "\\n")
	if len(s) > max {
		return s[:max] + "…"
	}
	return s
}
