package digest

import (
	"os"
	"path/filepath"
	"testing"
	"time"
)

func TestFileHashesContent(t *testing.T) {
	path := filepath.Join(t.TempDir(), "weights.bin")
	if err := os.WriteFile(path, []byte("hello"), 0o644); err != nil {
		t.Fatal(err)
	}
	const want = "sha256:2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
	got, err := File(path)
	if err != nil {
		t.Fatal(err)
	}
	if got != want {
		t.Fatalf("File() = %q, want %q", got, want)
	}
}

// The cache exists so a 40GB weights file is hashed once, but a rewritten file
// must not keep its old digest — that would defeat the whole point.
func TestHasherPicksUpAChangedFile(t *testing.T) {
	path := filepath.Join(t.TempDir(), "weights.bin")
	if err := os.WriteFile(path, []byte("first"), 0o644); err != nil {
		t.Fatal(err)
	}
	h := NewHasher()
	first, err := h.File(path)
	if err != nil {
		t.Fatal(err)
	}
	if again, err := h.File(path); err != nil || again != first {
		t.Fatalf("second read = %q, %v; want the cached digest %q", again, err, first)
	}

	if err := os.WriteFile(path, []byte("second"), 0o644); err != nil {
		t.Fatal(err)
	}
	later := time.Now().Add(2 * time.Second)
	if err := os.Chtimes(path, later, later); err != nil {
		t.Fatal(err)
	}
	second, err := h.File(path)
	if err != nil {
		t.Fatal(err)
	}
	if second == first {
		t.Fatal("the hasher returned a stale digest after the file changed")
	}
}

func TestHasherReportsMissingFile(t *testing.T) {
	if _, err := NewHasher().File(filepath.Join(t.TempDir(), "nope.gguf")); err == nil {
		t.Fatal("expected an error for a missing weights file")
	}
}
