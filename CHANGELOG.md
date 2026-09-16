# Changelog

All notable changes to Bothy are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

Bothy will follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html) once
the wire contract in [PROTOCOL.md](PROTOCOL.md) stabilises. Until 1.0, treat that
contract as unstable between minor versions, and pin the version on both ends of
a connection if you care.

## [Unreleased]

Three things: the implementation is Python now, sharing a GPU no longer means
giving it away, and one command runs whatever a machine can be.

### Added

- **A 429 moves the client along instead of being handed to the caller.** The
  client used to resolve one host and stay there, so a host saying "full" was the
  end of the request rather than the start of a search — a fleet with room in it
  still looked busy, which is the one failure a fleet exists to avoid. `429`,
  `503` and `502` now mean "ask somebody else": up to three hosts are tried in one
  request, and the caller is told only when every host refused, with the shortest
  `Retry-After` on offer. A refused host is left alone until its `Retry-After`
  passes (capped at five minutes) and the list rotates, so a fleet takes turns.
  A host that cannot be reached is a `502` rather than a wait, because waiting for
  a broken fleet means waiting forever. A request body too large to hold in memory
  is sent once and gets no retry; `/bothy/status` now lists the hosts the client
  knows and which of them are being skipped, and why.
- **`free` replaces `capacity` in the registry, and absent is not zero.** The old
  field meant "free peer slots" and "unknown" with the same number, so an uncapped
  host sorted *behind* a full one. `free` is omitted by a host that has nothing to
  report, which is a different claim from reporting none — the design's own "bug
  waiting for a field". Hosts that reported something sort first, most free first;
  hosts that said nothing sort last but are still used.
- **A config file, so limits survive a reboot as something you can read.** Every
  setting already had an environment variable; now it can also be written in a
  file, using the same names, at `%AppData%\bothy\config` (or the equivalent per
  user config directory), and `bothy config init` writes a commented one to
  uncomment from. Precedence is flag, then environment, then file, then built-in
  default, implemented by the file supplying the defaults the flags are parsed
  against — so flags and environment keep exactly the meaning they had. A file
  that exists is read strictly: an unparseable line is an error naming the line,
  and an unknown setting is an error naming it and suggesting the one that was
  meant, because a typo that silently configures nothing is the failure mode a
  file listing your limits must not have. `bothy config path` says which file is
  in use. `-config`/`BOTHY_CONFIG` points elsewhere, and the file is written
  `0600` since it may hold a share key.
- **A host proxies inference and nothing else.** The engine's own control routes
  are no longer reachable through a host: `POST /api/pull` fills its disk,
  `DELETE /api/delete` removes its models, and with no share key — the supported
  way to run a public host — reaching them took nothing but a port number. This
  was demonstrated against a real Ollama before it was fixed: the delete route
  reached the engine and came back with the engine's own "model not found", which
  means a real model name would have been deleted. Anything outside the inference
  routes now answers `404` without touching the engine. `-allow-routes "POST
  /api/pull,GET /api/blobs/"` opens specific paths for an engine Bothy does not
  know, and `-allow-all-routes` restores the old behaviour for a private network —
  loudly, on startup.
- **A per-peer slot cap.** `-peer-max-concurrent` bounds how many of the host's
  slots one caller may hold at once. Without it the cap is first-come-first-served
  and one client with parallel requests occupies the whole GPU while everybody
  else is told the host is full — a taken machine rather than a shared one. The
  refusal distinguishes the two cases, because "the host is full" is a reason to
  try another host and "you already have your share" is a reason to wait.
- **A time limit for one request.** `-max-request-time 10m` is the only lever that
  bounds how long a single generation can hold the GPU. The caller gets a `504`
  naming the limit, and the slot is released either way.
- **A request body cap.** `-max-body`, 32 MiB by default, answers `413` before the
  engine is asked, and catches a body that lies about its length or arrives
  chunked. Prompts carrying images are legitimately megabytes, so the default is
  generous; the point is that it is bounded at all, where before it was not.
- **Every limit is now visible.** The host narrates each one at startup and
  reports it in `/bothy/healthz` and `/bothy/usage` — including `routes`, so "can
  a peer reach my engine's control API?" has an answer that is not shell history.
- **The registry has a page.** `GET /` renders the live entries `/models` already
  returns — model, host, address, free peer slots, an abbreviated digest, and how
  long ago the last heartbeat was — so "is this registry doing anything?" does not
  need `curl` and `jq`. It is a view of the contract rather than a second one: no
  state, no query to express a lookup with, and no per-client logging, so it
  cannot become the metadata leak the design warns about. `POST /register` is
  still the only write.
- **`bothy run` — one service, and no role to pick.** It probes for a local
  engine: if one answers with a model it shares it *and* opens the borrowing
  endpoint, because serving and borrowing are different ports and wanting both at
  once is ordinary — your own model locally and somebody else's for what your GPU
  cannot hold. No engine answering, no models, or no share key means it only
  borrows; the last one is a refusal rather than a warning, because an automatic
  mode must not open a GPU to anyone who can reach the port when nobody asked it
  to. A failure in either half stops the process instead of leaving half a
  service, and the error names which half failed.
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

- **The implementation is Python, and the Go tree is gone.** The protocol was
  always the contract rather than the program — PROTOCOL.md said as much from the
  start — so replacing the implementation changes how Bothy is written and not
  what it does. Each package was migrated one at a time and test first: the Go
  test files were the specification, ported a test at a time, and a Go package was
  deleted the moment its Python equivalent passed, so the tree never held two
  implementations of anything. The wire contract, the `BOTHY_*` settings, the
  config file and the command names are unchanged, and the Python modules carry
  the Go comments that explained *why* each decision was made, because that was
  the most valuable thing being ported. What changes for a user is on the outside:
  there is no binary and no build step — `python -m bothy run` from a checkout, on
  Python 3.9 or newer, with nothing installed — and the container image is an
  interpreter beside the source rather than a static binary.
- **`make check` now compiles and tests, and `make e2e` runs the stack.** The
  end-to-end scenario that used to live inside the CI workflow is
  [`scripts/e2e.sh`](scripts/e2e.sh): four real processes on real ports, the same
  script CI runs, so a claim tested there is a claim anyone can check locally.
  There is no formatter in the toolchain now that nothing is installed, which
  CONTRIBUTING.md says out loud rather than leaving to be discovered.
- **Release artifacts are gone with the binary.** A release is a tag: there is
  nothing to cross-compile, so there are no per-platform downloads and no
  `SHA256SUMS` to check.
- **`-host https://box:7777` is no longer dialled in cleartext.** The client kept
  the scheme when it asked a directly-addressed host for its model list and then
  dropped it when storing the address for the proxy, which re-added `http://` — so
  a TLS host was asked over TLS and served over HTTP, which is the worst of both.
  The scheme is now part of the address that gets dialled.
- **Servers bound IdleTimeout and MaxHeaderBytes.** `ReadHeaderTimeout` was the
  only one set, which left keep-alive connections open indefinitely and Go's 1 MiB
  default header limit in place. There is still deliberately no `WriteTimeout`:
  responses stream for minutes and a write deadline would cut a long generation in
  half.
- **The client listens on `11223`, not on Ollama's `11434`.** Bothy sits beside a
  local engine rather than impersonating it: an engine keeps its own port, the
  host keeps `7777`, and the client takes one of its own. That is what lets one
  machine serve its own model and use somebody else's in the same session, which
  the old default made impossible — and it stops a machine that already runs an
  engine from having to give the port up or fight over it. The cost is that a tool
  which should use the fleet is pointed at `11223` instead of finding Bothy where
  its Ollama used to be: one setting, in the tools that want the fleet, with the
  rest left alone. The old default is gone rather than kept behind a flag, because
  the number it used is exactly the collision this removes. `bothy run` reads
  `BOTHY_HOST_LISTEN` and `BOTHY_CLIENT_LISTEN`, since one process cannot use one
  `BOTHY_LISTEN` for two ports; a plain `BOTHY_LISTEN` still means the host's.
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

### Removed

- **The experimental task runner (`bothy solve`), its package and its document.**
  It was added as a plugin rather than a feature: behind a build tag, in no
  release binary, and outside PROTOCOL.md. It was also the one thing that could
  not survive the move to Python as it stood — a build tag has no Python
  equivalent, and the Go package stopped building the moment the code it imported
  became Python. Retiring it was the choice over porting ~2,300 lines of an
  experiment that never shipped, and the reasoning is not lost: the file was
  `docs/swarm.md` in git history, and the [roadmap](README.md#roadmap) now says
  the idea is parked rather than disproved. Nothing outside it imported it, which
  was the standard it had to keep: experimental work must be removable without
  unpicking the project around it.

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
