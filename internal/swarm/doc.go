// Package swarm is the experimental task runner: it takes a problem, fans it
// into independent attempts, checks each attempt with something cheaper than
// producing it, and stops at the integrator, which is a person.
//
// # A plugin, not a feature
//
// Everything else in this repository builds by default and is described by
// PROTOCOL.md. This package is neither:
//
//   - Every other file here carries `//go:build swarm`, so a default
//     `go build ./cmd/bothy` contains none of this code and `bothy solve` does
//     not exist. Opting in is explicit: `make swarm` builds it, `make
//     swarm-check` tests it.
//   - No release binary includes it and none is promised to. Taking it out is
//     deleting a directory, not unpicking a feature.
//   - PROTOCOL.md does not cover it. It is not a wire contract, nothing
//     interoperates with it, and it can change shape between commits.
//
// The reasoning is in docs/swarm.md, including the rule that decides what may be
// farmed at all and the parts that are still unsolved.
//
// # Why this file has no build tag
//
// Deliberately untagged, and deliberately empty. Go reports "build constraints
// exclude all Go files" for a package whose every file is tagged, which would
// turn `go build ./...` and `go test ./...` into errors for people who never
// asked for this. One untagged file keeps the default build green and the
// package legitimate, while the tag keeps the code out of it.
//
// # Not Go's plugin package
//
// plugin.Open needs cgo, an exactly matching toolchain on both sides, and fails
// rather than degrades. A build tag is the version of "opt-in" that works on
// every platform Bothy ships for, Windows included.
package swarm
