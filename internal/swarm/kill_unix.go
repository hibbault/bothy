//go:build swarm && !windows

package swarm

import (
	"os/exec"
	"syscall"
)

// prepareCommand puts the check in its own process group, so that killing it can
// take its children with it.
func prepareCommand(cmd *exec.Cmd) {
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
}

// killCommand kills the check and everything it started.
//
// The default is Process.Kill, which kills only the shell. A check is usually
// `go test ./...` or a script, so the process that matters is a child: killing
// the parent leaves the child running, the child holds the output pipe open, and
// the caller waits out the child's full lifetime. A timeout that does not time
// anything out is worse than no timeout.
func killCommand(cmd *exec.Cmd) error {
	if cmd.Process == nil {
		return nil
	}
	if err := syscall.Kill(-cmd.Process.Pid, syscall.SIGKILL); err != nil {
		return cmd.Process.Kill()
	}
	return nil
}
