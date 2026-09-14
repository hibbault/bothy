//go:build swarm

// Registering an experimental command, which is all this file does.
//
// It is build-tagged, so `go build ./cmd/bothy` has no `solve` in it and help
// does not mention one. `make swarm` compiles with -tags swarm and both appear.
// The code it calls lives in internal/swarm, which is tagged the same way.
package main

import (
	"context"
	"log/slog"

	"github.com/hibbault/bothy/internal/swarm"
)

func init() {
	experimentalUsage = `
Experimental — built with -tags swarm (make swarm), in no release binary, and
outside PROTOCOL.md. See docs/swarm.md for what it does and does not promise:
  solve       run a task: fan out into attempts, check each one, stop at the integrator
`
	experimentalCommand = func(ctx context.Context, log *slog.Logger, cmd string, args []string) (bool, error) {
		if cmd != "solve" {
			return false, nil
		}
		return true, swarm.Run(ctx, log, args)
	}
}
