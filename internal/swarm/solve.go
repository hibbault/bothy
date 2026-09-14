//go:build swarm

package swarm

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"log/slog"
	"os"
	"strings"
	"time"

	"github.com/hibbault/bothy/internal/config"
)

// Run parses flags for the "solve" command and runs a task.
//
// It takes (ctx, log, args), the same as every other Bothy command, so
// registering it in cmd/bothy is one line behind a build tag.
func Run(ctx context.Context, log *slog.Logger, args []string) error {
	fs := flag.NewFlagSet("solve", flag.ExitOnError)
	taskPath := fs.String("task", "", "task file to run (required)")
	engineURL := fs.String("engine-url", config.Str("BOTHY_ENGINE_URL", "http://127.0.0.1:11434"), "OpenAI-compatible endpoint to borrow inference from")
	shareKey := fs.String("share-key", config.Str("BOTHY_SHARE_KEY", ""), "key to present to a host, sent as X-Bothy-Key")
	model := fs.String("model", config.Str("BOTHY_MODEL", ""), "model to use when the task does not name one")
	dir := fs.String("dir", config.Str("BOTHY_SWARM_DIR", ".bothy"), "where artifacts and the report are written")
	planOnly := fs.Bool("plan", false, "validate the task and print what would run, without running it")
	asJSON := fs.Bool("json", false, "print the report as JSON")
	checkTimeout := fs.Duration("check-timeout", config.Dur("BOTHY_CHECK_TIMEOUT", 5*time.Minute), "how long one check may take")
	nodeTimeout := fs.Duration("node-timeout", config.Dur("BOTHY_NODE_TIMEOUT", 5*time.Minute), "how long one attempt may take")
	fs.Usage = func() {
		fmt.Fprint(os.Stderr, `bothy solve — run a task: fan it out into attempts, check every one, stop at the integrator.

An accept criterion is mandatory. Work that cannot be checked cannot be farmed,
and a task without one is refused rather than run.

Experimental: this is a build-tagged plugin (make swarm) and no release binary
contains it. See docs/swarm.md.

Usage:
  bothy solve -task task.json [-engine-url http://127.0.0.1:11434] [-model llama3.1:8b]

Flags:
`)
		fs.PrintDefaults()
	}
	if err := fs.Parse(args); err != nil {
		return err
	}
	if strings.TrimSpace(*taskPath) == "" {
		return errors.New("solve needs -task: a task file with a goal, an accept criterion and nodes")
	}

	task, err := LoadTask(*taskPath)
	if err != nil {
		return err
	}
	if *planOnly {
		printPlan(os.Stdout, task, *model)
		return nil
	}

	opts := Options{
		Dir:          *dir,
		WorkDir:      ".",
		Model:        *model,
		CheckTimeout: *checkTimeout,
		NodeTimeout:  *nodeTimeout,
		Log:          log,
	}
	rep := run(ctx, task, &Chat{BaseURL: *engineURL, Key: *shareKey}, opts)

	// The report is written whether or not the run succeeded: a failed run is
	// exactly the one worth being able to read afterwards.
	if err := writeReport(*dir, rep); err != nil {
		log.Warn("could not write the report", "err", err)
	}
	if *asJSON {
		enc := json.NewEncoder(os.Stdout)
		enc.SetIndent("", "  ")
		_ = enc.Encode(rep)
	} else {
		printReport(os.Stdout, rep)
	}
	if !rep.Accepted {
		return errors.New(firstNonEmpty(rep.Error, "the run was not accepted"))
	}
	return nil
}

// printPlan shows what would run. Validating a task and stopping is worth a flag
// of its own: a plan is cheap to read and an attempt is not.
func printPlan(w io.Writer, task Task, fallbackModel string) {
	fmt.Fprintf(w, "goal:     %s\n", task.Goal)
	if m := firstNonEmpty(task.Model, fallbackModel); m != "" {
		fmt.Fprintf(w, "model:    %s\n", m)
	} else {
		fmt.Fprintf(w, "model:    (none — set it on the task, on a node, or with -model)\n")
	}
	fmt.Fprintf(w, "accept:   %s\n", describeCheck(task.Accept))
	if task.Solution == "" {
		fmt.Fprintf(w, "solution: (none — an agreement accept compares the nodes it names)\n")
	} else {
		fmt.Fprintf(w, "solution: %s\n", task.Solution)
	}

	byID := nodesByID(task)
	order := topoOrder(task)
	fmt.Fprintf(w, "\n%d nodes, in the order they will run:\n", len(order))
	for i, id := range order {
		n := byID[id]
		line := fmt.Sprintf("  %2d. %-20s %-7s", i+1, id, kindOf(n))
		if n.Prompt != "" {
			line += " " + shorten(n.Prompt)
		}
		if len(n.DependsOn) > 0 {
			line += " (after " + strings.Join(n.DependsOn, ", ") + ")"
		}
		fmt.Fprintln(w, line)
		if n.Check != nil {
			fmt.Fprintf(w, "      check: %s\n", describeCheck(n.Check))
		}
	}
	fmt.Fprintf(w, "\nnothing has run. Drop -plan to run it.\n")
}

// printReport renders a run for a person.
func printReport(w io.Writer, rep *Report) {
	for _, o := range rep.Nodes {
		line := fmt.Sprintf("%-7s %-20s %-7s %7dms", o.Status, o.Node, o.Kind, o.DurationMS)
		switch {
		case o.Error != "":
			head, rest := o.Error, ""
			if i := strings.IndexByte(o.Error, '\n'); i >= 0 {
				head, rest = o.Error[:i], o.Error[i+1:]
			}
			fmt.Fprintf(w, "%s  %s\n", line, head)
			for _, l := range strings.Split(strings.TrimRight(rest, "\n"), "\n") {
				if strings.TrimSpace(l) != "" {
					fmt.Fprintf(w, "        %s\n", l)
				}
			}
		case o.Digest == "":
			fmt.Fprintf(w, "%s  %s\n", line, o.Check)
		default:
			fmt.Fprintf(w, "%s  %s  %s\n", line, shortDigest(o.Digest), o.Check)
		}
	}
	fmt.Fprintln(w)
	if rep.Accepted {
		// The digest can be empty: an agreement accept judges several artifacts
		// and the answer is the verdict, not one file.
		if rep.Digest == "" {
			fmt.Fprintf(w, "accepted\n  %s\n", rep.Verdict)
			return
		}
		fmt.Fprintf(w, "accepted %s  (%s)\n  %s\n", shortDigest(rep.Digest), rep.Solution, rep.Verdict)
		return
	}
	fmt.Fprintf(w, "not accepted: %s\n", rep.Error)
}

// describeCheck renders a check as the one line it usually is.
func describeCheck(c *Check) string {
	if c == nil {
		return "(none)"
	}
	switch strings.TrimSpace(c.Type) {
	case CheckCommand:
		return "command: " + c.Run
	case CheckExact:
		return "exact: " + shorten(c.Want)
	case CheckAgreement:
		need := c.Count
		if need == 0 {
			need = len(c.Nodes)/2 + 1
		}
		return fmt.Sprintf("agreement: %d of %s", need, strings.Join(c.Nodes, ", "))
	}
	return c.Type
}

// shortDigest trims a digest to something that fits on a line and is still long
// enough to compare two of them by eye.
func shortDigest(d string) string {
	hex := strings.TrimPrefix(d, "sha256:")
	if len(hex) > 12 {
		hex = hex[:12]
	}
	return "sha256:" + hex + "…"
}
