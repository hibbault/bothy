# Bothy

[![ci](https://github.com/hibbault/bothy/actions/workflows/ci.yml/badge.svg)](https://github.com/hibbault/bothy/actions/workflows/ci.yml)
[![go](https://img.shields.io/github/go-mod/go-version/hibbault/bothy)](go.mod)
[![license](https://img.shields.io/github/license/hibbault/bothy)](LICENSE)

**Share a GPU, borrow a GPU.**

A *bothy* is a small unlocked shelter in the Scottish hills. Nobody owns it,
nobody staffs it, nobody pays to sleep in it — volunteers keep it standing so
that whoever needs it can use it. That is the model here: people with spare GPU
capacity make it available to people who have none, for free, and Bothy handles
everything in between.

You have a GPU and a model running in Ollama (or llama.cpp, or vLLM). A friend
doesn't. They install Bothy, point their existing OpenAI client at `localhost`,
and their tokens run on your GPU. No weights move. No model download on their
side.

## Documentation

| | |
| --- | --- |
| [PROTOCOL.md](PROTOCOL.md) | The wire contract. Everything speaks this, so anything can implement it |
| [docs/design.md](docs/design.md) | Why it is shaped this way, and what is still open |
| [CONTRIBUTING.md](CONTRIBUTING.md) | Setup, layout, and the ground rules |
| [SECURITY.md](SECURITY.md) | The threat model, and how to report a problem |
| [CHANGELOG.md](CHANGELOG.md) | What landed in each release |

## Install

No account, no installer, no runtime to set up. Pick whichever fits:

```sh
# a static binary — linux/amd64 shown; arm64, macOS and Windows are built too
curl -fsSL https://github.com/hibbault/bothy/releases/latest/download/bothy-linux-amd64 -o bothy
chmod +x bothy

# or, with a Go toolchain
go install github.com/hibbault/bothy/cmd/bothy@latest

# or, the container to run beside an existing Ollama
docker build -t bothy .
docker run --network host -e BOTHY_ENGINE_URL=http://localhost:11434 \
  -e BOTHY_SHARE_KEY=a-secret bothy share
```

Sharing a GPU:

```sh
bothy share -engine-url http://localhost:11434 -share-key a-secret
```

Borrowing one:

```sh
bothy connect -discovery-url http://<registry>:8080 -model llama3.1:8b -share-key a-secret
```

`bothy version` says which release you are running, and every release publishes
`SHA256SUMS` alongside the binaries.

## The idea in one picture

```
        Client machine (no GPU)                    Host machine (GPU)

  your OpenAI client                          inference engine
        │                                     (Ollama / llama.cpp / vLLM)
        ▼                                              ▲
  127.0.0.1:11434 ──── network ───────►  :7777 ────────┘
   bothy connect                      bothy share
```

Two roles, one binary:

- **Host** (`bothy share`) runs next to your engine and *proxies* to it. It does
  not touch your models and never downloads anything. It announces what you can
  serve, requires a share key, meters who uses what, and caps how much of your
  GPU any one peer can occupy.
- **Client** (`bothy connect`) opens a **local, OpenAI-compatible endpoint**.
  Point anything at it and the tokens run elsewhere:

  ```sh
  curl http://127.0.0.1:11434/v1/chat/completions \
    -H 'content-type: application/json' \
    -d '{"model":"llama3.1:8b","messages":[{"role":"user","content":"hi"}]}'
  ```

The default port is **11434 — the port Ollama already uses**. A machine with no
GPU quietly *becomes* an Ollama as far as every editor, extension and CLI is
concerned. That is the whole pitch, in one default.

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
client       :11434  borrows it; published on 127.0.0.1:11434 for you
```

Then talk to somebody else's GPU:

```sh
curl http://127.0.0.1:11434/v1/chat/completions \
  -H 'content-type: application/json' \
  -d '{"model":"llama3.1:8b","messages":[{"role":"user","content":"who answered this?"}]}'
```

The reply names the engine and the digest that produced it, so you can always
tell whose GPU actually answered. Ask the client where it ended up:

```sh
curl http://127.0.0.1:11434/bothy/status
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
{ "in_flight": 1, "capacity": 3, "max_concurrent": 4,
  "peers": [
    { "peer": "alice", "requests": 3, "prompt_tokens": 6,
      "completion_tokens": 15, "limited": 0,
      "unmetered_responses": 0, "response_bytes": 1476 }
  ] }
```

The names come from per-peer keys:

```sh
bothy share -share-keys alice:key-a,bob:key-b -max-concurrent 4
```

Without them you get one row per caller address. That is still enough to see
that *someone* is pinning the GPU — just not who.

Two limits, both optional:

- **`BOTHY_MAX_CONCURRENT`** (default 4) caps requests in flight across the whole
  host. A GPU serialises work anyway, so this is the limit that actually protects
  it. Over it, the host answers `429` with a `Retry-After`.
- **`BOTHY_MAX_REQUESTS_PER_MINUTE`** (default 0, off) caps one peer's request
  rate, with a burst of the same size.

Free slots are also advertised in the registry as each host's `capacity`, which
is how a client can route to whichever host is least busy rather than to
whichever registered first.

Three honest caveats:

- The counts come from the **engine's own report**, and an engine that reports
  nothing is recorded as `unmetered_responses` rather than guessed at. A host
  cannot produce a token count it was never given — which is exactly why the
  meter is trustworthy only as far as the host is. Streaming is the case to
  watch: Ollama reports usage in the closing frame, but an OpenAI-compatible
  engine generally does so only when the client asks for it
  (`stream_options.include_usage`), so a streamed reply can land as unmetered
  depending on what is behind the host.
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

## Running without Docker

```sh
go build ./cmd/bothy

bothy discovery -listen :8080
bothy share  -engine-url http://localhost:11434 -share-key secret \
             -discovery-url http://localhost:8080 -address mybox.example:7777
bothy connect -discovery-url http://localhost:8080 -model llama3.1:8b -share-key secret
```

Every flag has a `BOTHY_*` environment default, which is how the containers are
configured. Run any command with `-h`. The important ones:

| Variable | Role | Meaning |
| --- | --- | --- |
| `BOTHY_LISTEN` | all | address to listen on |
| `BOTHY_ENGINE_URL` | host | your engine's base URL |
| `BOTHY_ENGINE_KIND` | host | `auto`, `ollama`, `openai`, `mock`, `static` |
| `BOTHY_PUBLIC_ADDRESS` | host | the address to advertise — what peers dial, not what you listen on |
| `BOTHY_SHARE_KEY` | both | key peers must present |
| `BOTHY_DISCOVERY_URL` | both | registry to announce to / look up in |
| `BOTHY_SHARE_KEYS` | host | per-peer keys, `alice:key,bob:key`; wins over `BOTHY_SHARE_KEY` |
| `BOTHY_MAX_CONCURRENT` | host | requests served at once (default 4; 0 = no cap) |
| `BOTHY_MAX_REQUESTS_PER_MINUTE` | host | per-peer request rate (default 0 = no cap) |
| `BOTHY_MODELS_DIR` | host | Ollama models dir, for real weights digests |
| `BOTHY_MODEL` / `BOTHY_EXPECTED_DIGEST` | client | what to use, and what to insist on |
| `BOTHY_HOST` | client | skip discovery and connect straight to an address |

## Writing your own host or client

Bothy is HTTP/JSON, and [`PROTOCOL.md`](PROTOCOL.md) is the contract. The Go
implementation in this repo is *one* implementation, not the definition, so a
Python host and a Go client interoperate fine.

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
5. Multiple hosts per client, routed by model and then by load.
6. Reachability: a relay or tunnel so home GPUs can be used without port
   forwarding. This is the hard half of discovery.
7. **Open question: paid hosts.** The meter is the foundation, but payment needs
   the honesty problem solved first — a paying host has a motive to lie about
   which model ran and how many tokens it produced, and the meter is on their
   machine. Any design also has to decide whether Bothy takes a cut. It should
   not: a commission is precisely what turns open-source software into a
   regulated money transmitter.
8. Later: reputation, Tor transport, incentives.

## Open decisions

- Whether the client's local endpoint fronts one host or several in v1.
- Transport between host and client: plain HTTP on a private network today;
  TLS with a pinned certificate before this is exposed to the open internet.
- Where the share key lives and how it rotates.
- What "capacity" means, so the registry can route by load rather than arrival.
- Whether the registry should sign entries, so clients can detect tampering.

## Non-goals, for now

No anonymity layer, no payments or incentives, no model parallelism, no public
directory of strangers' GPUs.

## Development

```sh
git clone https://github.com/hibbault/bothy
cd bothy
make check      # gofmt, vet, and the tests
make devnet     # the whole stack in containers, no GPU required
```

Go 1.22 or newer. Docker is optional and only needed for the devnet. See
[CONTRIBUTING.md](CONTRIBUTING.md) for the layout, the ground rules, and how to
extend it.

## License

[Apache-2.0](LICENSE). Use it, modify it, run it commercially, ship it — the
patent grant is explicit, and attribution is the only condition. Contributions
come in under the same license, with no CLA.
