package model

import "testing"

func TestNormalizeDigest(t *testing.T) {
	cases := map[string]string{
		"":               "",
		"ABCDEF":         "sha256:abcdef",
		"sha256:ABCDEF":  "sha256:abcdef",
		"  sha256:abc  ": "sha256:abc",
	}
	for in, want := range cases {
		if got := NormalizeDigest(in); got != want {
			t.Errorf("NormalizeDigest(%q) = %q, want %q", in, got, want)
		}
	}
}

// "unknown" must never read as "verified", or an engine that cannot report a
// digest would silently satisfy a pinned expectation.
func TestEqualDigestNeverMatchesUnknown(t *testing.T) {
	if EqualDigest("", "") {
		t.Fatal("two unknown digests must not compare as equal")
	}
	if !EqualDigest("sha256:ab", "AB") {
		t.Fatal("digests should compare ignoring case and the sha256: prefix")
	}
	if EqualDigest("sha256:ab", "sha256:cd") {
		t.Fatal("different digests must not compare as equal")
	}
}

func TestSameNameTreatsMissingTagAsLatest(t *testing.T) {
	if !SameName("llama3.1", "llama3.1:latest") {
		t.Fatal("a missing tag means :latest")
	}
	if SameName("llama3.1", "llama3.1:8b") {
		t.Fatal("different tags are different models")
	}
}

// A request without a tag has to reach a host that only offers tagged models,
// or "who has llama3.1?" would miss most of the network.
func TestMatches(t *testing.T) {
	cases := []struct {
		want, have string
		ok         bool
	}{
		{"", "llama3.1:8b", true},
		{"llama3.1", "llama3.1:8b", true},
		{"llama3.1", "llama3.1", true},
		{"llama3.1:latest", "llama3.1", true},
		{"llama3.1:8b", "llama3.1:8b", true},
		{"llama3.1:8b", "llama3.1", false},
		{"llama3.1:8b", "llama3.1:70b", false},
		{"llama3.1", "qwen2.5:7b", false},
	}
	for _, c := range cases {
		if got := Matches(c.want, c.have); got != c.ok {
			t.Errorf("Matches(%q, %q) = %v, want %v", c.want, c.have, got, c.ok)
		}
	}
}

func TestParseList(t *testing.T) {
	got, err := ParseList("llama3.1:8b=sha256:aa, plain ")
	if err != nil {
		t.Fatal(err)
	}
	if len(got) != 2 {
		t.Fatalf("parsed %d models, want 2", len(got))
	}
	if got[0].Name != "llama3.1:8b" || got[0].Digest != "sha256:aa" {
		t.Fatalf("first model = %+v", got[0])
	}
	if got[1].Name != "plain" || got[1].Digest != "" {
		t.Fatalf("second model = %+v, want a name with no digest", got[1])
	}

	if _, err := ParseList("=sha256:aa"); err == nil {
		t.Fatal("an item with no name should be an error")
	}
	if got, err := ParseList(""); err != nil || len(got) != 0 {
		t.Fatalf("ParseList(\"\") = %+v, %v; want no models and no error", got, err)
	}
}

// FormatList is how a host says in its log what it is serving, so it has to be
// the inverse of ParseList rather than something that merely looks similar.
func TestFormatListRoundTrips(t *testing.T) {
	const spec = "llama3.1:8b=sha256:aa,qwen2.5:7b=sha256:bb"
	models, err := ParseList(spec)
	if err != nil {
		t.Fatal(err)
	}
	if got := FormatList(models); got != spec {
		t.Fatalf("FormatList = %q, want %q", got, spec)
	}

	// An engine that cannot report a digest still has a name worth printing, and
	// printing "name=" for it would look like a missing value rather than none.
	if got := FormatList([]Model{{Name: "plain"}}); got != "plain" {
		t.Fatalf("FormatList = %q, want a bare name", got)
	}
	if got := FormatList(nil); got != "" {
		t.Fatalf("FormatList(nil) = %q, want empty", got)
	}
}
