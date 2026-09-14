# Design notes

The reasoning behind Bothy: what it is, why it is shaped this way, and which
questions are still open. The [README](../README.md) is the pitch,
[PROTOCOL.md](../PROTOCOL.md) is the contract, and this is the *why*.

## The idea

Someone with a GPU runs Bothy next to the inference engine they already have.
Someone without one runs Bothy too, and gets a **local, OpenAI-compatible
endpoint that is really the other person's machine**.

Model weights never move. Bothy never does inference. It coordinates: it
announces what a GPU can serve, looks up who has a model, meters usage, and
proxies. All the math stays in Ollama, vLLM or llama.cpp, where it already was.

## Why this is worth building

GPUs are idle almost all the time, and the people who need them are usually not
the people who have them. The friction in the obvious fix — "just share your
box" — is not compute, it is *coordination*: exposing a port, agreeing which
model, proving the weights match, not getting your GPU pinned by one person.

The design goal that follows is: **nobody should have to change their tools.**
That single constraint explains most of the decisions below, including why the
client listens on the port Ollama already uses.

## What Bothy is not

Worth being blunt, because each of these is a plausible thing to mistake it for:

- **Not an inference engine.** No kernels, no tensors, no CUDA.
- **Not a model downloader.** Weights stay where they are; nothing is fetched.
- **Not a marketplace.** No payments, no incentives, no token.
- **Not anonymous.** Both sides see each other's IP.
- **Not model-parallel.** Each host holds whole models. Bothy routes; it does not
  split one model across machines — that is the Petals shape, a different project
  with different hard parts.

## The decisions that shape everything

### Proxy, don't reimplement

The host forwards every request it does not handle itself straight to the engine,
and the client does the same to the host.

This is the highest-leverage decision in the codebase. It means streaming works
by construction rather than by careful implementation, every OpenAI and Ollama
endpoint works the day it ships upstream, and the only engine-specific code is a
small lister that answers "what models do you have, and what are their digests?"

The cost is that Bothy mostly does not understand request bodies it proxies.
Counting tokens has to be done by observing responses — see metering below. That
is a real cost, and it is still much smaller than reimplementing an API.

There is exactly one exception, and it is worth naming rather than leaving to be
discovered in the diff. An OpenAI-compatible engine reports no usage on a stream
unless the request asks for it, so a host whose peers all streamed would meter
nothing, and streaming is how interactive use arrives. The host therefore adds
`stream_options.include_usage` to streamed requests on the two OpenAI routes.

It is kept as narrow as it can be: two routes, POST only, a body that has to
parse as JSON already saying `stream: true`, no `stream_options` key of its own,
under a megabyte, and switchable off. The rule it protects is that a proxy should
not silently change what a caller asked for — so the moment a caller has an
opinion, the rewrite stops. If a second exception ever seems necessary, that is
the moment to reconsider the decision rather than add another.

### The client keeps Ollama's port

The client listens on `127.0.0.1:11434` by default. A machine with no GPU quietly
*becomes* an Ollama as far as every editor, extension and CLI is concerned.

Any other port would work identically and be worth far less, because every tool
would need reconfiguring. A local-first default is the whole pitch in one number.

### Identity is a digest, not a tag

`llama3` on two machines can be different quantizations, different fine-tunes, or
different files. So a model is identified by the SHA-256 of its weights.

The honest caveat matters as much as the feature: **a digest catches honest
mismatches and proves nothing.** A host can report any digest it likes. Really
proving which weights ran needs deterministic re-sampling or attested hardware.
This is an open problem, not a solved one.

Two smaller rules fall out of it:

- Ollama's `/api/tags` reports the **manifest** digest, which changes whenever any
  layer does. Bothy reads the weights layer out of the manifest when it can,
  because that is the number that means "same model".
- An engine that reports no digest at all yields an *empty* digest, and an empty
  digest never satisfies a pinned expectation. "Unknown" must not read as
  "verified".

### Register *is* the heartbeat

Hosts re-`POST` their entries on a timer; entries expire without one. There is no
separate liveness endpoint, because two mechanisms that must agree eventually
disagree. The cost is one extra HTTP call per heartbeat, which is nothing.

The useful consequence: a host that crashed or slept falls out of the registry
instead of being handed to clients forever.

### Metering before money

The host counts requests, tokens, bytes and refusals per peer. That came before
any payment discussion, and deliberately: **you cannot charge for something you
do not count.** The counter also does immediate work with no money involved —
it enforces limits and produces the `capacity` a host advertises, which is how a
client can route to whoever is least busy.

Counting happens by observing the response as it streams past, which is the price
of the proxy-not-reimplement decision. An engine that reports nothing is recorded
as `unmetered_responses`, never as zero: the host cannot invent a count it was
never given.

## Discovery, or: what a registry cannot do

Discovery means one thing: **how does a client learn which host to talk to?** It
gets confusing because it is three problems wearing one name.

1. **Address exchange** — getting `host:port` to the client. Easy. It is a phone
   book: a file, a database, an API.
2. **Reachability** — *can the client actually connect?* Hard, and not a data
   problem. Most volunteer GPUs sit behind a home router with no open port and a
   changing IP, so a registry can list an address nobody can dial. Bothy does not
   solve this yet.
3. **Trust** — finding *an* address is not being allowed to use it, nor knowing it
   is the machine you think it is. Hence share keys and digests.

The registry here solves (1) only, and is deliberately dumb: it cannot check that
an address is up and it cannot check that a digest is honest. It is a bulletin
board, not an authority.

| | Approach | Cost |
| --- | --- | --- |
| 0 | Swap address and key by hand | nothing |
| 1 | A static JSON list at a URL | nothing |
| 2 | A registry with heartbeats ← **this repo** | a small service |
| 3 | DHT or gossip, signed lists | a lot |

Rung 0 is fine for a long time. The registry exists because two people who know
each other should not have to coordinate addresses by text message, not because
the network needs to scale yet.

## Anonymity, and why it is not here

The original plan was onion-first: hosts behind Tor onion services, a directory
mapping model to `.onion` address, clients reaching it over Tor. It was descoped
on purpose, because it made the project two hard things at once — "does
distributed inference work at all?" and "does it work anonymously?" — and because
the anonymity claim was thinner than it looked: Tor hides the two parties from
each other, but the host still reads your prompts in plaintext, and with a handful
of servers the anonymity set is tiny.

The good news is that it stays cheap to add, because the core is
transport-agnostic: the registry stores an opaque `address`, and the client dials
whatever that is. An onion address is a different string, not a different design.
Adding Tor is wrapping the listener and swapping the dialer, which is exactly why
that indirection exists.

The options, for whenever it comes back:

- **A. Onion service only.** Server IP hidden; client reaches it over Tor. Weakest
  and simplest. The registry must itself be an onion service, or it logs every
  model lookup against every client — the largest metadata leak in the design.
- **B. Rendezvous point.** A middle layer routes clients to a random host, hiding
  which host served which request. The rendezvous becomes the trust point.
- **C. Full mixnet.** Nym/Loopix-style. Strongest, highest latency, much more
  machinery. Not in scope.

## Money, and why there isn't any

If Bothy ever charges, the meter above is the foundation. But three things have to
be true first, and today none of them are.

**The economy of the three properties.** You cannot have all of anonymity,
payment, and strangers-serving-inference:

| Anonymity | Payment | Inference by strangers |
| --- | --- | --- |
| yes | no | yes — **Bothy today** |
| no | yes | yes — Akash Homenode |
| yes | yes | **unsolved** |

The bottom row is empty for a concrete reason: **the meter runs on the seller's
machine.** A paying host has a motive to lie about which model ran *and* about how
many tokens it produced, and we cannot detect either. Free, lying gains nothing.

**The precedent.** Akash — the most funded attempt at decentralized compute —
required its own token for payment, found that enterprises walked away rather than
eat 20% price swings, moved to letting tenants pay in USDC with stable provider
settlement, and then found that demand for their token eroded. Their fix (BME)
has users pay a stable amount which is converted behind the scenes, so nobody
holding compute has to hold a volatile asset. That sequence is worth reading
before designing anything in this space.

**The commission.** Taking a cut of payments between users and GPU providers is
close to the definition of money transmission, and it is precisely what converts a
piece of open-source software into a regulated financial intermediary. If money
ever flows, hosts should bill users directly and Bothy should stay a protocol.

## Open questions

- Does the client's local endpoint front one host or several? Today: one.
- Transport security between peers. Today HTTP, which is fine on a private network
  and not fine on the open internet. A pinned certificate is the obvious next step.
- What does `capacity` mean under load? Today it is free request slots, which says
  nothing about speed.
- Should the registry sign entries, so clients can detect tampering by the registry
  itself?
- How would a paid host prove what it served? This is the blocker for anything
  involving money.

## Deliberate non-goals for v1

Anonymity, payments, model parallelism, a public directory of strangers' GPUs, and
a library API — `internal/` is deliberately unimportable, because none of these
packages are promises.
