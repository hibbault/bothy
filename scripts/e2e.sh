#!/bin/sh
# The whole stack over real HTTP, the way the CI job and a laptop both run it.
#
# It is a script rather than a block in the workflow file because a claim that is
# only ever tested in CI is a claim nobody can check while working on it. Every
# service here is an ordinary process on a loopback port, so this needs nothing
# installed but Python and curl -- no GPU, no Docker.
#
# Run it with `make e2e` from the repository root.
set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BOTHY="python -m bothy"
cd "$ROOT"

# Ports of its own, so this never collides with a development stack already
# running on the defaults (7777, 8080, 11223).
MOCK=19101
REGISTRY=19102
HOST=19103
CLIENT=19104

pids=""
cleanup() {
	for pid in $pids; do
		kill "$pid" 2>/dev/null || true
	done
}
trap cleanup EXIT INT TERM

# Each service gets its own log, so a failure below can be read in full rather
# than guessed at from a truncated pipe.
logdir="$(mktemp -d)"
echo "logs in $logdir"

$BOTHY mock      -listen :$MOCK -name e2e           > "$logdir/mock.log"     2>&1 &
pids="$pids $!"
$BOTHY discovery -listen :$REGISTRY                 > "$logdir/registry.log" 2>&1 &
pids="$pids $!"

# The mock engine answers in about a tenth of a second; the registry binds a
# socket. Waiting for what is needed beats sleeping and hoping.
wait_for() {
	url="$1"
	label="$2"
	for _ in $(seq 1 100); do
		if curl -fsS "$url" > /dev/null 2>&1; then
			return 0
		fi
		sleep 0.1
	done
	echo "the $label never came up ($url)"
	return 1
}

wait_for "http://127.0.0.1:$MOCK/api/tags" "mock engine"
wait_for "http://127.0.0.1:$REGISTRY/healthz" "registry"

BOTHY_SHARE_KEY=ci-key $BOTHY share -listen :$HOST \
	-engine-url http://127.0.0.1:$MOCK \
	-engine-kind mock \
	-discovery-url http://127.0.0.1:$REGISTRY \
	-share-key ci-key -admin-key ci-admin \
	-address 127.0.0.1:$HOST                       > "$logdir/host.log"     2>&1 &
pids="$pids $!"

BOTHY_SHARE_KEY=ci-key $BOTHY connect -listen 127.0.0.1:$CLIENT \
	-discovery-url http://127.0.0.1:$REGISTRY \
	-model llama3.1:8b -share-key ci-key           > "$logdir/client.log"   2>&1 &
pids="$pids $!"

wait_for "http://127.0.0.1:$HOST/bothy/healthz" "host"
wait_for "http://127.0.0.1:$CLIENT/bothy/status" "client"

echo "--- the registry knows the host is there, with a digest ---"
# The host registers on a heartbeat, and this is the same wait the devnet job
# does: an entry that has not been announced yet is not a failure, it is early.
found=""
for _ in $(seq 1 50); do
	if curl -fsS "http://127.0.0.1:$REGISTRY/models?model=llama3.1:8b" | grep -q '"digest"'; then
		found=yes
		break
	fi
	sleep 0.2
done
test -n "$found" || { echo "the registry never listed the host"; cat "$logdir/host.log"; exit 1; }

echo "--- a request through the client reaches the engine ---"
reply="$(curl -fsS "http://127.0.0.1:$CLIENT/v1/chat/completions" \
	-H 'content-type: application/json' \
	-d '{"model":"llama3.1:8b","messages":[{"role":"user","content":"e2e"}]}')"
echo "$reply"
echo "$reply" | grep -q 'you-said'

echo "--- the engine is never reachable without the key ---"
code="$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$HOST/v1/models")"
test "$code" = "401" || { echo "expected 401 without a key, got $code"; exit 1; }

echo "--- usage was metered, per peer ---"
curl -fsS -H 'X-Bothy-Key: ci-key' "http://127.0.0.1:$HOST/bothy/usage" | grep -q '"peer": "default"'

echo "--- and a streamed reply is counted, not merely noticed ---"
curl -fsSN "http://127.0.0.1:$CLIENT/v1/chat/completions" \
	-H 'content-type: application/json' \
	-d '{"model":"llama3.1:8b","stream":true,"messages":[{"role":"user","content":"streamed"}]}' \
	> /dev/null
# Streaming is how most interactive use arrives, so a stream the meter cannot see
# is the common case being invisible.
curl -fsS -H 'X-Bothy-Key: ci-key' "http://127.0.0.1:$HOST/bothy/usage" | grep -q '"unmetered_responses": 0'

echo "--- the owner has a slot of their own ---"
# The default cap of 4 with the default reserve of 1: peers get 3, and the fourth
# is not advertised as available to anybody.
curl -fsS "http://127.0.0.1:$HOST/bothy/healthz" | grep -q '"owner_reserve": 1'
curl -fsS "http://127.0.0.1:$HOST/bothy/healthz" | grep -q '"peer_slots": 3'

echo "--- a second implementation, on the same wire ---"
# PROTOCOL.md says a service can be replaced without touching the others, which is
# a slogan until something that shares no code with this implementation talks to
# one of them. The example client is that something.
example="python $ROOT/examples/python/bothy_client.py"
$example models --host "127.0.0.1:$HOST" --key ci-key | grep -q 'llama3.1:8b'
if $example models --host "127.0.0.1:$HOST" > /dev/null 2>&1; then
	echo "the example client was not refused without a key"
	exit 1
fi
$example chat --host "127.0.0.1:$HOST" --key ci-key \
	--model llama3.1:8b --prompt "from the example" | grep -q 'you-said'
# The same streamed path the client uses, through a different implementation.
$example chat --host "127.0.0.1:$HOST" --key ci-key \
	--model llama3.1:8b --prompt "streamed from the example" --stream | grep -q 'you-said'
# And it refuses weights it was not promised, so the check is not decoration.
if $example chat --host "127.0.0.1:$HOST" --key ci-key \
	--model llama3.1:8b --prompt "no" --expected-digest sha256:deadbeef > /dev/null 2>&1; then
	echo "the example client used weights it was not promised"
	exit 1
fi

echo "--- a share key cannot stop the host ---"
code="$(curl -s -o /dev/null -w '%{http_code}' -X POST "http://127.0.0.1:$HOST/bothy/sharing" \
	-H 'X-Bothy-Key: ci-key' -d '{"paused": true}')"
test "$code" = "401" || { echo "a peer's share key reached the control endpoint: $code"; exit 1; }

echo "--- and the admin key can, without stopping anything ---"
curl -fsS -X POST "http://127.0.0.1:$HOST/bothy/sharing" \
	-H 'X-Bothy-Key: ci-admin' -d '{"paused": true}' | grep -q '"paused": true'
code="$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$CLIENT/v1/chat/completions" \
	-H 'content-type: application/json' \
	-d '{"model":"llama3.1:8b","messages":[{"role":"user","content":"paused"}]}')"
test "$code" = "503" || { echo "expected 503 through the client while paused, got $code"; exit 1; }

echo "--- and a host that asked to be left alone is left alone ---"
# A pause is a refusal without a Retry-After, because waiting is not what fixes it
# -- and it is still a refusal, so the client stops asking this host on every
# request and tells the caller to come back. That is the same rule a full host
# gets, and it applies to every refusal rather than only to the 429s.
code="$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$CLIENT/v1/chat/completions" \
	-H 'content-type: application/json' \
	-d '{"model":"llama3.1:8b","messages":[{"role":"user","content":"still paused"}]}')"
test "$code" = "429" || { echo "expected the client to skip a host that refused it, got $code"; exit 1; }

echo "--- a pinned digest that cannot match must stop the client ---"
# Run in the background and check that it has gone, rather than waiting on it:
# if the port is wrong the failure mode is a client that serves happily, and a
# foreground wait would hang the whole script instead of reporting that.
BOTHY_SHARE_KEY=ci-key $BOTHY connect -listen 127.0.0.1:19107 \
	-discovery-url "http://127.0.0.1:$REGISTRY" -model llama3.1:8b \
	-expected-digest sha256:deadbeef > "$logdir/mismatch.log" 2>&1 &
mismatch_pid=$!
sleep 3
if kill -0 "$mismatch_pid" 2>/dev/null; then
	kill "$mismatch_pid" 2>/dev/null || true
	echo "the client kept serving despite a digest mismatch:"
	cat "$logdir/mismatch.log"
	exit 1
fi
if wait "$mismatch_pid"; then
	echo "the client exited zero despite a digest mismatch:"
	cat "$logdir/mismatch.log"
	exit 1
fi

echo "--- resuming serves callers again ---"
curl -fsS -X POST "http://127.0.0.1:$HOST/bothy/sharing" \
	-H 'X-Bothy-Key: ci-admin' -d '{"paused": false}' | grep -q '"paused": false'
# A client that never saw the refusal, because the one above is deliberately still
# skipping this host -- which is what was just asserted, so it cannot also be the
# one that proves a resume works.
BOTHY_SHARE_KEY=ci-key $BOTHY connect -listen 127.0.0.1:19105 \
	-discovery-url "http://127.0.0.1:$REGISTRY" -model llama3.1:8b > "$logdir/client2.log" 2>&1 &
pids="$pids $!"
wait_for "http://127.0.0.1:19105/bothy/status" "client after a resume"
curl -fsS "http://127.0.0.1:19105/v1/chat/completions" \
	-H 'content-type: application/json' \
	-d '{"model":"llama3.1:8b","messages":[{"role":"user","content":"resumed"}]}' \
	| grep -q 'you-said'

echo "--- two hosts, one paused: the refusal moves the request along ---"
# The behaviour a fleet exists for, checked over the wire rather than in a test
# double: a host that will not take the request must not become the caller's
# problem. The first client is still skipping the host that refused it, so this
# also exercises the re-resolve that finds a host it has never heard of.
curl -fsS -X POST "http://127.0.0.1:$HOST/bothy/sharing" \
	-H 'X-Bothy-Key: ci-admin' -d '{"paused": true}' > /dev/null
BOTHY_SHARE_KEY=ci-key $BOTHY share -listen :19106 \
	-engine-url http://127.0.0.1:$MOCK -engine-kind mock \
	-discovery-url "http://127.0.0.1:$REGISTRY" \
	-address 127.0.0.1:19106 -share-key ci-key > "$logdir/host2.log" 2>&1 &
pids="$pids $!"
wait_for "http://127.0.0.1:19106/bothy/healthz" "second host"

for _ in $(seq 1 50); do
	if curl -fsS "http://127.0.0.1:$CLIENT/v1/chat/completions" \
		-H 'content-type: application/json' \
		-d '{"model":"llama3.1:8b","messages":[{"role":"user","content":"failover"}]}' \
		| grep -q 'you-said'; then
		echo "the request moved to the host that would take it"
		curl -fsS -X POST "http://127.0.0.1:$HOST/bothy/sharing" \
			-H 'X-Bothy-Key: ci-admin' -d '{"paused": false}' > /dev/null || true
		echo "all end-to-end checks passed"
		exit 0
	fi
	sleep 0.2
done
echo "the request never reached the host that was available"
cat "$logdir/client.log"
exit 1
