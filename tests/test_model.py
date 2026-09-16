"""The shared vocabulary: a model, and the identity of its weights.

The same tag means different weights on different machines -- a different
quantization, a fine-tune, a stale pull. The digest is what makes "the same model"
mean something, so it travels with every model record. These tests are the
definition of that behaviour.
"""

from __future__ import annotations

import unittest

from bothy import model
from bothy.errors import ConfigError


class TestNormalizeDigest(unittest.TestCase):
    def test_normalize_digest(self):
        cases = {
            "": "",
            "ABCDEF": "sha256:abcdef",
            "sha256:ABCDEF": "sha256:abcdef",
            "  sha256:abc  ": "sha256:abc",
        }
        for raw, want in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(model.normalize_digest(raw), want)


class TestEqualDigestNeverMatchesUnknown(unittest.TestCase):
    # "unknown" must never read as "verified", or an engine that cannot report a
    # digest would silently satisfy a pinned expectation.
    def test_equal_digest_never_matches_unknown(self):
        self.assertFalse(model.equal_digest("", ""), "two unknown digests must not compare as equal")
        self.assertTrue(
            model.equal_digest("sha256:ab", "AB"),
            "digests should compare ignoring case and the sha256: prefix",
        )
        self.assertFalse(model.equal_digest("sha256:ab", "sha256:cd"), "different digests are not equal")


class TestSameNameTreatsMissingTagAsLatest(unittest.TestCase):
    def test_same_name_treats_missing_tag_as_latest(self):
        self.assertTrue(model.same_name("llama3.1", "llama3.1:latest"), "a missing tag means :latest")
        self.assertFalse(model.same_name("llama3.1", "llama3.1:8b"), "different tags are different models")


class TestMatches(unittest.TestCase):
    # A request without a tag has to reach a host that only offers tagged models,
    # or "who has llama3.1?" would miss most of the network.
    def test_matches(self):
        cases = [
            ("", "llama3.1:8b", True),
            ("llama3.1", "llama3.1:8b", True),
            ("llama3.1", "llama3.1", True),
            ("llama3.1:latest", "llama3.1", True),
            ("llama3.1:8b", "llama3.1:8b", True),
            ("llama3.1:8b", "llama3.1", False),
            ("llama3.1:8b", "llama3.1:70b", False),
            ("llama3.1", "qwen2.5:7b", False),
        ]
        for want, have, ok in cases:
            with self.subTest(want=want, have=have):
                self.assertEqual(model.matches(want, have), ok)


class TestParseList(unittest.TestCase):
    def test_parse_list(self):
        got = model.parse_list("llama3.1:8b=sha256:aa, plain ")
        self.assertEqual(len(got), 2, "want the empty item dropped and the rest kept")
        self.assertEqual((got[0].name, got[0].digest), ("llama3.1:8b", "sha256:aa"))
        self.assertEqual((got[1].name, got[1].digest), ("plain", ""), "want a name with no digest")
        with self.assertRaises(ConfigError):
            model.parse_list("=sha256:aa")
        self.assertEqual(model.parse_list(""), [])


class TestFormatListRoundTrips(unittest.TestCase):
    # format_list is how a host says in its log what it is serving, so it has to
    # be the inverse of parse_list rather than something that merely looks similar.
    def test_format_list_round_trips(self):
        spec = "llama3.1:8b=sha256:aa,qwen2.5:7b=sha256:bb"
        models = model.parse_list(spec)
        self.assertEqual(model.format_list(models), spec)
        # An engine that cannot report a digest still has a name worth printing,
        # and printing "name=" for it would look like a missing value rather than none.
        self.assertEqual(model.format_list([model.Model(name="plain")]), "plain")
        self.assertEqual(model.format_list(None), "")
