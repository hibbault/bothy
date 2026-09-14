# Changelog

All notable changes to Bothy are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

Bothy will follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html) once
the wire contract in [PROTOCOL.md](PROTOCOL.md) stabilises. Until 1.0, treat that
contract as unstable between minor versions, and pin the version on both ends of
a connection if you care.

## [Unreleased]

### Fixed

- Streamed replies are counted. The mock engine reported token usage only on
  whole responses, so a streamed request could only ever be recorded as
  `unmetered_responses` — and streaming is how most interactive use arrives, so
  the one path that could not be counted was the common one. The mock now closes
  a stream with usage, as Ollama does and as OpenAI does when a client asks, with
  a test and a CI assertion holding it there.

## [0.1.0] - 2026-09-14

First release. The whole flow works end to end — a host shares an engine, a
client borrows it, and the client's local endpoint is indistinguishable from a
local Ollama. Nothing is frozen, and no host should be exposed to strangers yet;
see [SECURITY.md](SECURITY.md).

### Added

- **Four roles in one binary.** `discovery` runs a registry, `share` runs a host,
  `connect` runs a client, and `mock` runs a fake engine so the whole stack works
  with no GPU. One artifact because the roles overlap in practice.
- **A registry with heartbeats, not a static list.** A host re-registers on an
  interval and entries expire without one, so clients are never handed a dead
  host. Optional `BOTHY_REGISTRY_TOKEN` gates publishing.
- **Model identity by weights digest, not tag.** The host reads the weights layer
  out of an Ollama manifest, or hashes a file, or takes an explicit pin. The
  client refuses a host offering anything else, and refuses to start on a
  mismatch rather than warning.
- **A meter.** Requests, prompt and completion tokens, response bytes, refusals
  and unmetered responses are counted per peer, named by per-peer share keys.
  `BOTHY_MAX_CONCURRENT` and `BOTHY_MAX_REQUESTS_PER_MINUTE` enforce the caps the
  counts describe, and free slots are advertised to the registry as `capacity`.
- **A devnet with composable profiles.** `mock` for no GPU, `real` for Ollama,
  `mismatch` for a second host with deliberately different weights — the only way
  to exercise digest refusal without downloading a model twice.
- **Streaming that is actually streamed.** Both proxies flush per event, so
  tokens arrive as they are produced instead of in one lump at the end.
- **[PROTOCOL.md](PROTOCOL.md)**, so an implementation in another language is a
  peer and not a port, plus `examples/python/bothy_client.py` as the proof.

### Notes

- CI runs green on the first commit: gofmt, vet and tests, four cross-builds, and
  an end-to-end job that starts all four roles as ordinary processes and asserts
  a completion proxied through the client, a `401` without a share key, a refused
  digest, and a metered usage row.
- Every claim in this file is now exercised somewhere. The release builds and
  pushes the image for `linux/amd64` and `linux/arm64`; a CI job builds it again
  and brings the entire compose devnet up, then asserts a proxied completion, a
  `401` without the share key, a registry entry and a metered usage row; and the
  Python example is run against that same live stack, including a refusal, so
  "another language can implement this" is a test rather than a slogan.
- What is still not proven anywhere: that a digest is **honest**. A host can
  report any digest it likes, so checking one catches mistakes rather than lies.
  That is the open problem this release documents instead of pretending to have
  solved, along with reaching a host behind a home router.
- The meter is on the seller's hardware and a digest is a claim rather than
  proof, which is why there is no payment anywhere in this release.

[Unreleased]: https://github.com/hibbault/bothy/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/hibbault/bothy/releases/tag/v0.1.0
