"""Hashing a weights file, and remembering the answer.

Bothy prefers to ask an engine what weights it has, but only some engines can
answer; the rest of the fleet still needs a hash it can compare against, which
means this package has to read the file itself. A 40GB weights file cannot be
re-read on every heartbeat, so the answer is cached against size and mtime — and a
digest that outlives the file it describes would be worse than no digest at all.
Half of these tests are about that second half.

A digest is a promise about weights. A wrong one is worse than none: it would let
a host advertise something verifiable that is not there, so a path that cannot be
digested has to fail rather than answer with a digest of nothing.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import threading
import time
import unittest
from unittest import mock

from bothy import digest


class _TempDir(unittest.TestCase):
    """A throwaway directory per test, the counterpart of Go's `t.TempDir()`."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)

    def path(self, name):
        return os.path.join(self.dir, name)

    def write(self, path, data):
        with open(path, "wb") as f:
            f.write(data)


class TestFileHashesContent(_TempDir):
    def test_file_hashes_content(self):
        path = self.path("weights.bin")
        self.write(path, b"hello")
        want = "sha256:2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
        self.assertEqual(digest.file(path), want)


class TestHasherPicksUpAChangedFile(_TempDir):
    # The cache exists so a 40GB weights file is hashed once, but a rewritten file
    # must not keep its old digest — that would defeat the whole point.
    def test_hasher_picks_up_a_changed_file(self):
        path = self.path("weights.bin")
        self.write(path, b"first")
        h = digest.Hasher()
        first = h.file(path)
        # The second read is the cache's reason to exist, and it has to agree with
        # the first: a cache that answers differently is not a cache, it is a bug.
        self.assertEqual(h.file(path), first, "want the cached digest on a second read")

        # New content of the same size, with a new mtime: size alone would not
        # notice this, which is why both are part of the key.
        self.write(path, b"story")
        self._touch_forward(path)
        second = h.file(path)
        self.assertNotEqual(second, first, "the hasher returned a stale digest after the file changed")
        self.assertEqual(second, digest.file(path), "want a digest of what is on disk now")

        # And the other half of the key: new content of a different size, with the
        # old mtime put back so size is the only thing that changed.
        was = os.stat(path).st_mtime_ns
        self.write(path, b"second")
        os.utime(path, ns=(was, was))
        third = h.file(path)
        self.assertNotEqual(third, second, "the hasher missed a file that changed size")
        self.assertEqual(third, digest.file(path), "want a digest of what is on disk now")

    def _touch_forward(self, path):
        # The Go test moves the mtime two seconds ahead rather than trusting the
        # write to do it: filesystem timestamps are coarse enough that a rewrite in
        # the same tick can look unchanged, which would make this test lie.
        later = time.time() + 2
        os.utime(path, (later, later))


class TestHasherAnswersFromTheCache(_TempDir):
    # The promise is not "the same file hashed twice gives the same answer" — a
    # hasher with no cache at all keeps that one, and pays for it by re-reading a
    # 40GB file on every heartbeat. What has to hold is that an unchanged file is
    # not read again, so this test swaps the bytes underneath a key that still
    # matches: only the cache can answer with the old digest.
    def test_hasher_answers_from_the_cache(self):
        path = self.path("weights.bin")
        self.write(path, b"first")
        h = digest.Hasher()
        first = h.file(path)

        was = os.stat(path).st_mtime_ns
        self.write(path, b"story")  # same length, so size still matches
        os.utime(path, ns=(was, was))
        if os.stat(path).st_mtime_ns != was:
            self.skipTest("this filesystem cannot put an mtime back exactly")

        self.assertNotEqual(digest.file(path), first, "the test needs the bytes on disk to have really changed")
        self.assertEqual(
            h.file(path),
            first,
            "want the cached digest for a file whose size and mtime did not move",
        )


class TestHasherReportsMissingFile(_TempDir):
    def test_hasher_reports_missing_file(self):
        h = digest.Hasher()
        with self.assertRaises(FileNotFoundError):
            h.file(self.path("nope.gguf"))

        # A file that was hashed and has since been removed is gone, not remembered:
        # the cache is a memory of what was read, not a claim that it is still there.
        path = self.path("weights.bin")
        self.write(path, b"hello")
        h.file(path)
        os.remove(path)
        with self.assertRaises(FileNotFoundError):
            h.file(path)


class TestHasherDoesNotSerialiseUnrelatedFiles(_TempDir):
    # A weights file is hashed once and then never again — but someone always asks
    # first, and the ask that triggers the hash is the one that takes minutes. The
    # mutex is dropped across the hash so that a heartbeat asking about any other
    # path is answered while a 40GB file is still being read, which is the reason
    # the lock is not simply held for the whole method.
    def test_hashing_one_file_does_not_block_another(self):
        slow, other = self.path("slow.gguf"), self.path("other.gguf")
        self.write(slow, b"slow")
        self.write(other, b"other")

        real_file = digest.file
        started, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def parked(path):
            if path == slow:
                started.set()
                release.wait(5)
            return real_file(path)

        h = digest.Hasher()
        with mock.patch.object(digest, "file", parked):
            # The hash is the slow part of a big read, so that is the part this test
            # makes take forever.
            hashing = threading.Thread(target=h.file, args=(slow,))
            hashing.start()
            self.assertTrue(started.wait(5), "the hashing thread never started")

            waiting = threading.Thread(target=h.file, args=(other,))
            waiting.start()
            waiting.join(2)
            self.assertFalse(waiting.is_alive(), "an unrelated path had to wait for a hash to finish")

            release.set()
            hashing.join(5)


class TestFileRefusesPathsItCannotDigest(_TempDir):
    # File is called on a path that came out of configuration, so a missing file has
    # to come back as an error that names it rather than as a digest of nothing. A
    # digest is a promise about weights, and a wrong one is worse than none: it would
    # let a host advertise something verifiable that is not there.
    #
    # Python raises where Go returns, so the Go test's "no digest alongside the
    # error" check has no counterpart here: an exception is the whole answer.
    def test_file_refuses_paths_it_cannot_digest(self):
        missing = self.path("absent.gguf")
        with self.assertRaises(FileNotFoundError, msg="a missing file must not have a digest") as caught:
            digest.file(missing)
        self.assertIn("absent.gguf", str(caught.exception), "the error has to name the path it could not read")

        # A directory opens happily on some systems and then fails to read. The
        # distinction matters: a models-dir pointed one level off must not yield a
        # digest of whatever little was readable.
        with self.assertRaises(OSError):
            digest.file(self.dir)
