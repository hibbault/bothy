#!/usr/bin/env python3
"""Talk to Bothy from Python, with no dependencies.

Nothing here is privileged: Bothy is HTTP/JSON, so this script is an
implementation of PROTOCOL.md rather than a wrapper around the service itself. Use
it as a starting point for tooling, or as the shape of a Python host.

Requires Python 3.8+. Standard library only — no pip install.

  # What is on offer, anywhere?
  ./bothy_client.py registry --registry http://localhost:8080

  # Ask a host what it serves, with digests
  ./bothy_client.py models --host box.example:7777 --key dev-share-key

  # One completion, then a streamed one
  ./bothy_client.py chat --host box.example:7777 --key dev-share-key \\
      --model llama3.1:8b --prompt "who are you?"

  # Refuse to run on different weights
  ./bothy_client.py chat --host box.example:7777 --key dev-share-key \\
      --model llama3.1:8b --prompt "hi" \\
      --expected-digest sha256:1111111111111111111111111111111111111111111111111111111111111111

  # Or point at the local client endpoint instead of a host
  ./bothy_client.py chat --host 127.0.0.1:11223 --model llama3.1:8b --prompt "hi"
"""

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request

TIMEOUT = 60


class BothyError(RuntimeError):
    """An error bothy returned, or a transport failure."""


def _url(host, path):
    if "://" not in host:
        host = "http://" + host
    return host.rstrip("/") + path


def _open(request, timeout=TIMEOUT):
    try:
        return urllib.request.urlopen(request, timeout=timeout)
    except urllib.error.HTTPError as err:
        body = err.read().decode("utf-8", "replace")
        message = body
        try:
            message = json.loads(body)["error"]["message"]
        except (ValueError, KeyError, TypeError):
            pass
        raise BothyError("HTTP %s: %s" % (err.code, message or err.reason)) from None
    except urllib.error.URLError as err:
        raise BothyError("cannot reach %s: %s" % (request.full_url, err.reason)) from None


def get_json(host, path, key=None):
    request = urllib.request.Request(_url(host, path))
    request.add_header("Accept", "application/json")
    if key:
        request.add_header("X-Bothy-Key", key)
    with _open(request) as response:
        return json.load(response)


def post_json(host, path, payload, key=None, timeout=TIMEOUT):
    request = urllib.request.Request(
        _url(host, path), data=json.dumps(payload).encode("utf-8"), method="POST"
    )
    request.add_header("Content-Type", "application/json")
    request.add_header("Accept", "application/json")
    if key:
        request.add_header("X-Bothy-Key", key)
    with _open(request, timeout=timeout) as response:
        return json.load(response)


def discover(registry, model=None):
    """Ask a registry who has a model."""
    path = "/models"
    if model:
        path += "?model=" + urllib.parse.quote(model)
    return get_json(registry, path)["entries"]


def host_models(host, key=None):
    """Ask a host what it serves, which is also how a direct client learns what
    it can verify."""
    return get_json(host, "/bothy/models", key=key)["models"]


def host_usage(host, key=None):
    return get_json(host, "/bothy/usage", key=key)


def normalize_digest(value):
    value = (value or "").strip().lower()
    if not value:
        return ""
    return value if value.startswith("sha256:") else "sha256:" + value


def check_digest(expected, actual, model_name):
    """Raise unless the host's digest is the one we asked for.

    No expectation accepts anything. A host that advertises no digest never
    satisfies one, because "unknown" must not read as "verified".
    """
    if not (expected or "").strip():
        return
    if normalize_digest(expected) != normalize_digest(actual):
        raise BothyError(
            "digest mismatch for %s: expected %s, host offers %s"
            % (model_name, normalize_digest(expected), normalize_digest(actual) or "nothing")
        )


def chat(host, model, prompt, key=None, stream=False, extra=None):
    """Send one chat completion. Yields text chunks when streaming."""
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": bool(stream),
    }
    payload.update(extra or {})

    if not stream:
        body = post_json(host, "/v1/chat/completions", payload, key=key)
        return body["choices"][0]["message"]["content"]

    request = urllib.request.Request(
        _url(host, "/v1/chat/completions"),
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
    )
    request.add_header("Content-Type", "application/json")
    request.add_header("Accept", "text/event-stream")
    if key:
        request.add_header("X-Bothy-Key", key)

    def chunks():
        with _open(request) as response:
            for raw in response:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                data = line[len("data:"):].strip()
                if not data or data == "[DONE]":
                    continue
                try:
                    frame = json.loads(data)
                except ValueError:
                    continue
                for choice in frame.get("choices", []):
                    text = (choice.get("delta") or {}).get("content")
                    if text:
                        yield text

    return chunks()


def _run(args):
    if args.command == "registry":
        entries = discover(args.registry, args.model)
        if not entries:
            print("no host is offering that right now")
            return 0
        for entry in entries:
            print("%-20s %-40s %s" % (
                entry.get("host") or "-",
                entry.get("address") or "-",
                (entry.get("model") or "") + " " + (entry.get("digest") or "digest unknown"),
            ))
        return 0

    if args.command == "models":
        for item in host_models(args.host, args.key):
            print("%-24s %s" % (item.get("name"), item.get("digest") or "digest unknown"))
        return 0

    if args.command == "usage":
        print(json.dumps(host_usage(args.host, args.key), indent=2))
        return 0

    if args.command == "status":
        print(json.dumps(get_json(args.host, "/bothy/status"), indent=2))
        return 0

    if args.command == "chat":
        # Verify before spending a token on it, when we were told what to expect.
        if args.expected_digest:
            offered = [
                m for m in host_models(args.host, args.key)
                if m.get("name") == args.model
                or m.get("name", "").split(":")[0] == args.model.split(":")[0]
            ]
            actual = offered[0].get("digest") if offered else ""
            check_digest(args.expected_digest, actual, args.model)
            print("digest verified: %s" % normalize_digest(actual), file=sys.stderr)

        reply = chat(args.host, args.model, args.prompt, key=args.key, stream=args.stream)
        if args.stream:
            for text in reply:
                sys.stdout.write(text)
                sys.stdout.flush()
            sys.stdout.write("\n")
        else:
            print(reply)
        return 0

    raise SystemExit("unknown command")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Talk to Bothy from Python.")
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p):
        p.add_argument("--host", default="127.0.0.1:11223",
                       help="host (or the local client endpoint) to talk to")
        p.add_argument("--key", help="share key, if the host requires one")

    registry = sub.add_parser("registry", help="ask a registry who has a model")
    registry.add_argument("--registry", default="http://localhost:8080")
    registry.add_argument("--model", help="model to look for; a bare name matches any tag")

    models = sub.add_parser("models", help="ask a host what it serves")
    common(models)

    usage = sub.add_parser("usage", help="ask a host who is using it")
    common(usage)

    status = sub.add_parser("status", help="ask the local client where it is connected")
    common(status)

    chat_cmd = sub.add_parser("chat", help="send one completion")
    common(chat_cmd)
    chat_cmd.add_argument("--model", required=True)
    chat_cmd.add_argument("--prompt", required=True)
    chat_cmd.add_argument("--stream", action="store_true", help="stream the reply as it arrives")
    chat_cmd.add_argument("--expected-digest",
                          help="refuse to run unless the host serves exactly these weights")

    args = parser.parse_args(argv)
    try:
        return _run(args)
    except BothyError as err:
        print("bothy: %s" % err, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
