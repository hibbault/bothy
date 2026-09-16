"""The whole thing, end to end, the way a person runs it.

Every other file in this suite tests one module. This one tests the claim on the
front of the README: it starts the services the way the command line starts them
-- a mock engine, a registry, two hosts sharing it, and a client -- and then makes
ordinary requests to the client's local endpoint, which is the only part of Bothy
anyone is meant to notice.

Two things are deliberately checked here and nowhere else.

**A refusal moves the request along.** Host A is given a budget of one request, so
its second answer is a 429. The client must ask host B rather than hand that
refusal to the caller, because a fleet that has room in it looking busy is the one
failure a fleet exists to avoid.

**The numbers are honest.** The host that served a request says so, the host that
refused says that instead, and the registry is told what a host has free rather
than what it wishes it had.

Everything here speaks HTTP to a real process on a real port, over the wire
contract in PROTOCOL.md. Nothing reaches inside a module: a test that used an
internal API would still pass while the protocol was broken, which is the failure
this file exists to catch.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request

PYTHON_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The group secret: peers present it to a host, and this host requires it. The
# client presents it on the caller's behalf, which is the whole reason the caller
# can use any key it likes at the local end.
SHARE_KEY = "fleet-test-share-key"

READY_TIMEOUT = 30.0


def cli_available() -> bool:
    return os.path.exists(os.path.join(PYTHON_DIR, "bothy", "cli.py"))


def free_port() -> int:
    """A port nobody is using, which is what a test on a busy machine needs."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def http(method: str, url: str, payload=None, key: str = None, timeout: float = 30.0):
    """One request. Returns (status, parsed body or raw text)."""
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    if key:
        headers["X-Bothy-Key"] = key
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
            status = response.status
    except urllib.error.HTTPError as err:
        body = err.read()
        status = err.code
    try:
        return status, json.loads(body)
    except ValueError:
        return status, body.decode("utf-8", "replace")


class Service:
    """One Bothy command, running as its own process.

    Output goes to a file rather than a pipe: a service that logs a lot would
    block on a full pipe nobody is reading, and a test that hangs is worse than
    one that fails. The file is quoted when something goes wrong, which is the
    only moment it matters.
    """

    def __init__(self, name: str, args, env=None, ready: str = None):
        self.name = name
        log = tempfile.NamedTemporaryFile(prefix="bothy-fleet-", suffix=".log", delete=False)
        self.log_path = log.name
        environment = dict(os.environ)
        environment.update(env or {})
        environment["PYTHONPATH"] = PYTHON_DIR
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "bothy"] + list(args),
            cwd=PYTHON_DIR,
            env=environment,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        log.close()
        self.ready = ready
        self.base = None

    def wait_ready(self, base: str, path: str = "/healthz", timeout: float = READY_TIMEOUT) -> None:
        """Wait until the service answers, or report what it said instead."""
        self.base = base
        deadline = time.monotonic() + timeout
        last = ""
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise AssertionError("%s exited with %s:\n%s" % (self.name, self.proc.returncode, self.logs()))
            try:
                with urllib.request.urlopen(base + path, timeout=2) as response:
                    response.read()
                    return
            except urllib.error.HTTPError:
                return  # an answer is an answer, even a 401: it is listening
            except Exception as err:  # not up yet
                last = str(err)
            time.sleep(0.1)
        raise AssertionError("%s never answered %s (%s):\n%s" % (self.name, path, last, self.logs()))

    def logs(self) -> str:
        try:
            with open(self.log_path, "r", encoding="utf-8", errors="replace") as handle:
                return handle.read()
        except OSError:
            return "(no output)"

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=10)
        try:
            os.unlink(self.log_path)
        except OSError:
            pass


@unittest.skipUnless(cli_available(), "the Python command line does not exist yet: this is the acceptance test for the migration")
class FleetTest(unittest.TestCase):
    """The one test that is about Bothy rather than about a module."""

    @classmethod
    def setUpClass(cls):
        cls.services = []
        try:
            cls._start()
        except Exception:
            cls.tearDownClass()
            raise

    @classmethod
    def tearDownClass(cls):
        for service in reversed(cls.services):
            service.stop()
        cls.services = []

    @classmethod
    def start(cls, name, args, base, ready="/healthz", env=None):
        service = Service(name, args, env=env)
        cls.services.append(service)
        service.wait_ready(base, ready)
        return service

    @classmethod
    def _start(cls):
        cls.engine_port = free_port()
        cls.registry_port = free_port()
        cls.host_a_port = free_port()
        cls.host_b_port = free_port()
        cls.client_port = free_port()

        engine_base = "http://127.0.0.1:%d" % cls.engine_port
        cls.engine = cls.start("mock engine", ["mock", "-listen", "127.0.0.1:%d" % cls.engine_port], engine_base, "/api/tags")

        # The model name is read back rather than guessed: a test that hard-codes
        # one is a test that breaks when the mock engine's defaults change, for a
        # reason that has nothing to do with what it is checking.
        status, tags = http("GET", engine_base + "/api/tags")
        assert status == 200, "mock engine %s: %r" % (status, tags)
        cls.model = (tags.get("models") or [{}])[0].get("name") or tags["models"][0]["model"]
        assert cls.model, "the mock engine offered no models: %r" % (tags,)

        cls.registry = cls.start(
            "registry", ["discovery", "-listen", "127.0.0.1:%d" % cls.registry_port], "http://127.0.0.1:%d" % cls.registry_port, "/healthz"
        )
        registry_url = "http://127.0.0.1:%d" % cls.registry_port
        cls.registry_url = registry_url
        common = [
            "-engine-url", engine_base,
            "-engine-kind", "mock",
            "-discovery-url", registry_url,
            "-max-concurrent", "4",
            "-owner-reserve", "1",
        ]
        # Host A runs out of budget after one request, which is what makes the
        # routing behaviour observable from the outside.
        cls.host_a = cls.start(
            "host A",
            ["share", "-listen", "127.0.0.1:%d" % cls.host_a_port, "-address", "127.0.0.1:%d" % cls.host_a_port, "-peer-quota", "1/1h"] + common,
            "http://127.0.0.1:%d" % cls.host_a_port,
            "/bothy/healthz",
            env={"BOTHY_SHARE_KEY": SHARE_KEY},
        )
        cls.host_b = cls.start(
            "host B",
            ["share", "-listen", "127.0.0.1:%d" % cls.host_b_port, "-address", "127.0.0.1:%d" % cls.host_b_port] + common,
            "http://127.0.0.1:%d" % cls.host_b_port,
            "/bothy/healthz",
            env={"BOTHY_SHARE_KEY": SHARE_KEY},
        )
        cls.client = cls.start(
            "client",
            ["connect", "-listen", "127.0.0.1:%d" % cls.client_port, "-discovery-url", cls.registry_url, "-model", cls.model],
            "http://127.0.0.1:%d" % cls.client_port,
            "/bothy/status",
            env={"BOTHY_SHARE_KEY": SHARE_KEY},
        )
        cls.client_base = "http://127.0.0.1:%d" % cls.client_port

        # Registration is a heartbeat, so a host that is up is not necessarily a
        # host that is advertised yet. Waiting here rather than in each test keeps
        # a startup race from looking like a routing bug.
        deadline = time.monotonic() + READY_TIMEOUT
        while time.monotonic() < deadline:
            if len(cls.registry_entries_now()) == 2:
                return
            time.sleep(0.1)
        raise AssertionError(
            "the registry never listed both hosts:\nregistry:\n%s\nhost A:\n%s\nhost B:\n%s"
            % (cls.registry.logs(), cls.host_a.logs(), cls.host_b.logs())
        )

    def chat(self, route="/v1/chat/completions"):
        """One completion, through the client's local endpoint, with any old key.

        The route is the one the mock engine actually serves: it speaks the
        OpenAI shape, plus Ollama's `/api/tags` for the model list. The key is
        deliberately not the share key -- the local endpoint is loopback only, so
        it accepts anything, and what reaches the host is the share key the client
        was configured with.
        """
        return http(
            "POST",
            self.client_base + route,
            {"model": self.model, "messages": [{"role": "user", "content": "hello"}], "stream": False, "max_tokens": 16},
            key="any-local-key",
            timeout=30,
        )

    def registry_entries(self):
        status, body = http("GET", "http://127.0.0.1:%d/models?model=%s" % (self.registry_port, urllib.parse.quote(self.model)))
        self.assertEqual(status, 200, body)
        return body["entries"]

    @classmethod
    def registry_entries_now(cls):
        """The live entries, without any assertion: used while waiting for startup."""
        status, body = http("GET", "http://127.0.0.1:%d/models?model=%s" % (cls.registry_port, urllib.parse.quote(cls.model)))
        return body.get("entries", []) if status == 200 else []

    def usage(self, port):
        status, body = http("GET", "http://127.0.0.1:%d/bothy/usage" % port, key=SHARE_KEY)
        self.assertEqual(status, 200, body)
        return body

    # --- the fleet -----------------------------------------------------------

    def test_the_registry_lists_hosts_with_their_free_slots(self):
        entries = self.registry_entries()
        self.assertEqual(len(entries), 2, "both hosts should be advertised: %r" % (entries,))
        for entry in entries:
            # Four slots with one kept for the owner: the reserve is what makes a
            # host look full to peers before it is actually full.
            self.assertEqual(entry.get("free"), 3, "free peer slots, reserve excluded: %r" % (entry,))
            self.assertEqual(entry["model"], self.model)
            self.assertTrue(entry["address"], "an address a peer can dial: %r" % (entry,))

    def test_a_request_through_the_client_is_answered_by_a_host(self):
        status, body = self.chat()
        self.assertEqual(status, 200, body)
        self.assertTrue(body.get("choices"), "a completion came back: %r" % (body,))
        status, usage = http("GET", self.client_base + "/bothy/status")
        self.assertEqual(status, 200, usage)
        self.assertTrue(usage["host"], "the client should say which host answered: %r" % (usage,))
        self.assertEqual(usage["model"], self.model)

    def test_a_refusal_moves_the_request_to_the_next_host(self):
        """The behaviour the whole fleet exists for."""
        codes = [self.chat()[0] for _ in range(6)]
        self.assertEqual(codes, [200] * 6, "a busy host must not become the caller's problem: %r" % (codes,))

        a = sum(row["requests"] for row in self.usage(self.host_a_port)["peers"])
        a_limited = sum(row["limited"] for row in self.usage(self.host_a_port)["peers"])
        b = sum(row["requests"] for row in self.usage(self.host_b_port)["peers"])
        self.assertGreaterEqual(a, 1, "host A should have served its one allowed request")
        self.assertGreaterEqual(a_limited, 1, "host A should have refused at least once")
        self.assertGreaterEqual(b, 1, "host B should have picked up what A refused")

    def test_the_host_reports_who_used_it(self):
        status, usage = http("GET", "http://127.0.0.1:%d/bothy/usage" % self.host_b_port, key=SHARE_KEY)
        self.assertEqual(status, 200, usage)
        self.assertTrue(usage["peers"], "the answer to 'who is using my GPU?' cannot be nobody: %r" % (usage,))
        row = usage["peers"][0]
        for field in ("peer", "requests", "prompt_tokens", "completion_tokens"):
            self.assertIn(field, row, "usage rows are a report, so they name their fields: %r" % (row,))
        self.assertGreater(row["requests"], 0)

    def test_the_host_requires_the_share_key(self):
        status, body = http("POST", "http://127.0.0.1:%d/v1/chat/completions" % self.host_b_port, {"model": self.model, "messages": []})
        self.assertEqual(status, 401, body)
        self.assertIn("error", body, "an OpenAI-shaped error, so a client shows something useful: %r" % (body,))

    def test_a_model_nobody_offers_is_refused_clearly(self):
        """A caller asking for something the fleet does not have gets an answer.

        The client is pointed at the same registry but told to ask for a model
        nobody advertises, which is the only way to reach this path: a client
        routes by the model it was configured with, so the model named in a
        request body reaches whichever host the client picked. A mock engine
        answers any name at all, so asking for an unserved model through the
        configured client would prove nothing.
        """
        port = free_port()
        self.start(
            "client for an unserved model",
            ["connect", "-listen", "127.0.0.1:%d" % port, "-discovery-url", self.registry_url, "-model", "nobody-offers:1b"],
            "http://127.0.0.1:%d" % port,
            "/bothy/status",
            env={"BOTHY_SHARE_KEY": SHARE_KEY},
        )
        status, body = http(
            "POST",
            "http://127.0.0.1:%d/v1/chat/completions" % port,
            {"model": "nobody-offers:1b", "messages": [{"role": "user", "content": "hi"}], "stream": False},
            key="any-local-key",
            timeout=30,
        )
        self.assertIn(status, (404, 502, 503), "a clear failure rather than a hang: %r" % (body,))
        self.assertIn("error", body)


if __name__ == "__main__":
    unittest.main()
