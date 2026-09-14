# The swarm

Bothy shares a **GPU**. This document is about what it would take to share a
**problem** — to hand a fleet of borrowed machines something to solve rather than
something to answer.

Part of it is a design and part of it is built: stage v0 exists, in this
repository, behind a build tag.

## It is a plugin, not a feature

Nothing here is in a release binary, and PROTOCOL.md does not describe it. That is
deliberate, and it is enforced by the build rather than promised in prose:

| | |
| --- | --- |
| `make build` | no swarm code in the binary; `bothy solve` does not exist and help does not mention it |
| `make swarm` | builds `bin/bothy-swarm`, with the command in it |
| `make swarm-check` | gofmt, vet and test the tagged packages — **deliberately not part of `make check`** |
| releases | never include it. There is no swarm artifact on any release |
| [`PROTOCOL.md`](../PROTOCOL.md) | says nothing about it. Nothing interoperates with it and nothing else needs to |

The mechanism is a Go build tag, `swarm`, on every implementation file. The only
thing a default build sees is an untagged `doc.go`, because a package whose every
file is tagged makes `go build ./...` fail for people who never asked for this.

It is *not* Go's `plugin` package: `plugin.Open` needs cgo, an exactly matching
toolchain on both ends, and fails rather than degrades. A build tag is the version
of "opt-in" that works on every platform Bothy ships for, Windows included.

Nothing in `internal/swarm` is required by anything else, so deleting the
directory removes the feature completely. That is the standard this has to keep:
experimental work must be removable without unpicking the project around it.

## The filter that decides everything

Decomposing a problem, dispatching pieces, collecting results and integrating them
is a **DAG scheduler**, and a DAG scheduler is a solved, boring problem.

Verification is not.

So the whole design turns on one rule:

> **Only farm work whose answer can be checked more cheaply, and more
> trustworthily, than it can be produced.**

Everything else follows from it. "Research this and write it up" fails the filter
because nothing cheap can check the answer. "Make this failing test pass" passes
it, because `go test ./...` either exits zero or it doesn't.

The rule has an uncomfortable corollary: **if you cannot say how a result is
checked, it cannot be farmed.** An LLM grading another LLM's prose is not
verification — it is an opinion machine with extra latency and a bill. Being
strict here is what makes the rest possible, and relaxing it is how this kind of
system becomes useless while still looking busy.

This is the same discipline as the digest decision, one level up. A host can
report any weights digest it likes, so Bothy refuses to depend on the claim and
verifies at the boundary it can. A worker can claim anything, so a worker's output
is believed only through its evidence.

Within that rule, the roles split cleanly:

- **Fan out what is checkable. Keep judgment centralized.**
- **Workers are untrusted, stateless and replaceable.**
- **The planner and the integrator are not.** Planning cannot be checked cheaply,
  which is exactly why it does not fan out.

## Two decisions, taken deliberately

**Code and engineering tasks first.** A test run is proof. Nothing else available
here is. Arithmetic can be checked by recomputation and logic by a checker, but
tests are the only oracle in reach that is cheap, reproducible and already lying
around in every repository.

**The submitter's machine executes; the fleet only serves inference.** This is the
decision that keeps the threat model identical to the one in
[SECURITY.md](../SECURITY.md): a host still runs inference and nothing else. The
alternative — dispatching "run this in a sandbox on your GPU box" — is
substantially more powerful and substantially more dangerous, and it would mean
writing a new security document rather than extending an existing one. It also
puts the sandboxing burden on the person who submitted the work, which is the
person best placed to know what the work does.

## The shape of a task

A task is a goal, an acceptance criterion, and a DAG. The acceptance criterion is
mandatory: that is what "accept the problem" means in practice, and a task without
one is refused rather than run.

```json
{ "goal": "three attempts at one prompt",
  "model": "llama3.1:8b",
  "accept": { "type": "agreement", "nodes": ["attempt-1", "attempt-2-1", "attempt-2-2"] },
  "nodes": [
    { "id": "attempt-1", "prompt": "name the capital of France",
      "check": { "type": "command", "run": "test -s \"$BOTHY_ARTIFACT\"" } },
    { "id": "attempt-2", "same_as": "attempt-1", "count": 2 },
    { "id": "join", "kind": "verify",
      "depends_on": ["attempt-1", "attempt-2-1", "attempt-2-2"],
      "check": { "type": "agreement", "nodes": ["attempt-1", "attempt-2-1", "attempt-2-2"] } }
  ] }
```

Five things are load-bearing:

- **`accept` is required, and the runner will not guess what "solved" means for
  you.** A task without one is refused at load time, before any GPU is touched.
- **A result is an artifact plus evidence, never a claim.** Artifacts are written
  to disk addressed by the SHA-256 of their bytes, for the same reason models are.
  A report names the digest it judged, so "which bytes passed" is answerable
  afterwards.
- **`verify` is a separate node, and it cannot produce.** A verify node with a
  prompt is refused: a checker that produces is a producer, and the entire point
  of the split is that it is not one.
- **`same_as` copies, it does not modify.** A copy inherits its prompt, model,
  check and requirements, and declaring any of those on the copy is refused.
  Overriding half of what is being copied makes "the same attempt" mean two things
  at once.
- **`needs` is refused, not ignored.** Requirements on the serving machine — a
  model, a weights digest — are stage v2. Accepting the field and doing nothing
  with it would leave a task looking constrained when it is not, which is worse
  than a clear refusal.

Every one of those is a refusal with a test behind it, in the style the rest of
this repo already uses: the interesting tests are the ones that assert a refusal.

## The three checks

A check is how "did this work" gets answered without asking a model. Three exist,
and the type is a closed set.

| Check | Passes when | Notes |
| --- | --- | --- |
| `command` | the shell command exits 0 | given `$BOTHY_ARTIFACT`, `$BOTHY_ARTIFACT_DIR`, `$BOTHY_NODE`, `$BOTHY_DIGEST`; on timeout it fails, and the whole process group is killed |
| `exact` | the artifact matches `want` | line endings and surrounding whitespace are normalised, nothing else |
| `agreement` | at least `count` (default: a majority) of the named artifacts are identical | a check on determinism, not on quality |

Two honest limits on `agreement`, because it is the one most likely to be
misused:

- **Agreement is not quality.** Three attempts agreeing says they agree. It is fit
  for a computed value, an identifier or a classification, and unfit for prose,
  which is why it is a count rather than a vote.
- **A majority of free identities is worth nothing.** Anyone can spin up a hundred
  workers and vote wrong. Consensus has to rest on a check; counting identities is
  how this kind of system is gamed.

## What v0 actually is, and what it is not

```sh
make swarm
./bin/bothy-swarm solve -task task.json -plan      # validate and show the plan
./bin/bothy-swarm solve -task task.json \
    -engine-url http://127.0.0.1:11434 -model llama3.1:8b
```

It reads a task file, expands `same_as`, runs the nodes whose dependencies are
met, checks each one, writes every artifact under its digest, and evaluates the
accept criterion. `-json` prints the report; `report.json` is written next to the
artifacts whether or not the run succeeded, because a failed run is exactly the
one worth being able to read afterwards. It stops at the first failed node and
does not retry — retrying is a policy question (how many attempts, whose budget,
which host) and a policy invented before the loop is shown to work is a guess with
extra steps.

Inference comes from an OpenAI-compatible endpoint, which means it is the *same*
surface `bothy connect` exposes and `bothy share` proxies. Pointing it at a
borrowed GPU is one flag:

```sh
bothy connect -discovery-url http://registry:8080 -model llama3.1:8b -share-key s &
bothy-swarm solve -task task.json -share-key s      # inference from somebody else's GPU
```

No new transport, no new protocol, and nothing for PROTOCOL.md to describe.

It is not a daemon, there is no coordinator, nothing is dispatched to another
machine, and workers are not untrusted yet because there is only ever one of them.
Those arrive with the stages below, in that order, and each is useful on its own.

## Why a fleet helps at all

Not because it has more FLOPS. Because it can make **independent attempts**.

For LLM work, *N attempts plus a checker* beats one long attempt far more reliably
than it beats it on wall-clock. The swarm is not a bigger GPU; it is a **search
over attempts**. That is also what makes it work at all on the size of model
people can actually afford to share.

Which leads to the honest caveat: **attempts from the same weights with the same
prompt are correlated failures.** Sampling temperature buys less diversity than it
looks like. The genuinely interesting asset here is that the fleet is
*heterogeneous* — one attempt from `llama3.1:8b`, one from `qwen2.5:7b`, one from a
different quantization entirely. Diversity is the feature, and it is a reason to
identify models by digest rather than by name.

## Staging

Each stage is useful on its own and none of them require the next one.

| | What it is | Why it comes here |
| --- | --- | --- |
| **v0** | One machine: a DAG, the three checks, artifacts by digest, the loop end to end. No fleet, no network. | If decompose → verify → integrate does not work locally, distributing it multiplies the failure rather than the capacity. **Built.** |
| **v1** | Best-of-N over the fleet: N attempts of one node, one checker. | The first stage where the fleet earns its keep, and it needs nothing new but attempts and a check. |
| **v2** | Heterogeneous nodes: a node declares what it needs, the coordinator matches it to a host. | Where model diversity starts paying, where `needs` stops being refused, and where the registry becomes a scheduler's input. |
| **v3** | Untrusted workers: redundancy, spot-checks, reputation. | The hard one, and the one where the project's habit of documenting what it cannot prove matters most. |

## Where this kind of project dies

1. **Verification costing more than production.** The reason v0 is code-only. Every
   task type added later has to be argued past the filter, not added because it
   sounds useful.
2. **Decomposition errors compounding.** A bad split produces confidently wrong
   joins, and the failure looks like a worker failing rather than a plan being
   wrong. Distinguishing the two is the real skill, and the reason the integrator
   stays human for now.
3. **Sybil.** Majority-of-N is worthless when identities are free: anyone can spin
   up a hundred workers and vote wrong. Consensus must rest on a **check**, never on
   counting identities. Reputation can order work; it cannot be the thing that
   decides whether an answer is right.
4. **Coordination overhead.** If a subtask is thirty seconds of GPU and planning is
   five minutes of inference, the swarm is slower than one careful attempt. Fan out
   only when the work is large relative to the orchestration.
5. **Tests as an oracle are only as good as the tests.** A node can pass by
   weakening the check — deleting the failing assertion is a green test. v0 farms
   **answers**, not patches, so it does not yet apply an artifact to a working tree
   at all; when it does, the mitigation is that the checker runs the check from a
   clean tree against the original tests, not the ones the attempt brought with it.

## What is still unsolved

Written down rather than discovered later:

- **Two passing solutions cannot be ranked by a check.** `go test` says pass or
  fail, not which is better, and `agreement` says the attempts agreed, not that
  they were right. Choosing between two passing answers is judgment, and it stays
  with the submitter.
- **Proof of work.** A digest proves which weights a host *claimed*, not that a
  given artifact came out of them. The same open problem as before, one level up.
- **Cost accounting for coordination.** The meter counts tokens per peer. It has no
  notion of a task, so it cannot yet answer "what did this problem cost".
- **A worker that returns the artifact you wanted, unsolved.** Evidence closes most
  of this, but "the check passed for the wrong reason" is a real category.
- **Farming patches.** v0's artifacts are text. Code artifacts mean applying a diff
  to a tree, which means a sandbox and a new security document, and that is not
  something to bolt on quietly.
