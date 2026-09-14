# Changelog

All notable changes to Bothy are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

Bothy will follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html) once
the wire contract in [PROTOCOL.md](PROTOCOL.md) stabilises. Until 1.0, treat that
contract as unstable between minor versions, and pin the version on both ends of
a connection if you care.

## [Unreleased]

Two independent things: sharing a GPU no longer means giving it away, and there is
a task runner behind a build tag.

### Added

- **The owner keeps a slot.** `-owner-reserve` (default 1) holds that many of
  `-max-concurrent` out of peers' reach, so your own request never queues behind
  four strangers. It is a guarantee of headroom rather than a measurement of what
  you are doing — your own traffic never passes through the host, and no portable
  engine API reports whether an engine is busy, so there is no signal to detect.
  The reservation is applied inside the `capacity` a host already advertises, so a
  host whose last free slot is the owner's simply looks full and clients route
  elsewhere without needing to know why.
- **Per-peer budgets.** `-peer-quota 200/1h` is 200 requests and then wait for the
  window to turn over, which is what stops one person using your GPU all day; a
  rate limit only slows them down, since 30 a minute is still 43,200 a day. It
  counts **requests, not tokens**, deliberately: tokens are known only after a
  response has been produced, so a token budget could only be enforced after the
  fact, and an engine that reports no usage — which is allowed — would evade it
  entirely.
- **`POST /bothy/sharing`**, guarded by `-admin-key`, pauses and resumes sharing
  without stopping the process. Peers get a `503` saying what happened, the host
  stops announcing itself so clients route elsewhere, and the refusal is not
  counted against the peer because it is not their doing. An unset admin key means
  no control surface at all, and it is deliberately not a share key: peers hold
  those, and a peer who can stop your host is worse than no control.
- **`-paused`** starts a host paused, which makes a sharing schedule two cron
  entries — pause at 9am, resume at 6pm.
- `quota_used` and `quota_reset` per peer in `/bothy/usage`, so an owner watching a
  peer stop can tell a spent budget from a crash.

### Changed

- **The limiter decides before it spends.** A request refused for one reason no
  longer consumes another limit's allowance, so a peer turned away by a full host
  does not also lose part of its budget for work that was never done.
- The concurrency refusal now reads "the host is serving as many peer requests as it
  allows right now", rather than "maximum concurrent requests": with a reserve, a
  host at its peer maximum is not at its maximum.
- All three `429` reasons — peer capacity, rate and budget — set `Retry-After`, not
  only the rate one. Every one of them is a "come back later".
- An `-owner-reserve` that leaves no room for peers is refused at startup rather
  than started as a host that reports healthy while serving nobody.
- `/bothy/healthz` and `/bothy/usage` report `owner_reserve`, `peer_slots`,
  `peer_quota` and `paused`.

### Fixed

- A non-positive `-heartbeat` / `BOTHY_HEARTBEAT` is refused at startup. It used
  to reach `time.NewTicker` in the announce loop, which panics, so a zero left in
  an environment file was a crash inside a goroutine rather than an error naming
  the setting.
- A non-positive `-ttl` / `BOTHY_REGISTRY_TTL` is refused at startup. Every
  registration expired on arrival, so the registry answered every lookup with
  nothing while reporting itself healthy.

### Added — experimental

An experimental task runner, added as a plugin rather than a feature: it is
build-tagged, it is in no release binary, and PROTOCOL.md does not cover it.
Nothing above depends on it, and it depends on nothing above.

- **`bothy solve` — a task runner, behind `-tags swarm`.** It reads a task file (a
goal, a **mandatory** accept criterion, and a DAG), expands `same_as` copies into
independent attempts, runs the nodes whose dependencies are met, checks each one,
and writes every artifact to disk addressed by the SHA-256 of its bytes. `-plan`
validates and shows what would run without touching a GPU. See
[docs/swarm.md](docs/swarm.md).
- **Three checks**, all of which answer "did this work" without asking a model:
`command` (a shell command, given the artifact's path in `$BOTHY_ARTIFACT`, with
the whole process group killed on timeout), `exact`, and `agreement` (a count, not
a vote).
- **A refusal suite over the task file**, in the style the rest of the repo
already uses: no accept criterion, a verify node that produces, a cycle, a copy
that overrides half of what it copies, an ambiguous answer, work whose result
nothing uses, `needs` requirements the runner cannot honour, and a misspelled
field. All refused at load time, before any GPU is touched.
- `make swarm` and `make swarm-check`, plus a CI job that first asserts a default
build contains no trace of the swarm and then runs the loop end to end against the
mock engine.

### Notes — experimental

- `make check` does not test the swarm, deliberately: a contributor should not
  meet experimental code unless they asked for it. `make swarm-check` does, and CI
  runs it separately.

## [0.2.0] - 2026-09-14

A host can finally meter the way people actually use a GPU. Interactive use
arrives as a stream rather than a whole response, and an OpenAI-compatible engine
reports no token usage on a stream unless the request asks for it — so the one
case that mattered most was the one that went uncounted.

### Added

- **The host asks its engine for streamed token usage.** An OpenAI-compatible
  engine reports no usage on a stream unless the request asks for it, so a host
  whose peers all streamed could serve for hours and meter nothing — which is the
  commonest way anyone uses a GPU. Streamed requests on the two OpenAI routes now
  go out with `stream_options.include_usage`. It is the only request body Bothy
  ever rewrites: it never overrides a caller's own preference, it forwards bodies
  over 1 MiB untouched rather than buffering them, and `BOTHY_STREAM_USAGE=false`
  disables it for an engine that objects.
- `BOTHY_STREAM_USAGE` / `-stream-usage` on `bothy share`, and a config helper for
  booleans that accepts `yes`/`no`/`on`/`off`, because those spellings are what
  end up in compose files and shell exports.

### Changed

- The mock engine now reports streamed usage only when it is asked, matching a
  real OpenAI-compatible engine rather than being more generous than one. That
  makes the devnet exercise the case the host exists to work around, and it turns
  the CI assertion about streamed replies into a test of the injection instead of
  a test of the mock's goodwill.

## [0.1.1] - 2026-09-14

A release about verification rather than features. Nothing changed for anyone
sharing or borrowing a GPU; what changed is that the parts of the stack nobody
had ever run now run on every push, and one accounting gap those runs exposed was
fixed.

### Added

- **The compose devnet runs in CI.** It builds the image, brings all four
  containers up, and then asserts a completion proxied through the client, a
  `401` without the share key, a registry entry and a metered usage row. Until
  now the profiles had only ever been described.
- **The Python example runs in CI, against a live host** — a registry lookup,
  digests, a missing-key refusal, and a digest mismatch. That makes
  [PROTOCOL.md](PROTOCOL.md)'s claim that another language is a peer rather than a
  port into something that fails loudly if the protocol drifts.
- **`make dist`** builds the release artifact set locally, and `bothy version` is
  stamped at build time, so a binary can say which release it is.

### Fixed

- **Streamed replies are counted.** The mock engine reported token usage only on
  whole responses, so a streamed request could only ever be recorded as
  `unmetered_responses` — and streaming is how most interactive use arrives, so
  the one path that could not be counted was the common one. The mock now closes a
  stream with usage, as Ollama does and as an OpenAI-compatible engine does when a
  client asks for it.

### Notes

- An engine that reports streamed usage only when it is asked
  (`stream_options.include_usage`) can still leave a streamed reply unmetered. The
  host does not inject that field, and the client does not either, because neither
  proxy rewrites request bodies — that is a design decision, not an oversight. It
  is recorded in [README.md](README.md) rather than papered over. (Superseded: the
  host now asks for streamed usage, in [Unreleased].)

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

- Three things shipped unverified by their author, because the development
  environment had no Docker, no Python and no CI runner: the Docker image with its
  compose profiles, the Python example, and the CI workflow's own structure. The
  first two were closed in 0.1.1; the third was closed by the first push.
- What is still not proven anywhere: that a digest is **honest**. A host can
  report any digest it likes, so checking one catches mistakes rather than lies.
  That is the open problem this release documents instead of pretending to have
  solved, along with reaching a host behind a home router.
- The meter is on the seller's hardware and a digest is a claim rather than
  proof, which is why there is no payment anywhere in this release.

[Unreleased]: https://github.com/hibbault/bothy/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/hibbault/bothy/releases/tag/v0.2.0
[0.1.1]: https://github.com/hibbault/bothy/releases/tag/v0.1.1
[0.1.0]: https://github.com/hibbault/bothy/releases/tag/v0.1.0
