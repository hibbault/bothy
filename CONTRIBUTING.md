# Contributing

Bothy is early, and the useful contributions right now are as much about *ideas
being wrong* as about code. If something in [docs/design.md](docs/design.md)
looks mistaken, that is a valuable issue.

You do not need a GPU to work on this. The devnet ships a mock engine so the
whole system runs on a laptop.

## Getting started

```sh
git clone https://github.com/hibbault/bothy
cd bothy
make check          # gofmt, vet, tests
make devnet         # the whole thing in containers, no GPU needed
```

Requires Go 1.22 or newer. Docker is optional, and only for the devnet.

`make devnet`, `make real` and `make mismatch` set the compose profile
themselves, so a fresh clone needs no `.env` — see `.env.example` if you want one
anyway. Do not keep credentials in `.env` or anywhere else under the repository:
`.env` is gitignored, but a file outside the tree is the habit that survives a
mistake.

To use a real GPU instead of the mock:

```sh
make real
docker compose exec engine-ollama ollama pull llama3.1:8b
```

## Layout

| Path | What lives there |
| --- | --- |
| `cmd/bothy` | The binary. One command per role |
| `internal/host` | `share`: announces models, meters, proxies to the engine |
| `internal/client` | `connect`: resolves a host, verifies digests, serves locally |
| `internal/discovery` | The registry service |
| `internal/registry` | The entry type, the TTL store, and the registry client |
| `internal/engine` | Adapters that describe an engine's models and digests |
| `internal/meter` | Usage counting, the response sniffer, limits |
| `internal/mockengine` | A fake engine, so the stack runs with no GPU |
| `internal/digest` | Hashing weights files, with a cache |
| `internal/httpx` | Shared HTTP helpers |
| `examples/python` | A client in another language, proving the protocol is the contract |

`internal/` is not a public API. Nothing outside the module can import it, and
that is deliberate: none of these packages are promises yet.

## Ground rules

**Standard library only, on purpose.** Bothy has zero dependencies. That is what
keeps "install this next to your Ollama" to a single static binary, and it is the
reason the project is pleasant to build anywhere. A new dependency needs to be
justified in an issue first, and the bar is high.

**Tests are for behaviour, especially failure paths.** The interesting tests in
this repo are the ones that assert a *refusal*: digest mismatches, a peer over
its limit, a stream that arrives split at an awkward byte boundary. A change that
alters what Bothy refuses should come with a test that says so.

**`gofmt`, `go vet` and `go test ./...` must pass.** `make check` runs all three.

**Small, focused changes.** One idea per pull request.

**Don't commit generated files.** `/bin` is ignored, and so is the `.freebuff/`
directory, which is personal agent scaffolding rather than part of the project.

## Trying the failure paths

The devnet exists so the interesting failures are reproducible. These are the
ones worth exercising before and after a change:

| Command | What it should prove |
| --- | --- |
| `docker compose stop host` | the client stops being handed a dead host, after the registry TTL |
| `make mismatch` | a second host serves the same model name with **different weights**, and the client refuses it |
| `docker compose stop discovery` | connected clients keep working; only new lookups fail |
| `docker compose --scale client=5 up` | concurrent clients hit the cap and get `429`s, not a pinned GPU |

The mismatch run is the important one, and the only way to test digest
verification without downloading the same model twice.

## Adding support for an engine

The seam is one interface, in `internal/engine`:

```go
type Lister interface {
    ListModels(ctx context.Context) ([]model.Model, error)
    Kind() string
}
```

Implement it, add a case to `New`, and document the probe in
[PROTOCOL.md](PROTOCOL.md#4-what-bothy-expects-of-an-engine). The engine's HTTP
API is not part of Bothy's protocol — the host adapts to the engine, never the
reverse — so an adapter only ever needs to read a model list.

If your engine cannot report a digest, that is fine and expected: return an empty
digest and let the operator pin one, or hash a weights file. Do not invent a
digest.

## Writing an implementation in another language

Bothy is HTTP/JSON, and [PROTOCOL.md](PROTOCOL.md) is the contract. The Go
implementation is one implementation, not the definition, so a Python host and a
Go client interoperate fine.

`examples/python/bothy_client.py` is a dependency-free client that speaks the
protocol, and is the model to follow: if your implementation disagrees with that
document, one of the two is a bug — please open an issue either way.

## Reporting

- Bugs and design disagreements: open an issue.
- Security problems: see [SECURITY.md](SECURITY.md). Please do not open a public
  issue for those.

## Commits and pull requests

Commit messages should explain *why*, in the imperative, and be readable on their
own. "Refuse a host whose weights differ, rather than warning" beats "fix digest
check".

Pull requests: say what changed, what you verified, and what you deliberately
left out. If you ran the devnet, say which failure paths you exercised.

## License

Contributions are accepted under the project's license — see
[LICENSE](LICENSE). There is no CLA and no copyright assignment.
