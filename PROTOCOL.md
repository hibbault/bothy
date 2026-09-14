# Bothy wire protocol

Bothy is three HTTP/JSON services, and this file is the contract between them.
The Go implementation in this repo is *one* implementation, not the definition —
anything that speaks this protocol interoperates, in any language.

Two useful things fall out of that. You can write a Python host and have Go
clients use it. And you can replace any one piece without touching the others.

```
client ──── discovery (registry) ──── host ──── inference engine
  │             look up who has             announce + proxy
  └──────────── the model, then talk ──────────────────►
                  to the host directly
```

## Conventions

- Bodies are JSON, UTF-8, with `Content-Type: application/json`.
- **Auth** is a key presented either as `X-Bothy-Key: <key>` or
  `Authorization: Bearer <key>`. `X-Bothy-Key` wins if both are present. Header
  values are trimmed, since leading/trailing whitespace is not part of a header
  value.
- **Errors** use an OpenAI-shaped body, so existing clients surface something
  useful instead of a blank failure:

  ```json
  { "error": { "message": "missing or invalid key", "type": "bothy_error" } }
  ```

- Status codes in use: `200`, `400` malformed request, `401` bad or missing key,
  `404`, `405`, `429` limited, `502` upstream unreachable, `503` the owner has
  paused sharing.
- Responses are pretty-printed JSON. Clients must not depend on the whitespace.

## 1. Discovery (registry)

Default listener: `:8080`. A bulletin board, not an authority — it cannot check
that an address is reachable or that a digest is honest.

### `POST /register`

Auth: required only if the registry was started with a token.

```json
{ "entries": [ { "model": "llama3.1:8b", "digest": "sha256:1111...",
                 "address": "box.example:7777", "host": "box", "capacity": 3 } ] }
```

Body is capped at 1 MiB. An empty `entries` array is a `400`.

Response `200`:

```json
{ "registered": 1, "live": 2, "ttl": "1m0s" }
```

Semantics that matter:

- **Registering *is* the heartbeat.** There is no separate liveness call. Re-POST
  at no more than a third of `ttl` (the Go host defaults to 20s against a 60s
  TTL). A host that stops registering expires and disappears from `/models`.
- Entries are keyed by `(model, address)`, so re-registering refreshes rather
  than duplicates.
- Entries missing `model` or `address` are silently dropped — not an error.
  `registered` tells you how many of the offered entries were accepted.

### `GET /models`

Optional `?model=`. **Requesting a bare name matches any tag** (`llama3.1` finds
`llama3.1:8b`), while a tagged request is exact. An empty request returns
everything live.

```json
{ "entries": [ { "model": "llama3.1:8b", "digest": "sha256:1111...",
                 "address": "box.example:7777", "host": "box",
                 "capacity": 3, "last_seen": "2026-09-14T10:01:44Z" } ] }
```

Ordered by `capacity` descending, then `host`, then `model`, so a client can take
the first entry that suits it. Expired entries are omitted.

### `GET /healthz`

```json
{ "ok": true, "live_entries": 2, "ttl": "1m0s" }
```

### The `Entry` object

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `model` | string | yes | Model name, e.g. `llama3.1:8b` |
| `digest` | string | no | SHA-256 of the weights. Empty means unknown |
| `address` | string | yes | Opaque dial target. `host:port` today |
| `host` | string | no | Human-readable identity for routing and logs |
| `capacity` | int | no | Free request slots for peers — slots kept for a host's owner are not counted, and are not offered. Absent or `0` means unknown |
| `last_seen` | RFC3339 | set by registry | Ignored on input |

## 2. Host

Default listener: `:7777`. Proxies to a local inference engine.

### `GET /bothy/healthz` — no auth

So a container healthcheck needs no key.

```json
{ "ok": true, "host": "box", "engine_kind": "ollama", "address": "box:7777",
  "model_count": 2, "discovery": "http://registry:8080", "key_required": true,
  "in_flight": 0, "capacity": 3, "max_concurrent": 4, "owner_reserve": 1,
  "peer_slots": 3, "requests_per_minute": 0, "peer_quota": "", "paused": false }
```

### `GET /bothy/models` — auth required

What this host serves, with digests. A client pointed straight at an address uses
this to learn what it can verify.

```json
{ "host": "box", "address": "box:7777", "capacity": 3,
  "models": [ { "name": "llama3.1:8b", "digest": "sha256:1111..." } ] }
```

### `GET /bothy/usage` — auth required

Who is using the GPU.

```json
{ "host": "box", "address": "box:7777",
  "in_flight": 1, "capacity": 3, "max_concurrent": 4, "owner_reserve": 1,
  "peer_slots": 3, "requests_per_minute": 0, "peer_quota": "200/1h",
  "paused": false,
  "peers": [ { "peer": "alice", "in_flight": 1, "requests": 3, "limited": 0,
               "prompt_tokens": 6, "completion_tokens": 15,
               "response_bytes": 1476, "unmetered_responses": 0,
               "last_seen": "2026-09-14T10:01:44Z",
               "quota_used": 3, "quota_reset": "2026-09-14T11:01:44Z" } ] }
```

`peer` is the name from the host's key list, or `addr:<ip>` when the host is open.

`quota_used` and `quota_reset` appear only when a per-peer budget is configured:
how much of the current window that peer has spent, and when it turns over. A
window starts at the peer's own first admitted request, so budgets do not all turn
over at once. Without these two fields, an owner watching a peer stop has no way
to tell a spent budget from a crash.

### `POST /bothy/sharing` — the admin key, when one is set

Pauses and resumes sharing, so that "not right now" does not have to mean stopping
the process. Stopping works, but it also drops the host from the registry and
leaves clients with a connection error instead of an answer.

The admin key is deliberately **not** a share key. Share keys are handed to peers,
and a peer who can stop your host is worse than no control surface at all. With no
admin key configured the route answers `404`: there is no control endpoint here,
and saying so is more use than implying one that refused you.

```sh
curl -X POST http://box:7777/bothy/sharing \
  -H 'X-Bothy-Key: <admin key>' \
  -d '{"paused": true}'
```

```json
{ "paused": true, "since": "2026-09-14T10:31:07Z" }
```

While paused:

- peer requests are refused with `503`, checked **before** the meter, because the
  refusal is not the peer's doing and must not spend their budget;
- the host **stops announcing itself**. There is no delete in the registry
  protocol, so the mechanism is to stop saying it and let the entry expire on the
  registry's own TTL. That is the point: clients route elsewhere rather than to a
  host that will refuse them;
- resuming announces again immediately, not at the next heartbeat.

A pause is one boolean in one POST, so a sharing schedule is two cron entries.
`-paused` starts a host paused, for the first of them.

### Everything else — auth required, proxied through

Any other path is forwarded to the engine, including `/v1/chat/completions`,
`/v1/models` and Ollama's `/api/*`. Responses stream through unbuffered, and the
presented key is stripped before forwarding so it never reaches the engine.

**One request body is ever rewritten.** On `POST /v1/chat/completions` and
`POST /v1/completions`, when the caller asked to stream and only then, the host
adds `"stream_options": {"include_usage": true}` before forwarding. An
OpenAI-compatible engine reports no token usage on a stream without being asked,
so a host whose peers all stream would otherwise meter nothing at all.

Three limits keep that a narrow exception rather than a licence to edit requests:

- A body that already carries a `stream_options` key is left exactly as it is. A
  caller's explicit choice is never overridden, including one that asks for less.
- A body over 1 MiB is forwarded untouched rather than read, so a prompt carrying
  images does not become a buffer. Such a reply is reported as
  `unmetered_responses`.
- `BOTHY_STREAM_USAGE=false` (or `-stream-usage=false`) turns it off entirely, for
  an engine that objects to the field.

### Refusals

- `401` — missing or wrong key.
- `429` — three reasons, and the body says which: over the host's **peer
  capacity**, over this peer's **rate**, or over this peer's **budget**. All three
  set `Retry-After` in seconds, because all three are a "come back later":

  ```json
  { "error": { "message": "peer \"alice\" has used its budget of 200 requests per 1h0m0s; retry in 1h0m0s",
               "type": "bothy_error" } }
  ```

- `503` — the owner has paused sharing. Unlike a `429` this is not something
  waiting will fix, so there is no `Retry-After`, and a client should try another
  host rather than retry this one.

**Peer capacity is not the whole cap.** `max_concurrent` includes the slots kept
back for the host's owner (`owner_reserve`, default 1), so a host with a cap of
four refuses peers after three, and reports `capacity` as free *peer* slots. A
reserved slot is never advertised, for the same reason it exists. A client needs
to know none of this: a host whose last free slot is the owner's looks full.

**A budget is not a rate.** A rate limit slows a peer down — 30 a minute permits
43,200 requests a day, for ever. A budget stops them: 200 an hour permits 200, and
then waits for the window to turn over. Budgets count **requests, not tokens**,
and that is a
limitation rather than a preference — tokens are known only after a response has
been produced, so a token budget can only ever be enforced retrospectively, and an
engine that reports no usage, which is allowed, would evade it entirely. Requests
are counted before the work starts, so a request budget always binds. The tokens
are still there in `/bothy/usage` for whoever is judging by them.

Limits apply to the **proxied inference path only**. `/bothy/models` and
`/bothy/usage` are metadata, cost no GPU time, and are deliberately not throttled.

## 3. Client

Default listener: `127.0.0.1:11434` — Ollama's own port, so existing tools need no
reconfiguration.

### `GET /bothy/status` — no auth

```json
{ "listening": "127.0.0.1:11434", "discovery": "http://registry:8080",
  "requested_model": "llama3.1:8b", "expected_digest": "",
  "connected": true, "host": "box:7777", "model": "llama3.1:8b",
  "digest": "sha256:1111...", "digest_verified": false }
```

`connected` is `false` until the first request triggers resolution. That is by
design: a host that is not up yet must not stop the client from starting.

### Everything else — proxied to the resolved host

The client resolves once (from discovery, or from its configured address), then
forwards. Any `Authorization` the local caller sent is replaced with the share
key. Re-resolution happens automatically after an upstream failure.

**Digest rule.** With no expected digest, any host is accepted. As soon as one is
configured, a host offering different weights is refused — at startup, and again
on every re-resolution. A host that advertises *no* digest never satisfies a
requirement; "unknown" must not read as "verified".

## 4. What Bothy expects of an engine

The engine is whatever is already running. Bothy only ever reads a model list
from it, and proxies everything else.

| `engine_kind` | Probe | Digest source |
| --- | --- | --- |
| `auto` (default) | `/internal/models`, then `/api/tags` | as below |
| `ollama` | `GET /api/tags` → `models[].name` | the manifest's `application/vnd.ollama.image.model` layer, when the models directory is readable |
| `openai` | `GET /v1/models` → `data[].id` | none; pair with a configured digest |
| `mock` | `GET /internal/models` → `models[].name` | reported by the engine |
| `static` | none | the configured list, or a hashed weights file |

A note on Ollama specifically: the `digest` in `/api/tags` is the **manifest**
digest, which changes whenever any layer does. It is not the weights hash, so
Bothy prefers the weights layer's digest from the manifest and only falls back to
the reported one.

## 5. How usage is counted

An engine reports usage in one of two shapes, and both are recognised:

```json
{ "usage": { "prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18 } }
{ "prompt_eval_count": 9, "eval_count": 4 }
```

In a stream, the numbers arrive in a later frame than the text, so the last
non-zero value wins. An engine that reports nothing is recorded as
`unmetered_responses`, never as zero — the host cannot invent a count it was
never given.

Because that is the common case rather than the rare one, the host asks for the
numbers: a streamed OpenAI request goes out with
`stream_options.include_usage`, as described under the host routes above. `total`
is filled in when an engine omits it and the other two are known.

## Compatibility

- **There is no protocol version field yet.** Fields are only ever added, never
  repurposed. The first breaking change should add one — most likely a
  `GET /bothy/version` route plus an `X-Bothy-Protocol` header.
- The stable surface is: the three routes above, the `Entry` and `PeerUsage`
  shapes, and the key headers. Which endpoints an *engine* exposes is not part of
  this protocol, since the host adapts to the engine rather than the reverse.
- The registry is expected to stay dumb. Anything that requires the registry to
  verify a claim is a change to this protocol, not an implementation detail.
