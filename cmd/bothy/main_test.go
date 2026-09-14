package main

import (
	"bytes"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
)

// bothyBin is the CLI built once for the package. A command line is only really
// testable as a process: the exit code and which stream a message lands on are
// part of the contract, and neither is reachable by calling a function.
var bothyBin string

func TestMain(m *testing.M) {
	dir, err := os.MkdirTemp("", "bothy-cli-")
	if err != nil {
		panic(err)
	}
	bothyBin = filepath.Join(dir, "bothy")
	if runtime.GOOS == "windows" {
		bothyBin += ".exe"
	}

	build := exec.Command(goTool(), "build", "-o", bothyBin, ".")
	build.Stderr = os.Stderr
	if err := build.Run(); err != nil {
		os.RemoveAll(dir)
		panic("cannot build the CLI for its own tests: " + err.Error())
	}

	code := m.Run()
	os.RemoveAll(dir)
	os.Exit(code)
}

func goTool() string {
	if goroot := runtime.GOROOT(); goroot != "" {
		candidate := filepath.Join(goroot, "bin", "go")
		if runtime.GOOS == "windows" {
			candidate += ".exe"
		}
		if _, err := os.Stat(candidate); err == nil {
			return candidate
		}
	}
	return "go"
}

// runBothy runs the CLI and reports what a shell would see. A command expected to
// serve forever must not be run through this.
func runBothy(t *testing.T, args ...string) (stdout, stderr string, code int) {
	t.Helper()
	cmd := exec.Command(bothyBin, args...)
	var out, errBuf bytes.Buffer
	cmd.Stdout, cmd.Stderr = &out, &errBuf
	err := cmd.Run()

	code = 0
	if err != nil {
		exit, ok := err.(*exec.ExitError)
		if !ok {
			t.Fatalf("running bothy %s: %v", strings.Join(args, " "), err)
		}
		code = exit.ExitCode()
	}
	return out.String(), errBuf.String(), code
}

// The version is stamped at build time, so this checks the wiring rather than a
// number: whatever the variable holds is what the binary has to say.
func TestVersionIsPrintedOnStdout(t *testing.T) {
	for _, flag := range []string{"version", "-v", "--version"} {
		stdout, stderr, code := runBothy(t, flag)
		if code != 0 {
			t.Errorf("bothy %s exited %d, want 0 (stderr: %s)", flag, code, stderr)
		}
		if got, want := strings.TrimSpace(stdout), "bothy "+version; got != want {
			t.Errorf("bothy %s printed %q, want %q", flag, got, want)
		}
	}
}

// Help is the first thing anyone runs, so it has to list the commands and stay on
// the stream a pipe-then-page user expects.
func TestHelpDescribesEveryCommand(t *testing.T) {
	for _, flag := range []string{"help", "-h", "--help"} {
		stdout, stderr, code := runBothy(t, flag)
		if code != 0 {
			t.Errorf("bothy %s exited %d, want 0", flag, code)
		}
		if stdout != "" {
			t.Errorf("bothy %s wrote to stdout, want help on stderr", flag)
		}
		for _, want := range []string{"discovery", "share", "connect", "mock", "OpenAI-compatible"} {
			if !strings.Contains(stderr, want) {
				t.Errorf("help does not mention %q:\n%s", want, stderr)
			}
		}
	}
}

// Running the binary with no command is a usage error, and saying so with a
// success exit code would break every script that pipes it.
func TestNoArgumentsIsAUsageError(t *testing.T) {
	stdout, stderr, code := runBothy(t)
	if code != 2 {
		t.Errorf("exited %d, want 2", code)
	}
	if stdout != "" {
		t.Errorf("wrote %q to stdout, want usage on stderr", stdout)
	}
	if !strings.Contains(stderr, "Commands:") {
		t.Errorf("usage was not printed:\n%s", stderr)
	}
}

func TestAnUnknownCommandIsAUsageErrorThatNamesIt(t *testing.T) {
	_, stderr, code := runBothy(t, "frobnicate")
	if code != 2 {
		t.Errorf("exited %d, want 2", code)
	}
	if !strings.Contains(stderr, `unknown command "frobnicate"`) {
		t.Errorf("stderr does not name the command:\n%s", stderr)
	}
}

// The experimental command is a plugin: a default build has no `solve` in it and
// help does not mention one. docs/swarm.md says exactly that, and now `make check`
// is what keeps it true rather than only the tagged CI job.
func TestADefaultBuildHasNoSwarmInIt(t *testing.T) {
	_, help, _ := runBothy(t, "help")
	if strings.Contains(help, "solve") {
		t.Errorf("help offers solve in a default build:\n%s", help)
	}

	_, stderr, code := runBothy(t, "solve", "-task", "whatever.json")
	if code != 2 {
		t.Errorf("bothy solve exited %d, want 2 — it is not in this build", code)
	}
	if !strings.Contains(stderr, `unknown command "solve"`) {
		t.Errorf("stderr does not report solve as unknown:\n%s", stderr)
	}
}

// A command that cannot do its job has to fail loudly and with a non-zero status,
// because that status is what a supervisor or a compose healthcheck reads.
func TestACommandThatCannotStartExitsNonZero(t *testing.T) {
	for _, tc := range []struct {
		name string
		args []string
	}{
		{"discovery on an address it cannot bind", []string{"discovery", "-listen", "127.0.0.1:not-a-port"}},
		{"share with a quota it cannot parse", []string{"share", "-peer-quota", "200"}},
		{"mock with no models configured", []string{"mock", "-models", ""}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			_, stderr, code := runBothy(t, tc.args...)
			if code != 1 {
				t.Errorf("exited %d, want 1 (stderr: %s)", code, stderr)
			}
			if !strings.Contains(stderr, "exiting") {
				t.Errorf("stderr does not say the command failed:\n%s", stderr)
			}
			if !strings.Contains(stderr, "err=") {
				t.Errorf("stderr carries no error detail:\n%s", stderr)
			}
		})
	}
}
