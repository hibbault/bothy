"""Which models an engine can serve, and whether the digest it reports means
anything.

These tests came over from the Go side with the package, and they are the
definition of the two things this package is easy to get plausibly wrong. The
first is which digest to believe: an engine's API reports the *manifest* digest,
which changes whenever any layer does, while the layer that holds the weights is
the one that means "same model". The second is `auto`, which has to work against
an engine that does not exist yet -- a container that has not finished starting
is a probe that fails, not a decision to remember.

`t.TempDir` and `httptest.Server` have no stdlib counterpart, so the two are
built here: a directory per test, and a threaded HTTP server answering a fixed
route table. The server is deliberately the standard library's rather than
`bothy.httpx.Server`: this package's client side is plain `urllib`, it does not
import `httpx` at all, and a test for the client should not need the project's
server to be finished first. What the tests need from the server is a status, a
body, and a record of what was asked for.
"""

from __future__ import annotations

import http.server
import json
import os
import shutil
import tempfile
import threading
import unittest

from bothy import digest, engine, model
from bothy.errors import BothyError, ConfigError


def _body_bytes(body):
    """The bytes to answer with: JSON for anything but None, bytes and str."""
    if body is None:
        return b""
    if isinstance(body, (bytes, bytearray)):
        return bytes(body)
    if isinstance(body, str):
        return body.encode("utf-8")
    return json.dumps(body).encode("utf-8")


class _RouteHandler(http.server.BaseHTTPRequestHandler):
    """Answers one request from the table its server was given."""

    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        """Silence the stdlib's stderr line; the tests assert on the routes."""

    def do_GET(self):
        srv = self.server
        srv.paths.append(self.path)
        srv.hits[self.path] = srv.hits.get(self.path, 0) + 1
        route = srv.routes.get(self.path)
        if route is None:
            self._answer(404, b"page not found\n")
            return
        if callable(route):
            # A route that changes its answer between calls, which is how a
            # probe that failed once can be shown to succeed the next time.
            route = route(srv.hits[self.path])
        status, body = route
        self._answer(status, _body_bytes(body))

    def _answer(self, status, body):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)


class _Routes(http.server.ThreadingHTTPServer):
    """ThreadingHTTPServer with a route table, a URL, and a request log.

    Threads because a request that fails has to fail while the test is watching
    it, and daemon threads so a hung one cannot keep the run alive.
    """

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, routes):
        self.routes = routes
        # Every path asked for, in order: the tests use it to show that a route
        # was requested once, or again.
        self.paths = []
        # How many times each path has been asked for, which is what lets a route
        # answer differently the second time.
        self.hits = {}
        super().__init__(("127.0.0.1", 0), _RouteHandler)

    @property
    def url(self):
        return "http://127.0.0.1:%d" % self.server_address[1]


class _EngineCase(unittest.TestCase):
    """Go's `t.TempDir` and `httptest.Server`, in one place."""

    def temp_dir(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        return d

    def write_file(self, path, data):
        with open(path, "wb") as f:
            f.write(data)
        return path

    def write_manifest(self, models_dir, namespace, name, tag, layers):
        """Lay out an Ollama-style manifest for name:tag.

        The layout is the real one, because finding it is half of what these
        tests are about: `manifests/<namespace>/<name>/<tag>`.
        """
        d = os.path.join(models_dir, "manifests", namespace, name)
        os.makedirs(d, exist_ok=True)
        self.write_file(os.path.join(d, tag), json.dumps({"layers": layers}).encode("utf-8"))

    def serve(self, routes):
        """Start an HTTP server answering routes, the counterpart of Go's
        `httptest.Server` plus `routeServer`.

        routes maps a path to a `(status, body)` pair, where a body is an object
        to answer with as JSON (None for no body), or to a callable given the
        number of times that path has now been asked for and returning such a
        pair. Anything unmatched is a 404.
        """
        srv = _Routes(routes)
        thread = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, name="test-engine-http", daemon=True)
        thread.start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        return srv


class TestListModelsPrefersWeightsDigestOverManifestDigest(_EngineCase):
    # The API reports the manifest digest, which changes whenever any layer does.
    # The weights layer is the one that means "same model", so it must win.
    def test_the_weights_layer_digest_wins(self):
        models_dir = self.temp_dir()
        weights = "sha256:weightshash"
        self.write_manifest(
            models_dir,
            "registry.ollama.ai",
            "library/llama3.1",
            "8b",
            [
                {"mediaType": "application/vnd.ollama.image.config", "digest": "sha256:confighash"},
                {"mediaType": "application/vnd.ollama.image.model", "digest": weights},
            ],
        )
        srv = self.serve({"/api/tags": (200, {"models": [{"name": "llama3.1:8b", "digest": "sha256:manifesthash"}]})})

        lister = engine.new("ollama", srv.url, engine.Options(models_dir=models_dir))
        models = lister.list_models()
        self.assertEqual(len(models), 1)
        self.assertEqual(models[0].name, "llama3.1:8b")
        self.assertEqual(models[0].digest, weights, "want the weights layer digest, not the manifest digest")


class TestListModelsFallsBackToTheReportedDigest(_EngineCase):
    def test_the_reported_digest_is_used_with_no_manifest(self):
        srv = self.serve({"/api/tags": (200, {"models": [{"name": "llama3.1:8b", "digest": "sha256:manifesthash"}]})})
        lister = engine.new("ollama", srv.url, engine.Options())
        models = lister.list_models()
        self.assertEqual(len(models), 1)
        self.assertEqual(models[0].digest, "sha256:manifesthash")


class TestAutoFallsBackToOllama(_EngineCase):
    # The default "auto" kind has to work against a real engine, which has no
    # /internal/models route.
    def test_an_engine_without_the_mock_route_is_listed_through_ollama(self):
        srv = self.serve({"/api/tags": (200, {"models": [{"name": "llama3.1:8b", "digest": "sha256:abc"}]})})
        lister = engine.new("auto", srv.url, engine.Options())
        models = lister.list_models()
        self.assertEqual(len(models), 1)
        self.assertEqual(models[0].name, "llama3.1:8b")
        self.assertIn("/internal/models", srv.paths, "the mock endpoint must be probed first")


class TestAutoReProbesOnEveryCall(_EngineCase):
    # A container race at startup must heal itself: the probe that failed is not
    # a fact about the engine, it is a fact about the moment it was asked.
    def test_a_failed_probe_is_not_cached(self):
        srv = self.serve(
            {
                "/internal/models": lambda hits: (404, None)
                if hits == 1
                else (200, {"models": [{"name": "llama3.1:8b", "digest": "sha256:frommock"}]}),
                "/api/tags": (200, {"models": [{"name": "llama3.1:8b", "digest": "sha256:fromollama"}]}),
            }
        )
        lister = engine.new("auto", srv.url, engine.Options())

        # The mock engine is not up yet, so the first call falls back.
        self.assertEqual(lister.list_models()[0].digest, "sha256:fromollama")
        # It is up now, and nothing remembered the failure: the next call finds it
        # without the host being restarted.
        self.assertEqual(lister.list_models()[0].digest, "sha256:frommock")
        self.assertEqual(srv.hits["/internal/models"], 2, "want the probe repeated rather than cached")


class TestManifestPathFindsTheDefaultNamespace(_EngineCase):
    def test_the_library_namespace_is_found_directly(self):
        models_dir = self.temp_dir()
        self.write_manifest(models_dir, "registry.ollama.ai", "library/llama3.1", "8b", None)
        want = os.path.join(models_dir, "manifests", "registry.ollama.ai", "library", "llama3.1", "8b")
        self.assertEqual(engine.manifest_path(models_dir, "llama3.1:8b"), want)


class TestManifestPathFindsAnotherNamespace(_EngineCase):
    # A model pulled from someone else's registry still has to resolve, which is
    # what the walk is for.
    def test_the_walk_finds_a_manifest_outside_the_library_namespace(self):
        models_dir = self.temp_dir()
        self.write_manifest(models_dir, "example.com/someone", "mistral", "7b", None)

        got = engine.manifest_path(models_dir, "mistral:7b")
        self.assertNotEqual(got, "", "expected the walk to find a manifest outside the library namespace")
        self.assertEqual(os.path.basename(got), "7b")
        self.assertEqual(os.path.basename(os.path.dirname(got)), "mistral")

        self.assertEqual(engine.manifest_path(models_dir, "absent:1b"), "", "a missing model has no manifest")
        self.assertEqual(engine.manifest_path("", "mistral:7b"), "", "no models directory, no manifest")


class TestManifestPathRejectsNamesItCannotUse(_EngineCase):
    def test_names_with_no_base_or_no_tag_are_refused(self):
        models_dir = self.temp_dir()
        for name in ("", ":8b", "llama3.1:"):
            self.assertEqual(engine.manifest_path(models_dir, name), "", "manifest_path(%r)" % name)
        self.assertEqual(engine.manifest_path("", ""), "")


class TestManifestWithoutAWeightsLayerReportsNothing(_EngineCase):
    # A manifest with no weights layer at all must not be mistaken for one whose
    # weights are known: the fallback is the caller's business, not a guess here.
    def test_the_reported_digest_is_kept(self):
        models_dir = self.temp_dir()
        self.write_manifest(
            models_dir,
            "registry.ollama.ai",
            "library/llama3.1",
            "8b",
            [{"mediaType": "application/vnd.ollama.image.config", "digest": "sha256:confighash"}],
        )
        srv = self.serve({"/api/tags": (200, {"models": [{"name": "llama3.1:8b", "digest": "sha256:manifesthash"}]})})

        lister = engine.new("ollama", srv.url, engine.Options(models_dir=models_dir))
        models = lister.list_models()
        self.assertEqual(len(models), 1)
        self.assertEqual(models[0].digest, "sha256:manifesthash", "want the reported digest as the fallback")


class TestStaticKindHashesAWeightsFile(_EngineCase):
    def test_a_file_is_hashed_and_named(self):
        weights = os.path.join(self.temp_dir(), "model.gguf")
        self.write_file(weights, b"weights")

        lister = engine.new("static", "http://unused", engine.Options(weights_path=weights, weights_model="local-gguf"))
        models = lister.list_models()
        want = digest.file(weights)
        self.assertEqual(len(models), 1)
        self.assertEqual(models[0].name, "local-gguf")
        self.assertEqual(models[0].digest, want)


class TestStaticKindServesTheOperatorsList(_EngineCase):
    # The spec string is "name=digest,name2=digest2" and is read with
    # model.parse_list; a bare name is a model with no digest to offer.
    def test_the_list_is_served_as_written(self):
        lister = engine.new(
            "static",
            "http://unused",
            engine.Options(static=model.parse_list("local-gguf=sha256:aa,other")),
        )
        models = lister.list_models()
        self.assertEqual([(m.name, m.digest) for m in models], [("local-gguf", "sha256:aa"), ("other", "")])


class TestStaticKindRefusesConfigurationsItCannotServe(_EngineCase):
    # The static kind is the escape hatch, so its failures have to be plain ones
    # at startup rather than a host that announces nothing.
    def test_no_list_and_no_weights_file(self):
        lister = engine.new("static", "http://unused", engine.Options())
        with self.assertRaises(engine.EngineError) as ctx:
            lister.list_models()
        self.assertIn("BOTHY_MODELS", str(ctx.exception), "the error must say how to fix it")

    def test_a_weights_file_that_is_not_there(self):
        missing = os.path.join(self.temp_dir(), "absent.gguf")
        lister = engine.new("static", "http://unused", engine.Options(weights_path=missing, weights_model="local"))
        with self.assertRaises(engine.EngineError) as ctx:
            lister.list_models()
        self.assertIn(missing, str(ctx.exception), "the error must name the file")


class TestStaticKindAssignsTheHashedWeightsToTheRightModel(_EngineCase):
    def test_only_the_named_model_gets_the_hash(self):
        weights = os.path.join(self.temp_dir(), "model.gguf")
        self.write_file(weights, b"some weights")

        configured = [
            model.Model(name="local-gguf", digest="stale"),
            model.Model(name="other", digest="sha256:untouched"),
        ]
        lister = engine.new(
            "static",
            "http://unused",
            engine.Options(static=configured, weights_path=weights, weights_model="local-gguf"),
        )
        models = lister.list_models()

        hashed = models[0].digest
        self.assertTrue(hashed.startswith("sha256:"), "want a normalized digest, got %r" % hashed)
        self.assertEqual(len(hashed), len("sha256:") + 64, "want a full sha256, got %r" % hashed)
        self.assertEqual(models[1].digest, "sha256:untouched", "an unrelated model's digest was overwritten")
        # The configured list belongs to the caller; handing back the same list
        # would let a later write mutate the operator's config.
        self.assertEqual(configured[0].digest, "stale", "the configured list was mutated in place")


class TestEachKindReportsItsName(_EngineCase):
    # Each kind reports the name that shows up in a host's logs and healthz, so a
    # rename is a user-visible change rather than an internal one.
    def test_the_kind_string_is_the_configured_one(self):
        srv = self.serve({"/nowhere": (200, None)})
        for kind, want in [
            ("", "auto"),
            ("auto", "auto"),
            ("ollama", "ollama"),
            ("openai", "openai"),
            ("mock", "mock"),
            ("static", "static"),
        ]:
            lister = engine.new(kind, srv.url, engine.Options(static=[model.Model(name="m")]))
            self.assertEqual(lister.kind(), want, "new(%r).kind()" % kind)


class TestBaseURLIsNormalised(_EngineCase):
    def test_a_trailing_slash_does_not_double(self):
        srv = self.serve({"/api/tags": (200, {"models": []})})

        # A trailing slash must not turn into //api/tags, which 404s on most
        # servers.
        lister = engine.new("ollama", "  " + srv.url + "/  ", engine.Options())
        self.assertEqual(lister.list_models(), [])
        self.assertEqual(srv.paths, ["/api/tags"], "want one clean /api/tags")


class TestAutoPrefersTheMockDigestEndpoint(_EngineCase):
    # The default kind probes for the mock's digest endpoint first. That is what
    # makes a mock engine give real digests without configuring anything, and it
    # has to win over the Ollama route when both exist.
    def test_both_routes_present_the_mock_wins(self):
        srv = self.serve(
            {
                "/internal/models": (200, {"models": [{"name": "llama3.1:8b", "digest": "sha256:frommock"}]}),
                "/api/tags": (200, {"models": [{"name": "llama3.1:8b", "digest": "sha256:fromollama"}]}),
            }
        )
        lister = engine.new("auto", srv.url, engine.Options())
        models = lister.list_models()
        self.assertEqual(len(models), 1)
        self.assertEqual(models[0].digest, "sha256:frommock", "want the mock endpoint's digest")
        self.assertEqual(srv.paths, ["/internal/models"], "the Ollama route must not be read as well")


class TestMockKindReportsDigests(_EngineCase):
    def test_the_digest_is_normalised(self):
        srv = self.serve({"/internal/models": (200, {"models": [{"name": "llama3.1:8b", "digest": "ABCDEF"}]})})
        lister = engine.new("mock", srv.url, engine.Options())
        models = lister.list_models()
        self.assertEqual(len(models), 1)
        self.assertEqual(models[0].digest, "sha256:abcdef", "want the digest normalised")


class TestOllamaAcceptsEitherNameFieldAndSkipsEmptyRows(_EngineCase):
    # Ollama's /api/tags is inconsistent between versions about which field holds
    # the name, and a row with neither is unusable rather than an empty-named
    # model.
    def test_name_model_and_empty_rows(self):
        srv = self.serve(
            {
                "/api/tags": (
                    200,
                    {
                        "models": [
                            {"name": "llama3.1:8b", "digest": "sha256:one"},
                            {"model": "qwen2.5:7b", "digest": "sha256:two"},
                            {"name": "", "model": "", "digest": "sha256:three"},
                        ]
                    },
                )
            }
        )
        lister = engine.new("ollama", srv.url, engine.Options())
        models = lister.list_models()
        self.assertEqual(len(models), 2, "want the unnamed row dropped, got %r" % (models,))
        self.assertEqual(models[1].name, "qwen2.5:7b", "want the model field used when name is absent")


class TestEngineFailuresAreReportedRatherThanTreatedAsEmpty(_EngineCase):
    # Every one of these would otherwise be a host that announces it has nothing
    # to serve while an engine sits there answering something else.
    def test_ollama_a_non_200(self):
        srv = self.serve({"/api/tags": (404, None)})
        lister = engine.new("ollama", srv.url, engine.Options())
        with self.assertRaises(engine.EngineError) as ctx:
            lister.list_models()
        self.assertIn("/api/tags", str(ctx.exception), "the error must say which endpoint failed")

    def test_ollama_a_body_that_is_not_the_documented_shape(self):
        srv = self.serve({"/api/tags": (200, {"models": "nope"})})
        lister = engine.new("ollama", srv.url, engine.Options())
        with self.assertRaises(engine.EngineError):
            lister.list_models()

    def test_openai_a_non_200(self):
        srv = self.serve({"/v1/models": (401, None)})
        lister = engine.new("openai", srv.url, engine.Options())
        with self.assertRaises(engine.EngineError) as ctx:
            lister.list_models()
        self.assertIn("/v1/models", str(ctx.exception), "the error must say which endpoint failed")

    def test_mock_an_undecodable_body(self):
        srv = self.serve({"/internal/models": (200, {"models": 7})})
        lister = engine.new("mock", srv.url, engine.Options())
        with self.assertRaises(engine.EngineError):
            lister.list_models()

    def test_a_dead_engine(self):
        srv = self.serve({"/api/tags": (200, None)})
        addr = srv.url
        srv.shutdown()
        srv.server_close()

        lister = engine.new("ollama", addr, engine.Options())
        with self.assertRaises(engine.EngineError) as ctx:
            lister.list_models()
        self.assertIn(addr, str(ctx.exception), "the error must name the engine")


class TestOpenAIKindMergesConfiguredDigestsByName(_EngineCase):
    # vLLM and llama.cpp server list model ids and no digests at all. A digest
    # configured by name is the only way those hosts can offer anything
    # verifiable, and it must not be handed to a different size of the same
    # model.
    def test_a_configured_digest_is_merged_and_nothing_else_is(self):
        srv = self.serve(
            {
                "/v1/models": (
                    200,
                    {
                        "object": "list",
                        "data": [{"id": "llama3.1:8b"}, {"id": "llama3.1:70b"}, {"id": ""}],
                    },
                )
            }
        )

        lister = engine.new("openai", srv.url, engine.Options(static=[model.Model(name="llama3.1:8b", digest="sha256:weights")]))
        models = lister.list_models()
        self.assertEqual(len(models), 2, "want the two named ids and not the empty one, got %r" % (models,))
        self.assertEqual(models[0].name, "llama3.1:8b")
        self.assertEqual(models[0].digest, "sha256:weights", "want the configured digest merged in")
        self.assertEqual(models[1].digest, "", "a configured :8b digest must not be applied to :70b")

        # With nothing configured the host offers these models with an empty
        # digest, which a client reads as "unknown" rather than as agreement.
        plain = engine.new("openai", srv.url, engine.Options())
        self.assertEqual([m.digest for m in plain.list_models()], ["", ""])


class TestDigestForUsesExactModelIdentity(unittest.TestCase):
    # `digest_for` is unexported on the Go side, and this test is in the package
    # there, so it reaches the helper directly rather than through an engine. The
    # same access is kept here.
    def test_identity_including_case_and_tag(self):
        configured = [
            model.Model(name="llama3.1", digest="sha256:latest-weights"),
            model.Model(name="qwen2.5:7b", digest="sha256:qwen"),
        ]
        for name, want in [
            # A digest written for llama3.1 (no tag, meaning :latest) does
            # satisfy a served llama3.1:latest...
            ("llama3.1", "sha256:latest-weights"),
            ("llama3.1:latest", "sha256:latest-weights"),
            # ...and not any other size of the same model.
            ("llama3.1:8b", ""),
            ("mistral", ""),
            # Model references are compared case-sensitively, like the digest
            # they are meant to pin: a differently-written name is a different
            # model, and silently borrowing a digest because the spelling is
            # close is exactly the mistake the digest exists to prevent.
            ("Qwen2.5:7B", ""),
        ]:
            self.assertEqual(engine._digest_for(configured, name), want, "digest_for(%r)" % name)
        self.assertEqual(engine._digest_for(None, "llama3.1"), "", "no configured list means no digest")


class TestUnknownKindIsRejected(_EngineCase):
    def test_an_unknown_kind_and_a_missing_url(self):
        with self.assertRaises(ConfigError) as ctx:
            engine.new("nonsense", "http://engine", engine.Options())
        self.assertIn("nonsense", str(ctx.exception), "the error must name the kind")

        with self.assertRaises(ConfigError):
            engine.new("ollama", "  ", engine.Options())


class TestEngineErrorsAreBothyErrors(unittest.TestCase):
    # A host has one clause for "Bothy refused", and this package's listers raise
    # their own type: the two have to meet.
    def test_engine_error_derives_from_bothy_error(self):
        self.assertTrue(issubclass(engine.EngineError, BothyError))
