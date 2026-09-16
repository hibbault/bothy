"""The command line, as a process.

A command line is only really testable as a process: the exit code and which
stream a message lands on are part of the contract, and neither is reachable by
calling a function. So every case below starts `python -m bothy` the way a shell
would, and reads what it printed and what it exited with.

The last two classes came from the Go tree's `internal/app` (in git history).
They are not about the process: what the one command decides is a decision, and a
half that fails is a function that returns -- so they call `app` directly, with a
lister and a logger that answer without a GPU.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from bothy import app, cli, config, model, version
from bothy.errors import BothyError, ConfigError

PYTHON_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def run_bothy(*args):
    """Run the CLI and report what a shell would see: (stdout, stderr, code).

    A command expected to serve forever must not be run through this. The
    environment is stripped of every BOTHY_* setting first, and BOTHY_CONFIG is
    pointed at a file that does not exist, because each assertion below is about
    which source supplied a default -- a flag, a config file, or the built-in
    fallback -- and an export or a config file on the machine running the tests
    would answer for all three.
    """
    environment = {k: v for k, v in os.environ.items() if not k.startswith("BOTHY_")}
    environment["PYTHONPATH"] = PYTHON_DIR
    environment["BOTHY_CONFIG"] = os.path.join(tempfile.gettempdir(), "bothy-cli-tests-no-config")
    proc = subprocess.run(
        [sys.executable, "-m", "bothy"] + list(args),
        cwd=PYTHON_DIR,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=60,
    )
    return proc.stdout, proc.stderr, proc.returncode


class VersionTest(unittest.TestCase):
    """The version is a constant here rather than something a linker stamps, so
    this checks the wiring rather than a number: whatever `bothy.version` holds is
    what the command has to say, under every spelling of it."""

    def test_version_is_printed_on_stdout(self):
        for flag in ("version", "-v", "--version"):
            stdout, stderr, code = run_bothy(flag)
            self.assertEqual(code, 0, "bothy %s exited %d (stderr: %s)" % (flag, code, stderr))
            self.assertEqual(stdout.strip(), "bothy " + version)


class HelpTest(unittest.TestCase):
    """Help is the first thing anyone runs, so it has to list the commands and
    stay on the stream a pipe-then-page user expects."""

    def test_help_describes_every_command(self):
        for flag in ("help", "-h", "--help"):
            stdout, stderr, code = run_bothy(flag)
            self.assertEqual(code, 0, "bothy %s exited %d" % (flag, code))
            self.assertEqual(stdout, "", "bothy %s wrote to stdout, want help on stderr" % flag)
            for want in ("discovery", "share", "connect", "mock", "OpenAI-compatible"):
                self.assertIn(want, stderr, "help does not mention %r:\n%s" % (want, stderr))

    def test_no_arguments_is_a_usage_error(self):
        """Running with no command is a usage error, and saying so with a success
        exit code would break every script that pipes it."""
        stdout, stderr, code = run_bothy()
        self.assertEqual(code, 2)
        self.assertEqual(stdout, "", "wrote %r to stdout, want usage on stderr" % stdout)
        self.assertIn("Commands:", stderr, "usage was not printed:\n%s" % stderr)

    def test_an_unknown_command_is_a_usage_error_that_names_it(self):
        _, stderr, code = run_bothy("frobnicate")
        self.assertEqual(code, 2)
        self.assertIn('unknown command "frobnicate"', stderr)


class NoSwarmTest(unittest.TestCase):
    """The experimental command is a build-tagged plugin in Go; Python has no
    build tags, so the guarantee is that nothing opt-in is wired in at all -- help
    offers no command this binary cannot run, and an unknown one is a usage error.
    """

    def test_a_default_build_has_no_swarm_in_it(self):
        _, help_text, _ = run_bothy("help")
        self.assertNotIn("solve", help_text, "help offers solve in a default build:\n%s" % help_text)

        _, stderr, code = run_bothy("solve", "-task", "whatever.json")
        self.assertEqual(code, 2, "bothy solve exited %d, want 2 — it is not in this build" % code)
        self.assertIn('unknown command "solve"', stderr, "stderr does not report solve as unknown:\n%s" % stderr)


class FailingCommandTest(unittest.TestCase):
    """A command that cannot do its job has to fail loudly and with a non-zero
    status, because that status is what a supervisor or a compose healthcheck
    reads."""

    def test_a_command_that_cannot_start_exits_nonzero(self):
        cases = [
            ("discovery on an address it cannot bind", ["discovery", "-listen", "127.0.0.1:not-a-port"]),
            ("share with a quota it cannot parse", ["share", "-peer-quota", "200"]),
            ("mock with no models configured", ["mock", "-models", ""]),
        ]
        for name, args in cases:
            with self.subTest(name):
                _, stderr, code = run_bothy(*args)
                self.assertEqual(code, 1, "exited %d, want 1 (stderr: %s)" % (code, stderr))
                self.assertIn("exiting", stderr, "stderr does not say the command failed:\n%s" % stderr)
                self.assertIn("err=", stderr, "stderr carries no error detail:\n%s" % stderr)


class ConfigTest(unittest.TestCase):
    """The config file, and the two commands that describe it."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="bothy-cli-")
        self.addCleanup(_remove_tree, self.temp_dir)
        self.path = os.path.join(self.temp_dir, "config")

    def test_every_setting_in_the_template_is_real(self):
        """The template is documentation people will paste from, so every setting
        it mentions has to be one the program actually knows. A template that
        suggests a name the loader then refuses is worse than no template at all.
        """
        offered = []
        for line in cli.config_template.split("\n"):
            trimmed = line.strip()
            if trimmed.startswith("# BOTHY_"):
                offered.append(trimmed[len("# "):])
        self.assertGreaterEqual(
            len(offered), 20, "the template only offers %d settings; it should describe the ones that matter" % len(offered)
        )

        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write("\n".join(offered) + "\n")
        try:
            config.load(self.path)
        except ConfigError as err:
            self.fail("a setting offered by the template does not exist: %s" % err)

    def test_config_path_says_where_it_looked(self):
        stdout, _, code = run_bothy("config", "path", "-config", self.path)
        self.assertEqual(code, 0)
        self.assertIn(self.path, stdout, "output does not name the file: %r" % stdout)
        self.assertIn("not found", stdout, "a missing file should say so rather than look configured: %r" % stdout)

    def test_config_init_writes_a_file_that_loads(self):
        _, _, code = run_bothy("config", "init", "-config", self.path)
        self.assertEqual(code, 0)
        config.load(self.path)

        # And a second init does not quietly throw away edits.
        _, stderr, code = run_bothy("config", "init", "-config", self.path)
        self.assertNotEqual(code, 0, "init overwrote an existing file without being asked to")
        self.assertIn("-force", stderr, "the refusal does not say how to overwrite: %r" % stderr)

        _, _, code = run_bothy("config", "init", "-force", "-config", self.path)
        self.assertEqual(code, 0, "-force did not overwrite")

    @unittest.skipIf(os.name == "nt", "Windows has no file modes to read")
    def test_init_writes_a_file_only_its_owner_can_read(self):
        """A config file may hold a share key, and a key in a world-readable file
        is a key someone else has. The directory is 0700 for the same reason: a
        listing of who runs Bothy here is a map of what to attack."""
        path = os.path.join(self.temp_dir, "bothy", "config")
        _, _, code = run_bothy("config", "init", "-config", path)
        self.assertEqual(code, 0)
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600, "the config file is not 0600")
        self.assertEqual(
            os.stat(os.path.dirname(path)).st_mode & 0o777, 0o700, "the config directory is not 0700"
        )

    def test_a_broken_config_file_stops_the_process(self):
        """A file that exists but cannot be understood stops the process, because
        running on with built-in defaults would leave the limits a mystery."""
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write("BOTHY_MAX_CONCURRENT 2\n")
        _, stderr, code = run_bothy("config", "path", "-config", self.path)
        self.assertNotEqual(code, 0, "a malformed config file was ignored")
        self.assertIn("line 1", stderr, "the error does not say which line: %r" % stderr)

    def test_the_config_file_sets_the_default_for_a_command(self):
        """The config file is read before any command parses its flags, which is
        what makes it the source of their defaults."""
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write("BOTHY_MAX_CONCURRENT = 11\nBOTHY_LISTEN = 127.0.0.1:0\n")

        # No environment, no flag: the file's value has to be what the command uses.
        stdout, stderr, code = run_bothy("share", "-config", self.path, "-h")
        self.assertEqual(code, 0, "exit code = %d: %s%s" % (code, stdout, stderr))
        self.assertIn("default 11", stderr, "the file did not become the flag's default:\n%s" % stderr)


def _remove_tree(path):
    """Remove a temporary directory, without caring whether it is still there."""
    for root, dirs, files in os.walk(path, topdown=False):
        for name in files:
            try:
                os.unlink(os.path.join(root, name))
            except OSError:
                pass
        for name in dirs:
            try:
                os.rmdir(os.path.join(root, name))
            except OSError:
                pass
    try:
        os.rmdir(path)
    except OSError:
        pass


def _quiet():
    """A logger that discards everything, the counterpart of slog's io.Discard."""
    log = logging.getLogger("bothy.app.test")
    if not log.handlers:
        log.addHandler(logging.NullHandler())
    log.propagate = False
    return log


class _FakeLister:
    """An engine that answers with a list, or refuses: the two things
    `should_serve` is allowed to ask of one."""

    def __init__(self, models=None, err=None):
        self.models = list(models or [])
        self.err = err

    def list_models(self):
        if self.err is not None:
            raise self.err
        return self.models

    def kind(self):
        return "fake"


served = [model.Model(name="llama3.1:8b", digest="sha256:aa")]


class ShouldServeTest(unittest.TestCase):
    """What the one command decides is the whole feature: a machine that can
    serve does both, and a machine that cannot does one. The case worth pinning
    hardest is the missing key — this mode is automatic, so a host that let
    strangers spend the GPU without anyone having asked for it would be a default
    nobody chose.
    """

    def test_should_serve(self):
        cases = [
            ("an engine with a model and a key is shared", app.Config(serve=True, share_key="s"), _FakeLister(served), True),
            ("per-peer keys are a key", app.Config(serve=True, share_keys="alice:k1"), _FakeLister(served), True),
            ("no key at all is refused, not shared openly", app.Config(serve=True), _FakeLister(served), False),
            ("an engine with no models is not shared", app.Config(serve=True, share_key="s"), _FakeLister(), False),
            (
                "an engine that never answers means borrowing only",
                app.Config(serve=True, share_key="s"),
                _FakeLister(err=BothyError("connection refused")),
                False,
            ),
            ("-serve=false borrows only, even with a key and a model", app.Config(serve=False, share_key="s"), _FakeLister(served), False),
        ]
        for name, cfg, lister, want in cases:
            with self.subTest(name):
                cfg.probe_timeout = 1.0
                self.assertEqual(app.should_serve(None, cfg, lister, _quiet()), want)


class FailedHalfTest(unittest.TestCase):
    """A half that fails takes the process down. Leaving the other one running
    would be a service nobody asked for — the user sees two ports and assumes both
    work — so the failure is raised, and it names the half that failed, because
    "address already in use" means something different on each.
    """

    def test_a_failed_half_stops_the_other(self):
        boom = BothyError("listen tcp :7777: bind: address already in use")
        stopped = threading.Event()

        def failing(_ctx):
            raise boom

        def partner(ctx):
            while not ctx.is_set():
                time.sleep(0.01)
            stopped.set()

        halves = [app.half(name="share", serve=failing), app.half(name="connect", serve=partner)]
        with self.assertRaises(BothyError) as caught:
            app.run_all(threading.Event(), _quiet(), halves)
        self.assertIs(caught.exception.__cause__, boom, "the failure itself must travel, as Go's %w does")
        self.assertIn("share", str(caught.exception), "the failed half must be named")
        self.assertTrue(stopped.wait(5), "the other half kept running after its partner failed")


if __name__ == "__main__":
    unittest.main()
