package engine

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"

	"github.com/hibbault/bothy/internal/digest"
)

// writeManifest lays out an Ollama-style manifest for name:tag.
func writeManifest(t *testing.T, modelsDir, namespace, name, tag string, layers any) {
	t.Helper()
	dir := filepath.Join(modelsDir, "manifests", namespace, name)
	if err := os.MkdirAll(dir, 0o755); err != nil {
		t.Fatal(err)
	}
	body, err := json.Marshal(map[string]any{"layers": layers})
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, tag), body, 0o644); err != nil {
		t.Fatal(err)
	}
}

func tagsServer(t *testing.T, name, dig string) *httptest.Server {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/tags" {
			http.NotFound(w, r)
			return
		}
		_ = json.NewEncoder(w).Encode(map[string]any{
			"models": []map[string]any{{"name": name, "digest": dig}},
		})
	}))
	t.Cleanup(srv.Close)
	return srv
}

// The API reports the manifest digest, which changes whenever any layer does.
// The weights layer is the one that means "same model", so it must win.
func TestListModelsPrefersWeightsDigestOverManifestDigest(t *testing.T) {
	modelsDir := t.TempDir()
	const weights = "sha256:weightshash"
	writeManifest(t, modelsDir, "registry.ollama.ai", "library/llama3.1", "8b", []map[string]string{
		{"mediaType": "application/vnd.ollama.image.config", "digest": "sha256:confighash"},
		{"mediaType": modelLayerMediaType, "digest": weights},
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
	if len(models) != 1 || models[0].Digest != weights {
		t.Fatalf("models = %+v, want the weights layer digest %q", models, weights)
	}
}

func TestListModelsFallsBackToTheReportedDigest(t *testing.T) {
	srv := tagsServer(t, "llama3.1:8b", "sha256:manifesthash")
	lister, err := New("ollama", srv.URL, Options{})
	if err != nil {
		t.Fatal(err)
	}
	models, err := lister.ListModels(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if len(models) != 1 || models[0].Digest != "sha256:manifesthash" {
		t.Fatalf("models = %+v", models)
	}
}

// The default "auto" kind has to work against a real engine, which has no
// /internal/models route.
func TestAutoFallsBackToOllama(t *testing.T) {
	srv := tagsServer(t, "llama3.1:8b", "sha256:abc")
	lister, err := New("auto", srv.URL, Options{})
	if err != nil {
		t.Fatal(err)
	}
	models, err := lister.ListModels(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if len(models) != 1 || models[0].Name != "llama3.1:8b" {
		t.Fatalf("models = %+v", models)
	}
}

func TestManifestPathFindsAnotherNamespace(t *testing.T) {
	modelsDir := t.TempDir()
	writeManifest(t, modelsDir, "example.com/someone", "mistral", "7b", nil)

	if got := ManifestPath(modelsDir, "mistral:7b"); got == "" {
		t.Fatal("expected the walk to find a manifest outside the library namespace")
	}
	if got := ManifestPath(modelsDir, "absent:1b"); got != "" {
		t.Fatalf("ManifestPath for a missing model = %q, want empty", got)
	}
	if got := ManifestPath("", "mistral:7b"); got != "" {
		t.Fatalf("ManifestPath with no models dir = %q, want empty", got)
	}
}

func TestStaticKindHashesAWeightsFile(t *testing.T) {
	weights := filepath.Join(t.TempDir(), "model.gguf")
	if err := os.WriteFile(weights, []byte("weights"), 0o644); err != nil {
		t.Fatal(err)
	}
	lister, err := New("static", "http://unused", Options{WeightsPath: weights, WeightsModel: "local-gguf"})
	if err != nil {
		t.Fatal(err)
	}
	models, err := lister.ListModels(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	want, err := digest.File(weights)
	if err != nil {
		t.Fatal(err)
	}
	if len(models) != 1 || models[0].Name != "local-gguf" || models[0].Digest != want {
		t.Fatalf("models = %+v, want local-gguf with digest %q", models, want)
	}
}

func TestUnknownKindIsRejected(t *testing.T) {
	if _, err := New("nonsense", "http://engine", Options{}); err == nil {
		t.Fatal("expected an error for an unknown engine kind")
	}
	if _, err := New("ollama", "  ", Options{}); err == nil {
		t.Fatal("expected an error for a missing engine URL")
	}
}
