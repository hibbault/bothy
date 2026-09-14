//go:build swarm && windows

package swarm

import "os/exec"

// prepareCommand does nothing here: there is no portable process-group flag, and
// a job object would be the Windows answer. Documented rather than pretended.
func prepareCommand(cmd *exec.Cmd) {}

// killCommand kills the check's own process. On Windows a child it started may
// outlive it, which is a known gap in the timeout on this platform.
func killCommand(cmd *exec.Cmd) error {
	if cmd.Process == nil {
		return nil
	}
	return cmd.Process.Kill()
}
