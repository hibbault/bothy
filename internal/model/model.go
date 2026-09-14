// Package model is the shared vocabulary: a servable model, and the identity of
// its weights.
//
// The same tag means different weights on different machines — a different
// quantization, a fine-tune, a stale pull. The digest is what makes "the same
// model" mean something, so it travels with every model record.
package model

import (
	"fmt"
	"strings"
)

// Model is a servable model. Digest is the SHA-256 of the weights file.
type Model struct {
	Name   string `json:"name"`
	Digest string `json:"digest"`
}

// NormalizeDigest makes digests comparable: lowercase, with a sha256: prefix.
func NormalizeDigest(s string) string {
	s = strings.TrimSpace(strings.ToLower(s))
	if s == "" {
		return ""
	}
	if strings.HasPrefix(s, "sha256:") {
		return s
	}
	return "sha256:" + s
}

// EqualDigest reports whether two digests identify the same weights. Two empty
// digests are not equal — "unknown" must never read as "verified".
func EqualDigest(a, b string) bool {
	na, nb := NormalizeDigest(a), NormalizeDigest(b)
	return na != "" && na == nb
}

// SameName reports whether two model names denote the same exact reference,
// treating a missing tag as :latest the way Ollama does. Use it to compare two
// names that are supposed to be the same model; use Matches to decide whether a
// model on offer satisfies a request.
func SameName(a, b string) bool {
	abase, atag, _ := splitTag(a)
	bbase, btag, _ := splitTag(b)
	if atag == "" {
		atag = "latest"
	}
	if btag == "" {
		btag = "latest"
	}
	return abase == bbase && atag == btag
}

// Matches reports whether a model named have satisfies a request for want.
//
// A request with no tag matches any tag, because "who has llama3.1?" should not
// miss a host offering llama3.1:8b. A request with a tag is exact, so asking for
// :8b never silently returns a different size. An empty request matches
// everything.
func Matches(want, have string) bool {
	if strings.TrimSpace(want) == "" {
		return true
	}
	if _, _, tagged := splitTag(want); tagged {
		return SameName(want, have)
	}
	wbase, _, _ := splitTag(want)
	hbase, _, _ := splitTag(have)
	return wbase == hbase
}

// splitTag splits "name:tag". A name with no tag reports tagged = false, which
// keeps "no tag" distinguishable from an explicit request for :latest.
func splitTag(name string) (base, tag string, tagged bool) {
	name = strings.TrimSpace(name)
	if i := strings.LastIndex(name, ":"); i > 0 {
		return name[:i], name[i+1:], true
	}
	return name, "", false
}

// ParseList parses "name=digest,name2=digest2". The digest is optional, for
// engines that cannot report one (llama.cpp, vLLM).
func ParseList(s string) ([]Model, error) {
	var out []Model
	for _, item := range strings.Split(s, ",") {
		item = strings.TrimSpace(item)
		if item == "" {
			continue
		}
		name, dig, hasDigest := strings.Cut(item, "=")
		name = strings.TrimSpace(name)
		if name == "" {
			return nil, fmt.Errorf("bad model list item %q: missing name", item)
		}
		m := Model{Name: name}
		if hasDigest {
			m.Digest = NormalizeDigest(dig)
		}
		out = append(out, m)
	}
	return out, nil
}

// FormatList renders models back into the name=digest form, for logging.
func FormatList(models []Model) string {
	parts := make([]string, 0, len(models))
	for _, m := range models {
		if m.Digest == "" {
			parts = append(parts, m.Name)
			continue
		}
		parts = append(parts, m.Name+"="+m.Digest)
	}
	return strings.Join(parts, ",")
}
