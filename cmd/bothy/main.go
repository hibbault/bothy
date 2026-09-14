// Command bothy is the whole project in one binary: the registry, a host that
// shares a GPU, a client that borrows one, and a mock engine for testing.
//
// One binary because the roles overlap: a person who shares a GPU is usually
// also the person who wants a client running, and shipping one artifact means
// installing Bothy is copying a file or pulling an image.
package main

import (
	"context"
	"fmt"
	"log/slog"
	"os"
	"os/signal"
	"syscall"

	"github.com/hibbault/bothy/internal/client"
	"github.com/hibbault/bothy/internal/discovery"
	"github.com/hibbault/bothy/internal/host"
	"github.com/hibbault/bothy/internal/mockengine"
)

// version is stamped at build time (`-X main.version=…`) so that a released
// binary can say which release it is. See the LDFLAGS in the Makefile.
var version = "0.2.1-dev"

func main() {
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	log := slog.New(slog.NewTextHandler(os.Stderr, &slog.HandlerOptions{Level: slog.LevelInfo}))

	if len(os.Args) < 2 {
		usage()
		os.Exit(2)
	}
	cmd, args := os.Args[1], os.Args[2:]

	var err error
	switch cmd {
	case "discovery":
		err = discovery.Run(ctx, log, args)
	case "share":
		err = host.Run(ctx, log, args)
	case "connect":
		err = client.Run(ctx, log, args)
	case "mock":
		err = mockengine.Run(ctx, log, args)
	case "version", "-v", "--version":
		fmt.Println("bothy", version)
		return
	case "help", "-h", "--help":
		usage()
		return
	default:
		fmt.Fprintf(os.Stderr, "bothy: unknown command %q\n\n", cmd)
		usage()
		os.Exit(2)
	}
	if err != nil {
		log.Error("exiting", "command", cmd, "err", err)
		os.Exit(1)
	}
}

func usage() {
	fmt.Fprint(os.Stderr, `bothy — share a GPU, borrow a GPU.

The client's local endpoint is OpenAI-compatible, so any tool that speaks OpenAI
(or Ollama) can talk to somebody else's GPU with no changes. Bothy never moves
model weights: it registers what an engine can serve, and proxies to it.

Commands:
  discovery   run a registry: hosts announce, clients look up
  share       run a host: proxy an inference engine and announce its models
  connect     run a client: expose a local OpenAI-compatible endpoint
  mock        run a fake engine, for testing the stack without a GPU

Every flag has a BOTHY_* environment default, which is how the containers are
configured. Run any command with -h to see its flags.

Examples:
  bothy discovery
  bothy share   -engine-url http://localhost:11434 -share-key secret
  bothy connect -discovery-url http://localhost:8080 -model llama3.1:8b

Then point anything OpenAI-compatible at http://127.0.0.1:11434/v1
`)
}
