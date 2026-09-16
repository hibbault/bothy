"""Computing and caching weights-file digests.

This is the one place Bothy may need to read model files instead of asking the
engine, because only some engines can report a weights hash. Hashes are cached
against size and mtime so a 40GB file is hashed once, not on every heartbeat.

The key is size and mtime rather than the digest for the obvious reason: the whole
point is to avoid computing the digest. What that costs is a stat per ask, which is
free next to reading a weights file, and what it buys is that a file someone
replaced under the same name is hashed again instead of being vouched for.

A path that cannot be read raises the operating system's own error, as Go returns
the `*os.PathError` from `os.Open` unchanged: `FileNotFoundError` here is Python's
`os.ErrNotExist`, and a caller that asks "was it not there, or was it not
readable?" has to still be able to tell. This package defines no error of its own,
so there is nothing of Bothy's to wrap it in — a digest is a promise about weights,
and the promise is only as good as the read that produced it.
"""

from __future__ import annotations

import hashlib
import os
import threading
from dataclasses import dataclass
from typing import Dict

# The read size used to stream a file through the hash. Go's io.Copy uses 32KiB;
# weights files are measured in gigabytes, so a megabyte per read means a few
# thousand syscalls per gigabyte instead of tens of thousands.
_CHUNK = 1 << 20


def file(path: str) -> str:
    """Return "sha256:<hex>" for the file at path."""
    h = hashlib.sha256()
    buf = bytearray(_CHUNK)
    view = memoryview(buf)
    with open(path, "rb") as f:
        while True:
            n = f.readinto(buf)
            if not n:
                break
            h.update(view[:n])
    return "sha256:" + h.hexdigest()


@dataclass(frozen=True)
class _CacheEntry:
    """What was hashed, not the contents.

    Go records the file's `time.Time`; nanoseconds are the same comparison, at the
    resolution both the filesystem and `st_mtime_ns` offer, without a float that
    would lose precision this far from the epoch.
    """

    size: int
    mod_time_ns: int
    digest: str


class Hasher:
    """Remembers digests keyed by path, size and mtime."""

    def __init__(self) -> None:
        self._mu = threading.Lock()
        self._cache: Dict[str, _CacheEntry] = {}

    def file(self, path: str) -> str:
        """Return the digest of path, hashing it only when the cache is cold or the
        file changed since it was last hashed.

        The stat comes first and unconditionally, so a file that no longer exists is
        an error even when its digest is cached — the cache is a memory of what was
        read, not a claim that it is still there.
        """
        st = os.stat(path)
        with self._mu:
            entry = self._cache.get(path)
        if entry is not None and entry.size == st.st_size and entry.mod_time_ns == st.st_mtime_ns:
            return entry.digest

        # The lock is deliberately not held across the hash itself. A cold 40GB file
        # takes minutes to read, and every heartbeat that wanted any digest in the
        # meantime would queue behind it — for an answer only the hash can give, and
        # which it can only give at the end. Released, the worst case is that two
        # threads hash the same cold file and then agree on the result.
        d = file(path)
        with self._mu:
            self._cache[path] = _CacheEntry(size=st.st_size, mod_time_ns=st.st_mtime_ns, digest=d)
        return d


def new_hasher() -> Hasher:
    """Return an empty cache.

    Go spells this `NewHasher` because a zero `Hasher` there has a nil map; Python
    does not need a constructor, so this is only that name kept — `Hasher()` is the
    same object.
    """
    return Hasher()
