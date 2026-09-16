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
make check          # byte-compile every module, then the tests
make e2e            # the whole stack over real HTTP: no GPU, no Docker
make devnet         # the same thing in containers
```

Requires Python 3.9 or newer, and nothing else: there are no dependencies, so
there is no virtualenv to create and no lockfile to keep. Docker is optional, and
only for the devnet.

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
| `bothy/cli.py` | The command line. One command per role |
| `bothy/app.py` | `run`: both roles in one process, chosen once at startup |
| `bothy/host.py` | `share`: announces models, meters, proxies to the engine |
| `bothy/client.py` | `connect`: resolves a host, verifies digests, serves locally |
| `bothy/discovery.py` | The registry service |
| `bothy/registry.py` | The entry type, the TTL store, and the registry client |
| `bothy/engine.py` | Adapters that describe an engine's models and digests |
| `bothy/meter.py` | Usage counting, the response sniffer, limits |
| `bothy/mockengine.py` | A fake engine, so the stack runs with no GPU |
| `bothy/digest.py` | Hashing weights files, with a cache |
| `bothy/httpx.py` | Shared HTTP helpers: routing, auth, streaming, serving |
| `bothy/config.py`, `model.py`, `errors.py` | Settings, the shared vocabulary, the error base |
| `scripts/e2e.sh` | The whole stack over real ports — the same script CI runs |
| `examples/python` | A second implementation of the protocol, in another process |

`bothy/` is not a public API. It is an application rather than a library: the
modules are split along the seams this design needed, not along lines anyone
promised to keep stable, and importing them from outside the project is not
supported.

## Ground rules

**Standard library only, on purpose.** Bothy has zero dependencies. That is what
keeps "install this next to your Ollama" to copying a directory and having an
interpreter on the machine, and it is the reason the project is pleasant to work
on anywhere. A new dependency needs to be justified in an issue first, and the bar
is high.

**Tests are for behaviour, especially failure paths.** The interesting tests in
this repo are the ones that assert a *refusal*: digest mismatches, a peer over
its limit, a stream that arrives split at an awkward byte boundary. A change that
alters what Bothy refuses should come with a test that says so.

**`make check` must pass.** It byte-compiles every module and runs the whole
suite. Nothing is installed, so nothing formats or lints for you: matching the
code around you is the formatter.

**Experiments live outside the product.** Anything speculative belongs behind its
own entry point rather than woven through the roles, so that dropping it leaves the
project whole. The task runner that used to live here was retired for exactly that
reason: nothing imported it, so removing it was a deletion rather than surgery.

**Small, focused changes.** One idea per pull request.

**Don't commit generated files.** `__pycache__` is ignored, and so is the
`.freebuff/` directory, which is personal agent scaffolding rather than part of
the project.

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

The seam is one interface, in `bothy/engine.py`:

```python
class Lister:
    """Reports the models an engine can serve, with a digest for each."""

    def list_models(self) -> List[Model]: ...
    def kind(self) -> str: ...
```

Implement it, add a case to `engine.new`, and document the probe in
[PROTOCOL.md](PROTOCOL.md#4-what-bothy-expects-of-an-engine). The engine's HTTP
API is not part of Bothy's protocol — the host adapts to the engine, never the
reverse — so an adapter only ever needs to read a model list.

If your engine cannot report a digest, that is fine and expected: return an empty
digest and let the operator pin one, or hash a weights file. Do not invent a
digest.

## Writing an implementation in another language

Bothy is HTTP/JSON, and [PROTOCOL.md](PROTOCOL.md) is the contract. The
implementation in this repository is one implementation, not the definition, so a
host written here and a client written anywhere else interoperate fine.

`examples/python/bothy_client.py` is a dependency-free client that speaks the
protocol, and is the model to follow: if your implementation disagrees with that
document, one of the two is a bug — please open an issue either way.

## Reporting

- Bugs and design disagreements: open an issue.
- Security problems: see [SECURITY.md](SECURITY.md). Please do not open a public
  issue for those.
- Labels live in [`.github/labels.txt`](.github/labels.txt) and a workflow applies
  them on push. If a label is missing, that file is where to add it — no
  repository admin rights required, which is why they are kept there.

## Commits and pull requests

Commit messages should explain *why*, in the imperative, and be readable on their
own. "Refuse a host whose weights differ, rather than warning" beats "fix digest
check".

Pull requests: say what changed, what you verified, and what you deliberately
left out. If you ran the devnet, say which failure paths you exercised.

## License

Contributions are accepted under the project's license — see
[LICENSE](LICENSE). There is no CLA and no copyright assignment.
