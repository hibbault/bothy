package engine

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/hibbault/bothy/internal/model"
)

// routeServer answers one route with one body, so each engine kind can be put in
// front of the shape it actually expects.
func routeServer(t *testing.T, path string, status int, body any) *httptest.Server {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != path {
			http.NotFound(w, r)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(status)
		if body != nil {
			_ = json.NewEncoder(w).Encode(body)
		}
	}))
	t.Cleanup(srv.Close)
	return srv
}

// Each kind reports the name that shows up in a host's logs and healthz, so a
// rename is a user-visible change rather than an internal one.
func TestEachKindReportsItsName(t *testing.T) {
	srv := routeServer(t, "/nowhere", http.StatusOK, nil)
	for kind, want := range map[string]string{
		"":       "auto",
		"auto":   "auto",
		"ollama": "ollama",
		"openai": "openai",
		"mock":   "mock",
		"static": "static",
	} {
		lister, err := New(kind, srv.URL, Options{Static: []model.Model{{Name: "m"}}})
		if err != nil {
			t.Fatalf("New(%q): %v", kind, err)
		}
		if got := lister.Kind(); got != want {
			t.Errorf("New(%q).Kind() = %q, want %q", kind, got, want)
		}
	}
}

func TestBaseURLIsNormalised(t *testing.T) {
	var paths []string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		paths = append(paths, r.URL.Path)
		_, _ = w.Write([]byte(`{"models":[]}`))
	}))
	t.Cleanup(srv.Close)

	// A trailing slash must not turn into //api/tags, which 404s on most servers.
	lister, err := New("ollama", "  "+srv.URL+"/  ", Options{})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := lister.ListModels(context.Background()); err != nil {
		t.Fatalf("ListModels: %v", err)
	}
	if len(paths) != 1 || paths[0] != "/api/tags" {
		t.Errorf("requested %v, want one clean /api/tags", paths)
	}
}

// The default kind probes for the mock's digest endpoint first. That is what
// makes a mock engine give real digests without configuring anything, and it has
// to win over the Ollama route when both exist.
func TestAutoPrefersTheMockDigestEndpoint(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/internal/models":
			_, _ = w.Write([]byte(`{"models":[{"name":"llama3.1:8b","digest":"sha256:frommock"}]}`))
		case "/api/tags":
			_, _ = w.Write([]byte(`{"models":[{"name":"llama3.1:8b","digest":"sha256:fromollama"}]}`))
		default:
			http.NotFound(w, r)
		}
	}))
	t.Cleanup(srv.Close)

	lister, err := New("auto", srv.URL, Options{})
	if err != nil {
		t.Fatal(err)
	}
	models, err := lister.ListModels(context.Background())
	if err != nil {
		t.Fatalf("ListModels: %v", err)
	}
	if len(models) != 1 || models[0].Digest != "sha256:frommock" {
		t.Fatalf("models = %+v, want the mock endpoint's digest", models)
	}
}

func TestMockKindReportsDigests(t *testing.T) {
	srv := routeServer(t, "/internal/models", http.StatusOK, map[string]any{
		"models": []map[string]string{{"name": "llama3.1:8b", "digest": "ABCDEF"}},
	})
	lister, err := New("mock", srv.URL, Options{})
	if err != nil {
		t.Fatal(err)
	}
	models, err := lister.ListModels(context.Background())
	if err != nil {
		t.Fatalf("ListModels: %v", err)
	}
	if len(models) != 1 || models[0].Digest != "sha256:abcdef" {
		t.Fatalf("models = %+v, want the digest normalised", models)
	}
}

func TestEngineFailuresAreReportedRatherThanTreatedAsEmpty(t *testing.T) {
	t.Run("ollama, a non-200", func(t *testing.T) {
		srv := routeServer(t, "/api/tags", http.StatusNotFound, nil)
		lister, _ := New("ollama", srv.URL, Options{})
		if _, err := lister.ListModels(context.Background()); err == nil {
			t.Fatal("a 404 from the engine was read as 'no models'")
		} else if !strings.Contains(err.Error(), "/api/tags") {
			t.Errorf("error %q does not say which endpoint failed", err)
		}
	})

	t.Run("ollama, a body that is not the documented shape", func(t *testing.T) {
		srv := routeServer(t, "/api/tags", http.StatusOK, map[string]any{"models": "nope"})
		lister, _ := New("ollama", srv.URL, Options{})
		if _, err := lister.ListModels(context.Background()); err == nil {
			t.Fatal("an undecodable body was accepted")
		}
	})

	t.Run("openai, a non-200", func(t *testing.T) {
		srv := routeServer(t, "/v1/models", http.StatusUnauthorized, nil)
		lister, _ := New("openai", srv.URL, Options{})
		if _, err := lister.ListModels(context.Background()); err == nil {
			t.Fatal("a 401 from the engine was read as 'no models'")
		} else if !strings.Contains(err.Error(), "/v1/models") {
			t.Errorf("error %q does not say which endpoint failed", err)
		}
	})

	t.Run("mock, an undecodable body", func(t *testing.T) {
		srv := routeServer(t, "/internal/models", http.StatusOK, map[string]any{"models": 7})
		lister, _ := New("mock", srv.URL, Options{})
		if _, err := lister.ListModels(context.Background()); err == nil {
			t.Fatal("an undecodable body was accepted")
		}
	})

	t.Run("a dead engine", func(t *testing.T) {
		srv := routeServer(t, "/api/tags", http.StatusOK, nil)
		addr := srv.URL
		srv.Close()
		lister, _ := New("ollama", addr, Options{})
		if _, err := lister.ListModels(context.Background()); err == nil {
			t.Fatal("a refused connection was read as 'no models'")
		} else if !strings.Contains(err.Error(), addr) {
			t.Errorf("error %q does not name the engine", err)
		}
	})
}

// vLLM and llama.cpp server list model ids and no digests at all. A digest
// configured by name is the only way those hosts can offer anything verifiable,
// and it must not be handed to a different size of the same model.
func TestOpenAIKindMergesConfiguredDigestsByName(t *testing.T) {
	srv := routeServer(t, "/v1/models", http.StatusOK, map[string]any{
		"object": "list",
		"data": []map[string]string{
			{"id": "llama3.1:8b"},
			{"id": "llama3.1:70b"},
			{"id": ""},
		},
	})

	lister, err := New("openai", srv.URL, Options{Static: []model.Model{
		{Name: "llama3.1:8b", Digest: "sha256:weights"},
	}})
	if err != nil {
		t.Fatal(err)
	}
	models, err := lister.ListModels(context.Background())
	if err != nil {
		t.Fatalf("ListModels: %v", err)
	}
	if len(models) != 2 {
		t.Fatalf("models = %+v, want the two named ids and not the empty one", models)
	}
	if models[0].Name != "llama3.1:8b" || models[0].Digest != "sha256:weights" {
		t.Errorf("models[0] = %+v, want the configured digest merged in", models[0])
	}
	if models[1].Digest != "" {
		t.Errorf("models[1] = %+v, want no digest: a configured :8b digest must not be applied to :70b", models[1])
	}
}

// digestFor compares the way model.SameName does, so a digest written for
// llama3.1 (no tag, meaning :latest) does satisfy a served llama3.1.
func TestDigestForUsesExactModelIdentity(t *testing.T) {
	list := []model.Model{
		{Name: "llama3.1", Digest: "sha256:latest-weights"},
		{Name: "qwen2.5:7b", Digest: "sha256:qwen"},
	}
	for _, tc := range []struct {
		name string
		want string
	}{
		{"llama3.1", "sha256:latest-weights"},
		{"llama3.1:latest", "sha256:latest-weights"},
		{"llama3.1:8b", ""},
		{"mistral", ""},
		// Model references are compared case-sensitively, like the digest they
		// are meant to pin: a differently-written name is a different model, and
		// silently borrowing a digest because the spelling is close is exactly
		// the mistake the digest exists to prevent.
		{"Qwen2.5:7B", ""},
	} {
		if got := digestFor(list, tc.name); got != tc.want {
			t.Errorf("digestFor(%q) = %q, want %q", tc.name, got, tc.want)
		}
	}
	if got := digestFor(nil, "llama3.1"); got != "" {
		t.Errorf("digestFor with no configured list = %q, want empty", got)
	}
}

// Ollama's /api/tags is inconsistent between versions about which field holds the
// name, and a row with neither is unusable rather than an empty-named model.
func TestOllamaAcceptsEitherNameFieldAndSkipsEmptyRows(t *testing.T) {
	srv := routeServer(t, "/api/tags", http.StatusOK, map[string]any{
		"models": []map[string]string{
			{"name": "llama3.1:8b", "digest": "sha256:one"},
			{"model": "qwen2.5:7b", "digest": "sha256:two"},
			{"name": "", "model": "", "digest": "sha256:three"},
		},
	})
	lister, err := New("ollama", srv.URL, Options{})
	if err != nil {
		t.Fatal(err)
	}
	models, err := lister.ListModels(context.Background())
	if err != nil {
		t.Fatalf("ListModels: %v", err)
	}
	if len(models) != 2 {
		t.Fatalf("models = %+v, want 2 (the unnamed row dropped)", models)
	}
	if models[1].Name != "qwen2.5:7b" {
		t.Errorf("models[1].Name = %q, want the model field used when name is absent", models[1].Name)
	}
}

// The static kind is the escape hatch, so its failures have to be plain ones at
// startup rather than a host that announces nothing.
func TestStaticKindRefusesConfigurationsItCannotServe(t *testing.T) {
	t.Run("no list and no weights file", func(t *testing.T) {
		lister, _ := New("static", "http://unused", Options{})
		_, err := lister.ListModels(context.Background())
		if err == nil {
			t.Fatal("a static engine with nothing configured was accepted")
		}
		if !strings.Contains(err.Error(), "BOTHY_MODELS") {
			t.Errorf("error %q does not say how to fix it", err)
		}
	})

	t.Run("a weights file that is not there", func(t *testing.T) {
		missing := filepath.Join(t.TempDir(), "absent.gguf")
		lister, _ := New("static", "http://unused", Options{
			WeightsPath:  missing,
			WeightsModel: "local",
		})
		_, err := lister.ListModels(context.Background())
		if err == nil {
			t.Fatal("a missing weights file was accepted")
		}
		if !strings.Contains(err.Error(), missing) {
			t.Errorf("error %q does not name the file", err)
		}
	})
}

func TestStaticKindAssignsTheHashedWeightsToTheRightModel(t *testing.T) {
	weights := filepath.Join(t.TempDir(), "model.gguf")
	if err := os.WriteFile(weights, []byte("some weights"), 0o644); err != nil {
		t.Fatal(err)
	}

	list := []model.Model{
		{Name: "local-gguf", Digest: "stale"},
		{Name: "other", Digest: "sha256:untouched"},
	}
	lister, err := New("static", "http://unused", Options{
		Static:       list,
		WeightsPath:  weights,
		WeightsModel: "local-gguf",
	})
	if err != nil {
		t.Fatal(err)
	}
	models, err := lister.ListModels(context.Background())
	if err != nil {
		t.Fatalf("ListModels: %v", err)
	}

	hashed := models[0].Digest
	if !strings.HasPrefix(hashed, "sha256:") || len(hashed) != len("sha256:")+64 {
		t.Fatalf("hashed digest = %q, want a full sha256", hashed)
	}
	if models[1].Digest != "sha256:untouched" {
		t.Errorf("an unrelated model's digest was overwritten: %+v", models[1])
	}
	// The configured list belongs to the caller; handing back the same slice
	// would let a later write mutate the operator's config.
	if list[0].Digest != "stale" {
		t.Errorf("the configured list was mutated in place: %+v", list)
	}
}

func TestManifestPathRejectsNamesItCannotUse(t *testing.T) {
	modelsDir := t.TempDir()
	for _, name := range []string{"", ":8b", "llama3.1:"} {
		if got := ManifestPath(modelsDir, name); got != "" {
			t.Errorf("ManifestPath(%q) = %q, want empty", name, got)
		}
	}
	if got := ManifestPath("", ""); got != "" {
		t.Errorf("ManifestPath with nothing = %q, want empty", got)
	}
}

// A manifest with no weights layer at all must not be mistaken for one whose
// weights are known: the fallback is the caller's business, not a guess here.
func TestManifestWithoutAWeightsLayerReportsNothing(t *testing.T) {
	modelsDir := t.TempDir()
	writeManifest(t, modelsDir, "registry.ollama.ai", "library/llama3.1", "8b", []map[string]string{
		{"mediaType": "application/vnd.ollama.image.config", "digest": "sha256:confighash"},
	})
	srv := tagsServer(t, "llama3.1:8b", "sha256:manifesthash")

	lister, err := New("ollama", srv.URL, Options{ModelsDir: modelsDir})
	if err != nil {
		t.Fatal(err)
	}
	models, err := lister.ListModels(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if len(models) != 1 || models[0].Digest != "sha256:manifesthash" {
		t.Fatalf("models = %+v, want the reported digest as the fallback", models)
	}
}
