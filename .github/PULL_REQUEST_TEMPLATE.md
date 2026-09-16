## What this changes

<!-- One or two sentences. Why, not just what. -->

## What you verified

<!--
"Tests pass" is a starting point, not an answer. Say which failure paths you
exercised, and how — the refusal paths are the interesting ones:

  make check
  make mismatch        # the client refuses a host with different weights
  docker compose stop host    # entries expire, clients stop being handed a corpse
-->

## What you deliberately left out

<!--
Scope you chose not to take on, and why. Also anything you are unsure about —
an honest "I could not verify the container build" is more useful than silence.
-->

## Checklist

- [ ] `make check` passes (it byte-compiles every module, then runs the tests)
- [ ] Behaviour changes come with a test — especially changes to what Bothy **refuses**
- [ ] No new dependencies (or a linked issue where one was agreed)
- [ ] `PROTOCOL.md` and `docs/design.md` still describe what the code does
- [ ] `CHANGELOG.md` updated if this is user-visible
- [ ] No credentials, tokens or `.env` files in the diff
