// Package digest computes and caches weights-file digests.
//
// This is the one place Bothy may need to read model files instead of asking the
// engine, because only some engines can report a weights hash. Hashes are cached
// against size and mtime so a 40GB file is hashed once, not on every heartbeat.
package digest

import (
	"crypto/sha256"
	"encoding/hex"
	"io"
	"os"
	"sync"
	"time"
)

// File returns "sha256:<hex>" for the file at path.
func File(path string) (string, error) {
	f, err := os.Open(path)
	if err != nil {
		return "", err
	}
	defer f.Close()
	h := sha256.New()
	if _, err := io.Copy(h, f); err != nil {
		return "", err
	}
	return "sha256:" + hex.EncodeToString(h.Sum(nil)), nil
}

// Hasher remembers digests keyed by path, size and mtime.
type Hasher struct {
	mu    sync.Mutex
	cache map[string]cacheEntry
}

type cacheEntry struct {
	size    int64
	modTime time.Time
	digest  string
}

// NewHasher returns an empty cache.
func NewHasher() *Hasher { return &Hasher{cache: make(map[string]cacheEntry)} }

// File returns the digest of path, hashing it only when the cache is cold or the
// file changed since it was last hashed.
func (h *Hasher) File(path string) (string, error) {
	st, err := os.Stat(path)
	if err != nil {
		return "", err
	}
	h.mu.Lock()
	c, ok := h.cache[path]
	h.mu.Unlock()
	if ok && c.size == st.Size() && c.modTime.Equal(st.ModTime()) {
		return c.digest, nil
	}
	d, err := File(path)
	if err != nil {
		return "", err
	}
	h.mu.Lock()
	h.cache[path] = cacheEntry{size: st.Size(), modTime: st.ModTime(), digest: d}
	h.mu.Unlock()
	return d, nil
}
