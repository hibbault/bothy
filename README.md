# Bothy

[![ci](https://github.com/hibbault/bothy/actions/workflows/ci.yml/badge.svg)](https://github.com/hibbault/bothy/actions/workflows/ci.yml)
[![python](https://img.shields.io/badge/python-3.9%2B-blue)](bothy)
[![license](https://img.shields.io/github/license/hibbault/bothy)](LICENSE)

**Share a GPU, borrow a GPU.**

A *bothy* is a small unlocked shelter in the Scottish hills. Nobody owns it,
nobody staffs it, nobody pays to sleep in it — volunteers keep it standing so
that whoever needs it can use it. That is the model here: people with spare GPU
capacity make it available to people who have none, for free, and Bothy handles
everything in between.

You have a GPU and a model running in Ollama (or llama.cpp, or vLLM). A friend
doesn't. They install Bothy, point their OpenAI client at Bothy's port, and their
tokens run on your GPU. No weights move. No model download on their side.

Your own engine is not touched, not reconfigured, and not impersonated. Ollama
keeps its port, Bothy has its own, and the two sit side by side — so the machine
that shares a model can use somebody else's in the same session.

## Documentation

| | |
| --- | --- |
| [PROTOCOL.md](PROTOCOL.md) | The wire contract. Everything speaks this, so anything can implement it |
| [docs/design.md](docs/design.md) | Why it is shaped this way, and what is still open |
| [CONTRIBUTING.md](CONTRIBUTING.md) | Setup, layout, and the ground rules |
| [SECURITY.md](SECURITY.md) | The threat model, and how to report a problem |
| [CHANGELOG.md](CHANGELOG.md) | What landed in each release |

## Install

No account, no installer, no build step, and nothing to install: Bothy is Python
with no dependencies outside the standard library, so the source tree is what
runs.

```sh
git clone https://github.com/hibbault/bothy
cd bothy

# or the container, to run beside an existing Ollama
docker build -t bothy .
docker run --network host -e BOTHY_ENGINE_URL=http://localhost:11434 \
  -e BOTHY_SHARE_KEY=a-secret bothy share
```

Python 3.9 or newer, and nothing else — no dependencies, no compilation, no
`pip install`. That is deliberate. A host runs on whatever machine already has
the GPU, and installing something should not be the step between a person and
sharing it.

One command runs the whole thing and picks the roles for itself:

```sh
python -m bothy run -share-key a-secret -discovery-url http://<registry>:8080
```

If a local engine answers with a model, that machine shares it **and** borrows;
if none does, it only borrows. The rest of this file names the two halves
explicitly, because a log line that belongs to one role is easier to read:

```sh
python -m bothy share   -engine-url http://localhost:11434 -share-key a-secret
python -m bothy connect -discovery-url http://<registry>:8080 -model llama3.1:8b -share-key a-secret
```

`python -m bothy version` says which release you are running. A release is a tag
rather than an artifact: what runs is the source tree, so there is nothing to
verify beyond the commit you are on.

## The idea in one picture

```
        Client machine                            Host machine (GPU)

  your OpenAI client                          inference engine
        │                                     (Ollama / llama.cpp / vLLM)
        ▼                                              ▲
  127.0.0.1:11223 ──── network ───────►  :7777 ────────┘
   bothy connect                      bothy share
```

Two roles, one command:

- **Host** (`python -m bothy share`) runs next to your engine and *proxies* to it. It does
  not touch your models and never downloads anything. It announces what you can
  serve, requires a share key, meters who uses what, and caps how much of your
  GPU any one peer can occupy.
- **Client** (`python -m bothy connect`) opens a **local, OpenAI-compatible endpoint**.
  Point anything at it and the tokens run elsewhere:

  ```sh
  curl http://127.0.0.1:11223/v1/chat/completions \
    -H 'content-type: application/json' \
    -d '{"model":"llama3.1:8b","messages":[{"role":"user","content":"hi"}]}'
  ```

And one command runs both, deciding for itself which the machine can be:

```sh
python -m bothy run
```

Three ports, one binary, none of them stolen:

| Port | What it is |
| --- | --- |
| `11223` | the client's local endpoint — how you borrow |
| `7777` | the host — how others reach your engine |
| `11434` | your engine's own port, untouched |

They are different on purpose. Serving and borrowing at the same time is an
ordinary thing to want — your own model locally, somebody else's for what your
GPU cannot hold — and two roles on one number cannot do that. Bothy also never
pretends to be an engine: your editor keeps talking to Ollama where it always
did, and the tools that should use the fleet are pointed at `11223` rather than
told a lie about where the model is.

## Try the whole thing right now, with no GPU

The devnet runs all four pieces in containers, with a fake engine standing in for
real inference:

```sh
make devnet          # or: COMPOSE_PROFILES=mock docker compose up --build
```

Windows without `make` (PowerShell, Docker Desktop running):

```powershell
$env:COMPOSE_PROFILES = "mock"; docker compose up --build
```

```
discovery    :8080   registry — hosts announce, clients look up
engine-mock  :11434  a fake engine that speaks enough OpenAI/Ollama to be useful
host         :7777   shares the fake GPU
client       :11223  borrows it; published on 127.0.0.1:11223 for you
```

Then talk to somebody else's GPU:

```sh
curl http://127.0.0.1:11223/v1/chat/completions \
  -H 'content-type: application/json' \
  -d '{"model":"llama3.1:8b","messages":[{"role":"user","content":"who answered this?"}]}'
```

The reply names the engine and the digest that produced it, so you can always
tell whose GPU actually answered. Ask the client where it ended up:

```sh
curl http://127.0.0.1:11223/bothy/status
```

To use a real GPU instead, `make real` and pull a model into the Ollama container.

### What the devnet is for

The three containers exist so the interesting failures are reproducible, not just
the happy path:

| Command | What it proves |
| --- | --- |
| `docker compose stop host` | after the registry TTL, the client stops being handed a dead host |
| `make mismatch` | a second host serves `llama3.1:8b` with **different weights**, and the client refuses it |
| `docker compose stop discovery` | already-connected clients keep working; only new lookups fail |
| `docker compose --scale client=5 up` | five clients against one host's cap — you should see refusals, not a pinned GPU |

The mismatch run is the important one. It is the only way to test digest
verification without downloading the same 8GB model twice:

```sh
# Point a throwaway client at the host whose weights are NOT the ones expected.
docker compose run --rm client connect \
  -host http://host-b:7777 -model llama3.1:8b \
  -expected-digest sha256:1111111111111111111111111111111111111111111111111111111111111111
# → refuses to start: digest mismatch
```

## Model identity: the same name is not the same model

`llama3` on your machine and `llama3` on mine can be different quantizations,
different fine-tunes, or different files entirely. So a model is identified by
the **SHA-256 of its weights file**, not its tag.

**How the host finds the digest**

- *Ollama:* the manifest under `~/.ollama/models/manifests/…` has a layer with
  mediaType `application/vnd.ollama.image.model` whose digest is the SHA-256 of
  the weights blob. Point `BOTHY_MODELS_DIR` at the models directory and Bothy
  reads it — no gigabyte hashing.
- *Ollama caution:* the `digest` in `/api/tags` is the **manifest** digest, which
  changes whenever any layer does. It is not the weights hash, so Bothy prefers
  the layer digest when it can read the manifest.
- *llama.cpp / vLLM:* these report no digest at all. Either list them explicitly
  (`BOTHY_MODELS="my-model=sha256:…"`) or hand Bothy the weights file
  (`BOTHY_WEIGHTS_PATH=…`) and it hashes it, caching the result against the
  file's size and mtime.

**How the client uses it:** give it `BOTHY_EXPECTED_DIGEST` and a host offering
anything else is refused. Leave it unset and any host is accepted, which is the
sane default — most people just want a working model.

**What a digest does and does not prove.** It catches honest mismatches: wrong
tag, wrong quant, stale pull. It does **not** prove the host actually ran those
weights — a dishonest host can report any digest it likes. Really proving it
needs deterministic re-sampling or attested hardware. That is an open problem
here, not a solved one.

## Who is using your GPU

A share key keeps strangers out. It says nothing about *how much* anyone is
using, though, so the host counts: requests, tokens in, tokens out, response
bytes and refusals, per peer.

```sh
curl -H 'X-Bothy-Key: key-a' http://localhost:7777/bothy/usage
```

```json
{ "in_flight": 1, "free": 3, "max_concurrent": 4, "owner_reserve": 1,
  "peer_slots": 3, "peer_quota": "200/1h", "paused": false,
  "peers": [
    { "peer": "alice", "requests": 3, "prompt_tokens": 6,
      "completion_tokens": 15, "limited": 0,
      "unmetered_responses": 0, "response_bytes": 1476,
      "quota_used": 3, "quota_reset": "2026-09-14T11:01:44Z" }
  ]
}
```

The names come from per-peer keys:

```sh
python -m bothy share -share-keys alice:key-a,bob:key-b -max-concurrent 4
```

Without them you get one row per caller address. That is still enough to see
that *someone* is pinning the GPU — just not who.

### Sharing should not mean giving your GPU away

Six knobs, all optional, and the first is on by default. They are what a public
host has instead of a bouncer: identity does not bound what a stranger can cost
you, limits do.

- **`BOTHY_OWNER_RESERVE`** (default **1**) keeps that many of
  `BOTHY_MAX_CONCURRENT` out of peers' reach, so your own request never queues
  behind strangers. Bothy's own traffic never passes through the host and no
  portable engine API reports whether an engine is busy, so this is not a reading
  of what you are doing — it is a guarantee of headroom, which is the only honest
  shape available for it.
- **`BOTHY_MAX_CONCURRENT`** (default 4) caps requests in flight across the whole
  host. A GPU serialises work anyway, so this is the limit that actually protects
  it. Over it, the host answers `429` with a `Retry-After`.
- **`BOTHY_PEER_MAX_CONCURRENT`** (default 0, off) caps how many of those slots
  one caller may hold at once. Without it the cap is first-come-first-served: one
  client with parallel requests takes the whole GPU and everybody else is told the
  host is full. The refusal says which it is — "the host is full" is a reason to
  try elsewhere, "you already have your share" is a reason to wait.
- **`BOTHY_MAX_REQUEST_TIME`** (default 0, off, e.g. `10m`) stops a single request
  at a wall-clock limit, and it is the only lever that bounds how long one
  generation can occupy the GPU. The caller gets a `504` naming the limit; a reply
  already streaming is simply cut off, because the headers are long gone.
- **`BOTHY_MAX_BODY`** (default **32 MiB**) refuses a request body larger than
  this with a `413`, before the engine is asked. Generous on purpose: a prompt
  carrying images is legitimately megabytes.
- **`BOTHY_PEER_QUOTA`** (default off) is a sustained budget, written as
  `count/period` — `200/1h` is 200 requests, and then wait for the window. This is
  what stops one person using your GPU all day: a rate limit only slows them down,
  because 30 a minute is still 43,200 a day.
- **`BOTHY_MAX_REQUESTS_PER_MINUTE`** (default 0, off) caps one peer's request
  rate, with a burst of the same size.

And the routes: **a host proxies inference and nothing else.** `POST /api/pull`,
`DELETE /api/delete` and the rest of the engine's control API answer `404` and
never reach the engine, because they are not inference and a stranger with a port
number should not be able to delete your models. `-allow-routes` opens specific
paths for an engine Bothy does not know; `-allow-all-routes` restores the old
proxy-everything behaviour for a network you control.

The host prints every one of these at startup, and they are all in
`/bothy/healthz` and `/bothy/usage`, so "what did I actually configure?" has an
answer that is not shell history.

A budget counts **requests, not tokens**, and that is a limitation rather than a
preference. Tokens are known only after a response has been produced, so a token
budget could only ever be enforced after the fact — and an engine that reports no
usage, which is allowed, would evade it entirely. Requests are counted before the
work starts, so a request budget always binds. The tokens are still in the usage
report for whoever is judging by them.

Free slots are advertised in the registry as each host's `free`, which is how a
client routes to whichever host is least busy rather than to whichever
registered first. That figure is free *peer* slots, so a host whose only free slot
is the reserve looks full to everybody else — and a client needs to understand
none of it. A host that reported no number at all is not treated as full: it is
unknown, and unknown sorts after known rather than being excluded.

### "Not right now"

Stopping the host works, but it also drops it out of the registry and leaves
connected clients with a connection error rather than an answer. Set an admin key
and you can pause instead:

```sh
python -m bothy share -admin-key a-secret

curl -X POST http://localhost:7777/bothy/sharing \
  -H 'X-Bothy-Key: a-secret' -d '{"paused": true}'
```

Peers get a `503` that says what happened, the host stops announcing itself so
clients route elsewhere, and your own engine is untouched throughout. One boolean
in one POST means a sharing schedule is two cron entries — pause at 9am, resume at
6pm. Without `-admin-key` there is no control endpoint at all, and it is
deliberately not a share key: peers hold those, and a peer who can stop your host
is worse than no control.

Three honest caveats:

- The counts come from the **engine's own report**, and an engine that reports
  nothing is recorded as `unmetered_responses` rather than guessed at. A host
  cannot produce a token count it was never given — which is exactly why the
  meter is trustworthy only as far as the host is.
- **Streamed replies are counted, and that takes work.** An OpenAI-compatible
  engine reports usage on a stream only when the request asks for it, so the host
  adds `stream_options.include_usage` on the way to the engine — the one request
  body Bothy ever rewrites, and only on the two streaming OpenAI routes. A caller
  that has already expressed a preference is never overridden, and
  `BOTHY_STREAM_USAGE=false` turns the whole thing off. Two cases still land as
  unmetered: a request body over 1 MiB, which is forwarded rather than read, and
  an engine that ignores the ask.
- Limits apply to the **inference path only**. `/bothy/models` and
  `/bothy/usage` are metadata, cost no GPU time, and are deliberately not
  throttled.
- The client's local endpoint accepts **any** API key, because it only listens on
  loopback. The share key is what reaches the host.

Billing would be built on exactly this counter. You cannot charge for something
you do not count, which is why the meter came before any payment discussion.

## Discovery: how a client finds a host

Discovery means one thing: **how does the client learn which host to talk to?**
Everything else is detail.

It gets confusing because it is three problems wearing one name:

1. **Address exchange** — getting `host:port` to the client. Easy. It's a phone
   book: a file, a database, an API.
2. **Reachability** — *can the client actually connect?* Hard, and not a data
   problem at all. Most volunteer GPUs sit behind a home router with no open
   port and a changing IP, so the phone book can list an address nobody can dial.
   A registry does not fix this. Nothing in this repo fixes it yet.
3. **Trust** — finding *an* address is not the same as being allowed to use it, or
   knowing it is the machine you think it is. Hence share keys and digests.

The registry in this repo solves (1) only. It is a bulletin board, not an
authority: it cannot check that an address is up, and it cannot check that a
digest is honest.

Open it in a browser and you get the bulletin board itself — a read-only page at
`/` listing what is live, who is serving it and how long ago they checked in:

```
bothy registry
3 live entries · ttl 1m0s · registration is open

MODEL             HOST     ADDRESS            FREE  DIGEST            LAST SEEN
llama3.1:8b       box      box.example:7777      3  sha256:11111111…  4s ago
qwen2.5:7b        hydra    hydra.lan:7777        0  sha256:22222222…  19s ago
```

It is a view of `/models`, not a second contract: no state, no query, no
per-client logging. Anything that needs to depend on the data reads the JSON.

**The ladder**, cheapest first — Bothy sits at rung 2, and rung 0 is fine for a
long time:

| | Approach | Cost |
| --- | --- | --- |
| 0 | Paste the address and key to each other | nothing |
| 1 | A static JSON list at a URL, edited by hand | nothing |
| 2 | A registry with heartbeats ← **this repo** | a small service |
| 3 | DHT or gossip, signed lists | a lot |

Registration doubles as the heartbeat: a host re-`POST`s every
`BOTHY_HEARTBEAT` (default 20s) and entries expire after `BOTHY_REGISTRY_TTL`
(default 60s), so machines that went to sleep fall out of the list instead of
being handed to clients forever. `BOTHY_REGISTRY_TOKEN` gates registration;
without it, anyone reachable can publish entries.

### Running a registry both sides can reach

The registry is the one piece that has to be reachable by everybody: hosts
announce to it, clients ask it who has a model. It is also the cheapest piece to
host, because it holds nothing on disk — registrations live in memory and expire
after `BOTHY_REGISTRY_TTL`, so a restart costs one heartbeat (20 seconds by
default) and it is as good as new. Anywhere that runs a small container or
process will do: a single Fly.io machine, Render's free web service, Cloud Run
with `--max-instances=1`, an always-free VM, or the machine next to your GPU.

Three things decide whether it works:

- **One instance, never a fleet.** The store is in memory, so two copies would
  each hold whichever hosts registered against them and a client's lookup would
  miss the rest. Do not put it behind a round-robin load balancer, and do not let
  it autoscale.
- **A token, if it is on the internet.** Registration is open unless
  `BOTHY_REGISTRY_TOKEN` is set, and an open registry lets anyone publish entries
  that clients will then dial. Set the token and give it to your hosts with
  `-register-token`.
- **`$PORT` is honoured.** A platform that tells a process which port its traffic
  arrives on gets what it asked for: an explicit `BOTHY_LISTEN` beats `$PORT`,
  and `$PORT` beats the command's own default.

```sh
docker run -p 8080:8080 -e BOTHY_REGISTRY_TOKEN=a-secret bothy discovery
```

The harder half is the hosts, not the registry. A GPU behind a home router has to
be dialled *by peers*, so `BOTHY_PUBLIC_ADDRESS` must be an address that resolves
for them — a tunnel (Cloudflare Tunnel, Tailscale Funnel) or a port forward. An
entry in the registry is only a phone number; Bothy cannot make the phone ring.

## Running without Docker

```sh
# nothing to build: run it from the checkout
python -m bothy run     -share-key secret -discovery-url http://localhost:8080

# or the halves separately
python -m bothy discovery -listen :8080
python -m bothy share  -engine-url http://localhost:11434 -share-key secret \
             -discovery-url http://localhost:8080 -address mybox.example:7777
python -m bothy connect -discovery-url http://localhost:8080 -model llama3.1:8b -share-key secret
```

Every flag has a `BOTHY_*` environment default, which is how the containers are
configured. Run any command with `-h`. The important ones:

| Variable | Role | Meaning |
| --- | --- | --- |
| `BOTHY_CLIENT_LISTEN` | run | where to borrow: `127.0.0.1:11223` |
| `BOTHY_HOST_LISTEN` | run | where to share: `:7777` |
| `BOTHY_SERVE` | run | share a local engine when one is found (default true) |
| `BOTHY_LISTEN` | all | address to listen on; on `run` it means the host's |
| `BOTHY_ENGINE_URL` | host | your engine's base URL |
| `BOTHY_ENGINE_KIND` | host | `auto`, `ollama`, `openai`, `mock`, `static` |
| `BOTHY_PUBLIC_ADDRESS` | host | the address to advertise — what peers dial, not what you listen on |
| `BOTHY_SHARE_KEY` | both | the group secret: your host requires it, your client presents it |
| `BOTHY_DISCOVERY_URL` | both | registry to announce to / look up in |
| `BOTHY_SHARE_KEYS` | host | per-peer keys, `alice:key,bob:key`; wins over `BOTHY_SHARE_KEY` |
| `BOTHY_MAX_CONCURRENT` | host | requests served at once (default 4; 0 = no cap) |
| `BOTHY_OWNER_RESERVE` | host | of that cap, slots peers may not use (default 1) |
| `BOTHY_PEER_MAX_CONCURRENT` | host | slots one caller may hold at once (default 0 = no separate cap) |
| `BOTHY_MAX_REQUEST_TIME` | host | wall-clock limit for one request, e.g. `10m` (default 0 = no limit) |
| `BOTHY_MAX_BODY` | host | largest request body in bytes (default 32 MiB; 0 = no cap) |
| `BOTHY_ALLOW_ROUTES` | host | extra engine paths to proxy, e.g. `POST /api/pull` |
| `BOTHY_ALLOW_ALL_ROUTES` | host | proxy every engine path, control routes included (default false) |
| `BOTHY_PEER_QUOTA` | host | per-peer request budget as `count/period`, e.g. `200/1h` |
| `BOTHY_MAX_REQUESTS_PER_MINUTE` | host | per-peer request rate (default 0 = no cap) |
| `BOTHY_ADMIN_KEY` | host | key for `POST /bothy/sharing`; unset means no control surface |
| `BOTHY_MODELS_DIR` | host | Ollama models dir, for real weights digests |
| `BOTHY_MODEL` / `BOTHY_EXPECTED_DIGEST` | client | what to use, and what to insist on |
| `BOTHY_HOST` | client | skip discovery and connect straight to an address |

`python -m bothy run` reads `BOTHY_HOST_LISTEN` for the sharing half and
`BOTHY_CLIENT_LISTEN` for the borrowing one, because one process running both
cannot use one `BOTHY_LISTEN` for two ports. A plain `BOTHY_LISTEN` still means
the host's, as it does for `python -m bothy share`.

### Where settings live

A flag is for the thing you are trying once. A config file is for the machine's
standing policy — and on a host that shares a GPU for months, the limits are
exactly the part worth writing down and reading back later.

```sh
python -m bothy config path      # which file is in use, and whether it exists
python -m bothy config init      # write a commented one to uncomment from
python -m bothy config init -force
python -m bothy config path -config ./bothy.conf   # or point somewhere else
```

```ini
# %AppData%\bothy\config — or ~/.config/bothy/config, Application Support on macOS
BOTHY_ENGINE_URL = http://127.0.0.1:11434
BOTHY_LISTEN = 127.0.0.1:7777
BOTHY_PEER_MAX_CONCURRENT = 1     # one caller cannot take the whole GPU
BOTHY_PEER_QUOTA = 200/1h         # and cannot use it all day
BOTHY_MAX_REQUEST_TIME = 10m      # one generation, bounded
```

Keys are the environment variable names, so a setting has exactly one name
everywhere it appears — in the file, the environment, `-h`, and this table.
Precedence is **flag, environment, file, built-in default**, which is what lets a
file hold the machine's policy while a container or a one-off command overrides
part of it. `-config <path>` (or `BOTHY_CONFIG`) points at a different file.

A file that exists is held to a standard, because the alternative is a typo that
silently configures nothing. An unparseable line is an error naming the line
number, and an unknown setting is an error naming it and suggesting what you
meant:

```
bothy: ~/.config/bothy/config line 3: unknown setting "BOTHY_MAX_CONCURENT",
did you mean BOTHY_MAX_CONCURRENT?
```

On Unix the file is created `0600`, since it may hold a share key.

## Writing your own host or client

Bothy is HTTP/JSON, and [`PROTOCOL.md`](PROTOCOL.md) is the contract. The
implementation in this repository is *one* implementation, not the definition —
nothing outside this file depends on it, and any language that speaks the protocol
interoperates with it.

[`examples/python/bothy_client.py`](examples/python/bothy_client.py) is a
working client that speaks it — standard library only, no `pip install`:

```sh
python3 examples/python/bothy_client.py registry --registry http://localhost:8080
python3 examples/python/bothy_client.py models --host box.example:7777 --key secret
python3 examples/python/bothy_client.py chat   --host box.example:7777 --key secret \
    --model llama3.1:8b --prompt "who are you?" --stream
```

## What this gives you, and what it doesn't

Provides:

- A remote GPU that looks local to any OpenAI- or Ollama-compatible client.
- Model identity you can check before sending work.
- A share key, so your GPU isn't open to whoever finds the port.

Does not provide:

- **Anonymity.** Both sides see each other's IP.
- **Prompt privacy from the host.** They run your prompt and can read it.
- **Proof the claimed model was served.** See the digest caveat above.
- **A trustworthy meter on someone else's machine.** The counts come from the
  host. Harmless while the network is free; the central problem the moment anyone
  pays.
- **Reachability.** A host without an open port or a tunnel cannot be reached,
  no matter what the registry says.
- **Model parallelism.** Each host holds whole models; Bothy routes, it does not
  split one model across machines.

## Roadmap

1. ~~One host, one client, one model — working end to end.~~ *(done)*
2. ~~Digest matching enforced on both sides, with the mismatch path tested.~~ *(done)*
3. ~~Metering, with concurrency caps and per-peer rate limits.~~ *(done)*
4. Multiple models per host, surfaced through `/v1/models`.
5. ~~Multiple hosts per client, routed by model and then by load.~~ *(done — a
   refusal moves the request to the next host, and hosts that reported free slots
   are asked first)*
6. Reachability: a relay or tunnel so home GPUs can be used without port
   forwarding. This is the hard half of discovery.
7. **Open question: paid hosts.** The meter is the foundation, but payment needs
   the honesty problem solved first — a paying host has a motive to lie about
   which model ran and how many tokens it produced, and the meter is on their
   machine. Any design also has to decide whether Bothy takes a cut. It should
   not: a commission is precisely what turns open-source software into a
   regulated money transmitter.
8. Later: reputation, Tor transport, incentives.

One idea is parked rather than disproved. **Farming problems rather than prompts**
— fanning a checkable task out across borrowed GPUs — was built here once and then
retired, because it never shipped and a build tag has no Python equivalent. The
part worth keeping is the filter it turned on: only farm work whose answer can be
checked more cheaply, and more trustworthily, than it can be produced. Its design
note is `docs/swarm.md` in git history.

## Open decisions

- Whether one request should be spread across several hosts, and how a burst
  arriving at once avoids filling one host while the fleet has room.
- Transport between host and client: plain HTTP on a private network today;
  TLS with a pinned certificate before this is exposed to the open internet.
- Where the share key lives and how it rotates.
- What `free` really measures — it is free peer slots, so it says nothing about
  speed, and it is the host's own claim.
- Whether the registry should sign entries, so clients can detect tampering.

## Non-goals, for now

No anonymity layer, no payments or incentives, no model parallelism, no public
directory of strangers' GPUs.

## Development

```sh
git clone https://github.com/hibbault/bothy
cd bothy
make check      # byte-compile every module, then the tests
make e2e        # the whole stack over real HTTP: four processes, no GPU, no Docker
make devnet     # the same stack in containers
```

Python 3.9 or newer. There are no dependencies to install, so there is no
virtualenv to make and no lockfile to update. Docker is optional, and only for the
devnet. See [CONTRIBUTING.md](CONTRIBUTING.md) for the layout, the ground rules,
and how to extend it.

## License

[Apache-2.0](LICENSE). Use it, modify it, run it commercially, ship it — the
patent grant is explicit, and attribution is the only condition. Contributions
come in under the same license, with no CLA.
