# Security

## Reporting a vulnerability

Please use GitHub's [private vulnerability
reporting](https://github.com/hibbault/bothy/security/advisories/new) rather than
opening a public issue. If that is unavailable for any reason, open a public issue
that says only that you have a security report and how to reach you — no details.

Bothy is pre-1.0 and maintained on a best-effort basis. There are no backports;
the fix will land on `main`.

## Supported versions

None yet. There has been no release, `main` is the only thing that exists, and
nothing here should be exposed to the open internet as-is.

## Threat model

Read this before exposing a host to anyone you do not know.

### What Bothy does protect

- **Strangers cannot use a keyed host.** A share key is required on every route
  that can spend GPU time. Compare-and-match is constant-time, and keys can be
  per-peer, so one can be revoked without disturbing the others.
- **A peer cannot take the whole GPU.** With `BOTHY_MAX_CONCURRENT` set, the host
  refuses work past its cap, and `BOTHY_MAX_REQUESTS_PER_MINUTE` bounds one peer's
  rate. Both are off-or-on by configuration, and the refusal is explicit.
- **Nobody is handed a dead host forever.** Entries expire without a heartbeat.
- **Weights can be checked before work is sent.** Given an expected digest, the
  client refuses a host serving something else.
- **Share keys never reach the engine.** They are stripped from requests before
  forwarding, so they do not land in the engine's logs.

### What Bothy does not protect

- **Prompts are plaintext to the host.** Whoever runs the GPU reads what you send.
  This is inherent: they have to run it.
- **Keys travel in a header over whatever transport you use.** The devnet is plain
  HTTP. Anyone who can read that traffic has your key. Use a private network or a
  TLS terminator in front of the host.
- **A digest is a claim, not proof.** A host can report any digest. Checking one
  catches honest mistakes — wrong quant, wrong tag, a stale pull — and does not
  stop a host that intends to lie.
- **The meter runs on the seller's machine.** Usage figures are as trustworthy as
  the host reporting them. Harmless while the network is free; the central problem
  if anyone ever pays.
- **The registry is unauthenticated by default.** Without
  `BOTHY_REGISTRY_TOKEN`, anyone reachable can publish entries. That is a spam
  problem rather than a security one, since the registry cannot spend anyone's
  GPU.
- **Registration is not verified.** A host can register models it cannot serve, or
  an address that is not reachable.
- **There is no anonymity.** Both parties see each other's IP.
- **The client's local endpoint is unauthenticated by default.** It listens on
  loopback only. If you change `BOTHY_LISTEN` to a public interface, set
  `BOTHY_LOCAL_API_KEY` — and be aware that anything on that machine can already
  use it, which is the point.

### If you run a host

- Always set `BOTHY_SHARE_KEY` or `BOTHY_SHARE_KEYS`. Without one, anyone who can
  reach the port spends your GPU. The process warns loudly about this on startup.
- Do not expose `:7777` to the open internet without TLS in front of it. Prefer a
  private network or VPN.
- Set `BOTHY_MAX_CONCURRENT`. A GPU serialises work anyway; the cap is what stops
  one peer from occupying it indefinitely.
- Set `BOTHY_REGISTRY_TOKEN` if your registry is reachable by anyone else.
- Read what your engine logs. Nothing in Bothy stops a peer asking for something
  you would rather not serve, and you are the one serving it.

### If you run a registry

- Treat it as public and dumb. It holds no secrets, and it deliberately does not
  hold share keys — those are exchanged out of band, per host.
- Entries are attacker-controlled strings. They are only ever echoed back as JSON,
  never interpreted, but do not build anything on top that trusts them.

## Known design limitations

These are documented rather than fixed, because fixing them is a design change:

1. Proving which weights ran is unsolved (see above).
2. Reachability is unsolved: a host behind a home router cannot be dialled unless
   the operator forwards a port or runs a tunnel. The registry will happily list
   an address that does not work.
3. Metering depends on the engine reporting usage. An engine that reports nothing
   is recorded as unmetered, which is honest and also useless for accounting. The
   host narrows this by asking for usage on streamed requests
   (`stream_options.include_usage`), but an engine that ignores the ask, or a
   request body over 1 MiB, stays uncounted.
