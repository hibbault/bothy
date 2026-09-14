// Package engine reports which models an inference engine can serve, with a
// digest for each.
//
// Bothy does not implement inference. It discovers what is already running and
// proxies to it, so "adding an engine" means teaching this package to describe
// one — never touching the data path.
package engine

import (
	"context"
	"encoding/json"
	"fmt"
	"io/fs"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/hibbault/bothy/internal/digest"
	"github.com/hibbault/bothy/internal/model"
)

// modelLayerMediaType is the manifest layer that holds the weights. Its digest
// is the SHA-256 of the weights blob — not the manifest digest an engine's API
// reports, which changes whenever any layer does.
const modelLayerMediaType = "application/vnd.ollama.image.model"

// Lister reports the models an engine can serve.
type Lister interface {
	ListModels(ctx context.Context) ([]model.Model, error)
	Kind() string
}

// Options tune how digests are resolved.
type Options struct {
	// ModelsDir is the Ollama models directory, usually ~/.ollama/models. When
	// set, digests come from the model manifest rather than the API, which is
	// the difference between a real weights hash and a manifest hash.
	ModelsDir string
	// WeightsPath is a single weights file (.gguf, .safetensors) to hash, for
	// engines that cannot report a digest at all.
	WeightsPath string
	// WeightsModel is the model name WeightsPath belongs to.
	WeightsModel string
	// Static is the explicit model list, used by the "static" kind and to supply
	// digests for engines that list models without them.
	Static []model.Model
}

// New returns a Lister for kind.
//
// "auto" probes the engine, and is the default so one host config works against
// Ollama, the mock engine, and anything else that looks like either.
func New(kind, baseURL string, opts Options) (Lister, error) {
	base := strings.TrimRight(strings.TrimSpace(baseURL), "/")
	if base == "" {
		return nil, fmt.Errorf("engine URL is required")
	}
	httpClient := &http.Client{Timeout: 20 * time.Second}
	ollamaLister := &ollama{base: base, http: httpClient, opts: opts, hasher: digest.NewHasher()}
	mockLister := &mock{base: base, http: httpClient}
	switch kind {
	case "", "auto":
		return &auto{ollama: ollamaLister, mock: mockLister}, nil
	case "ollama":
		return ollamaLister, nil
	case "openai":
		return &openai{base: base, http: httpClient, opts: opts}, nil
	case "mock":
		return mockLister, nil
	case "static":
		return &static{opts: opts, hasher: digest.NewHasher()}, nil
	default:
		return nil, fmt.Errorf("unknown engine kind %q (want auto, ollama, openai, mock or static)", kind)
	}
}

// auto probes for a mock engine's digest endpoint first, then falls back to
// Ollama's API. It re-probes on every call rather than caching, so a container
// race at startup heals itself without a restart.
type auto struct {
	ollama *ollama
	mock   *mock
}

func (a *auto) Kind() string { return "auto" }

func (a *auto) ListModels(ctx context.Context) ([]model.Model, error) {
	if models, err := a.mock.ListModels(ctx); err == nil {
		return models, nil
	}
	return a.ollama.ListModels(ctx)
}

// mock reads the digest endpoint a mock engine exposes.
type mock struct {
	base string
	http *http.Client
}

func (m *mock) Kind() string { return "mock" }

func (m *mock) ListModels(ctx context.Context) ([]model.Model, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, m.base+"/internal/models", nil)
	if err != nil {
		return nil, err
	}
	resp, err := m.http.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("%s/internal/models: %s", m.base, resp.Status)
	}
	var payload struct {
		Models []model.Model `json:"models"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&payload); err != nil {
		return nil, err
	}
	out := make([]model.Model, 0, len(payload.Models))
	for _, mm := range payload.Models {
		mm.Digest = model.NormalizeDigest(mm.Digest)
		out = append(out, mm)
	}
	return out, nil
}

// ollama lists models from Ollama's native API.
type ollama struct {
	base   string
	http   *http.Client
	opts   Options
	hasher *digest.Hasher
}

func (o *ollama) Kind() string { return "ollama" }

func (o *ollama) ListModels(ctx context.Context) ([]model.Model, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, o.base+"/api/tags", nil)
	if err != nil {
		return nil, err
	}
	resp, err := o.http.Do(req)
	if err != nil {
		return nil, fmt.Errorf("engine %s: %w", o.base, err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("engine %s: /api/tags: %s", o.base, resp.Status)
	}
	var payload struct {
		Models []struct {
			Name   string `json:"name"`
			Model  string `json:"model"`
			Digest string `json:"digest"`
		} `json:"models"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&payload); err != nil {
		return nil, fmt.Errorf("engine %s: decode /api/tags: %w", o.base, err)
	}
	out := make([]model.Model, 0, len(payload.Models))
	for _, m := range payload.Models {
		name := m.Name
		if name == "" {
			name = m.Model
		}
		if name == "" {
			continue
		}
		dig := model.NormalizeDigest(m.Digest)
		if w, ok := o.manifestDigest(name); ok {
			dig = w
		}
		out = append(out, model.Model{Name: name, Digest: dig})
	}
	return out, nil
}

// manifestDigest returns the digest of the weights layer in the manifest for
// name, when the models directory is readable.
func (o *ollama) manifestDigest(name string) (string, bool) {
	if o.opts.ModelsDir == "" {
		return "", false
	}
	path := ManifestPath(o.opts.ModelsDir, name)
	if path == "" {
		return "", false
	}
	b, err := os.ReadFile(path)
	if err != nil {
		return "", false
	}
	var manifest struct {
		Layers []struct {
			MediaType string `json:"mediaType"`
			Digest    string `json:"digest"`
		} `json:"layers"`
	}
	if err := json.Unmarshal(b, &manifest); err != nil {
		return "", false
	}
	for _, layer := range manifest.Layers {
		if layer.MediaType == modelLayerMediaType {
			return model.NormalizeDigest(layer.Digest), true
		}
	}
	return "", false
}

// ManifestPath finds the manifest file for name:tag under a models directory.
// It tries the default library namespace directly, then walks, so models pulled
// from another namespace still resolve.
func ManifestPath(modelsDir, name string) string {
	if modelsDir == "" || name == "" {
		return ""
	}
	base, tag := name, "latest"
	if i := strings.LastIndex(name, ":"); i > 0 {
		base, tag = name[:i], name[i+1:]
	}
	if i := strings.LastIndex(base, "/"); i >= 0 {
		base = base[i+1:]
	}
	if base == "" || tag == "" {
		return ""
	}
	direct := filepath.Join(modelsDir, "manifests", "registry.ollama.ai", "library", base, tag)
	if _, err := os.Stat(direct); err == nil {
		return direct
	}
	var found string
	_ = filepath.WalkDir(filepath.Join(modelsDir, "manifests"), func(p string, d fs.DirEntry, err error) error {
		if err != nil || d.IsDir() {
			return nil
		}
		if filepath.Base(p) != tag || filepath.Base(filepath.Dir(p)) != base {
			return nil
		}
		found = p
		return fs.SkipAll
	})
	return found
}

// openai lists models from an OpenAI-compatible /v1/models endpoint, which is
// what vLLM and llama.cpp server expose. Those endpoints report no digest, so
// any digest configured by name is merged in; without one, the host registers an
// empty digest and clients cannot verify it.
type openai struct {
	base string
	http *http.Client
	opts Options
}

func (o *openai) Kind() string { return "openai" }

func (o *openai) ListModels(ctx context.Context) ([]model.Model, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, o.base+"/v1/models", nil)
	if err != nil {
		return nil, err
	}
	resp, err := o.http.Do(req)
	if err != nil {
		return nil, fmt.Errorf("engine %s: %w", o.base, err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("engine %s: /v1/models: %s", o.base, resp.Status)
	}
	var payload struct {
		Data []struct {
			ID string `json:"id"`
		} `json:"data"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&payload); err != nil {
		return nil, fmt.Errorf("engine %s: decode /v1/models: %w", o.base, err)
	}
	out := make([]model.Model, 0, len(payload.Data))
	for _, d := range payload.Data {
		if d.ID == "" {
			continue
		}
		out = append(out, model.Model{Name: d.ID, Digest: digestFor(o.opts.Static, d.ID)})
	}
	return out, nil
}

// digestFor returns the configured digest for name, if any.
func digestFor(list []model.Model, name string) string {
	for _, m := range list {
		if model.SameName(m.Name, name) {
			return model.NormalizeDigest(m.Digest)
		}
	}
	return ""
}

// static serves a model list the operator wrote down. It is the escape hatch for
// engines Bothy cannot introspect, and the only kind that hashes a weights file.
type static struct {
	opts   Options
	hasher *digest.Hasher
}

func (s *static) Kind() string { return "static" }

func (s *static) ListModels(_ context.Context) ([]model.Model, error) {
	out := make([]model.Model, len(s.opts.Static))
	copy(out, s.opts.Static)
	if s.opts.WeightsPath == "" {
		if len(out) == 0 {
			return nil, fmt.Errorf("engine kind static needs a model list (BOTHY_MODELS)")
		}
		return out, nil
	}
	d, err := s.hasher.File(s.opts.WeightsPath)
	if err != nil {
		return nil, fmt.Errorf("hash %s: %w", s.opts.WeightsPath, err)
	}
	switch {
	case len(out) == 0:
		out = []model.Model{{Name: s.opts.WeightsModel, Digest: d}}
	case len(out) == 1:
		out[0].Digest = d
	default:
		for i, m := range out {
			if model.SameName(m.Name, s.opts.WeightsModel) {
				out[i].Digest = d
			}
		}
	}
	return out, nil
}
