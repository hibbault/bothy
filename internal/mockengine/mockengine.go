// Package mockengine is a stand-in for a real inference engine.
//
// It exists so the whole stack can run in CI and on a laptop with no GPU: it
// speaks enough of the OpenAI and Ollama APIs for Bothy and ordinary clients to
// work against it, and it reports whatever digest you configure. That last part
// is what makes the digest-mismatch path testable without downloading the same
// 8GB model twice.
package mockengine

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"log/slog"
	"net/http"
	"strings"
	"time"

	"github.com/hibbault/bothy/internal/config"
	"github.com/hibbault/bothy/internal/httpx"
	"github.com/hibbault/bothy/internal/model"
)

// Obviously-fake digests, so a mock's output can never be mistaken for a real
// weights hash. Full length, so they behave like real ones.
const (
	digestA = "sha256:1111111111111111111111111111111111111111111111111111111111111111"
	digestB = "sha256:2222222222222222222222222222222222222222222222222222222222222222"
)

var defaultModels = "llama3.1:8b=" + digestA + ",qwen2.5:7b=" + digestB

// Config describes one mock engine.
type Config struct {
	// Name identifies this engine in its replies, so two mocks are
	// distinguishable when you are looking at the output.
	Name   string
	Models []model.Model
	// Delay is an artificial pause between streamed chunks, for making latency
	// visible in a demo.
	Delay time.Duration
}

// Server is the mock engine's HTTP surface.
type Server struct {
	Config Config
	Log    *slog.Logger
}

// Run parses flags for the "mock" command and serves until ctx is cancelled.
func Run(ctx context.Context, log *slog.Logger, args []string) error {
	fs := flag.NewFlagSet("mock", flag.ExitOnError)
	listen := fs.String("listen", config.Str("BOTHY_LISTEN", ":11434"), "address to listen on")
	name := fs.String("name", config.Str("BOTHY_MOCK_NAME", "mock"), "name this engine reports, so two mocks are tellable apart")
	modelsFlag := fs.String("models", config.Str("BOTHY_MOCK_MODELS", defaultModels), "models as name=digest,name=digest")
	delay := fs.Duration("delay", config.Dur("BOTHY_MOCK_DELAY", 0), "pause between streamed chunks, to make latency visible")
	if err := fs.Parse(args); err != nil {
		return err
	}
	models, err := model.ParseList(*modelsFlag)
	if err != nil {
		return err
	}
	if len(models) == 0 {
		return fmt.Errorf("no models configured")
	}
	s := &Server{Config: Config{Name: *name, Models: models, Delay: *delay}, Log: log}
	log.Info("mock engine ready", "name", *name, "models", model.FormatList(models))
	return httpx.Serve(ctx, *listen, s.Handler(), log)
}

// Handler returns the engine's routes, which mirror the real ones closely enough
// that a client cannot tell the difference.
func (s *Server) Handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", s.handleHealth)
	mux.HandleFunc("GET /internal/models", s.handleInternalModels)
	mux.HandleFunc("GET /api/tags", s.handleTags)
	mux.HandleFunc("GET /v1/models", s.handleOpenAIModels)
	mux.HandleFunc("POST /v1/chat/completions", s.handleChat)
	mux.HandleFunc("POST /v1/completions", s.handleCompletions)
	return mux
}

func (s *Server) handleHealth(w http.ResponseWriter, _ *http.Request) {
	httpx.JSON(w, http.StatusOK, map[string]any{"ok": true, "engine": "mock", "name": s.Config.Name})
}

// handleInternalModels is Bothy's own digest endpoint. A real engine has no such
// route; this is why the mock is only useful for testing.
func (s *Server) handleInternalModels(w http.ResponseWriter, _ *http.Request) {
	httpx.JSON(w, http.StatusOK, map[string]any{"models": s.Config.Models})
}

// handleTags mirrors Ollama's /api/tags.
func (s *Server) handleTags(w http.ResponseWriter, _ *http.Request) {
	type tag struct {
		Name       string `json:"name"`
		Model      string `json:"model"`
		ModifiedAt string `json:"modified_at"`
		Size       int    `json:"size"`
		Digest     string `json:"digest"`
	}
	out := make([]tag, 0, len(s.Config.Models))
	for _, m := range s.Config.Models {
		out = append(out, tag{
			Name:       m.Name,
			Model:      m.Name,
			ModifiedAt: time.Now().UTC().Format(time.RFC3339),
			Size:       0,
			Digest:     m.Digest,
		})
	}
	httpx.JSON(w, http.StatusOK, map[string]any{"models": out})
}

// handleOpenAIModels mirrors GET /v1/models.
func (s *Server) handleOpenAIModels(w http.ResponseWriter, _ *http.Request) {
	type card struct {
		ID      string `json:"id"`
		Object  string `json:"object"`
		Created int64  `json:"created"`
		OwnedBy string `json:"owned_by"`
	}
	out := make([]card, 0, len(s.Config.Models))
	for _, m := range s.Config.Models {
		out = append(out, card{ID: m.Name, Object: "model", Created: 0, OwnedBy: "bothy-mock"})
	}
	httpx.JSON(w, http.StatusOK, map[string]any{"object": "list", "data": out})
}

type message struct {
	Role    string `json:"role"`
	Content any    `json:"content"`
}

type chatRequest struct {
	Model    string    `json:"model"`
	Stream   bool      `json:"stream"`
	Messages []message `json:"messages"`
	Prompt   any       `json:"prompt"`
	// An OpenAI-compatible engine reports usage on a stream only when the
	// request asks for it. The mock does the same, deliberately: it is the worse
	// of the two real behaviours, and the one the host has to work around.
	StreamOptions *struct {
		IncludeUsage bool `json:"include_usage"`
	} `json:"stream_options"`
}

// wantsUsage reports whether the request asked for usage on a stream.
func (r chatRequest) wantsUsage() bool {
	return r.StreamOptions != nil && r.StreamOptions.IncludeUsage
}

// handleChat serves POST /v1/chat/completions, streaming when asked.
func (s *Server) handleChat(w http.ResponseWriter, r *http.Request) {
	var req chatRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		httpx.Error(w, http.StatusBadRequest, "invalid JSON body: "+err.Error())
		return
	}
	prompt := lastUserMessage(req.Messages)
	reply := s.reply(req.Model, prompt)
	if req.Stream {
		s.streamChat(r.Context(), w, req.Model, prompt, reply, req.wantsUsage())
		return
	}
	httpx.JSON(w, http.StatusOK, map[string]any{
		"id":      "chatcmpl-mock",
		"object":  "chat.completion",
		"created": time.Now().Unix(),
		"model":   req.Model,
		"choices": []any{map[string]any{
			"index":         0,
			"message":       map[string]any{"role": "assistant", "content": reply},
			"finish_reason": "stop",
		}},
		"usage": usage(prompt, reply),
	})
}

// handleCompletions serves the older POST /v1/completions, because plenty of
// tools still use it.
func (s *Server) handleCompletions(w http.ResponseWriter, r *http.Request) {
	var req chatRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		httpx.Error(w, http.StatusBadRequest, "invalid JSON body: "+err.Error())
		return
	}
	prompt := textOf(req.Prompt)
	reply := s.reply(req.Model, prompt)
	if req.Stream {
		s.streamCompletion(r.Context(), w, req.Model, prompt, reply, req.wantsUsage())
		return
	}
	httpx.JSON(w, http.StatusOK, map[string]any{
		"id":      "cmpl-mock",
		"object":  "text_completion",
		"created": time.Now().Unix(),
		"model":   req.Model,
		"choices": []any{map[string]any{"index": 0, "text": reply, "finish_reason": "stop"}},
		"usage":   usage(prompt, reply),
	})
}

// reply is deterministic, and names the engine and digest that produced it —
// which is how you tell, from the client side, whose GPU actually answered.
func (s *Server) reply(reqModel, prompt string) string {
	var d string
	for _, m := range s.Config.Models {
		if model.SameName(m.Name, reqModel) {
			d = m.Digest
		}
	}
	if d == "" {
		d = "unknown-model"
	}
	if prompt == "" {
		prompt = "(empty prompt)"
	}
	return fmt.Sprintf("[%s] model=%s digest=%s you-said=%q", s.Config.Name, reqModel, d, prompt)
}

func (s *Server) streamChat(ctx context.Context, w http.ResponseWriter, reqModel, prompt, text string, includeUsage bool) {
	flusher, ok := w.(http.Flusher)
	if !ok {
		httpx.Error(w, http.StatusInternalServerError, "streaming unsupported by this server")
		return
	}
	w.Header().Set("Content-Type", "text/event-stream")
	w.Header().Set("Cache-Control", "no-cache")
	w.WriteHeader(http.StatusOK)

	for i, word := range strings.Fields(text) {
		delta := word
		if i > 0 {
			delta = " " + word
		}
		writeChunk(w, flusher, "chat.completion.chunk", reqModel, delta, nil, nil)
		if !s.pause(ctx) {
			return
		}
	}
	// The closing frame carries the usage when it was asked for. A host that
	// wants to meter a stream has to send stream_options.include_usage; this is
	// the behaviour it is compensating for.
	var reported map[string]any
	if includeUsage {
		reported = usage(prompt, text)
	}
	writeChunk(w, flusher, "chat.completion.chunk", reqModel, "", "stop", reported)
	fmt.Fprint(w, "data: [DONE]\n\n")
	flusher.Flush()
}

func (s *Server) streamCompletion(ctx context.Context, w http.ResponseWriter, reqModel, prompt, text string, includeUsage bool) {
	flusher, ok := w.(http.Flusher)
	if !ok {
		httpx.Error(w, http.StatusInternalServerError, "streaming unsupported by this server")
		return
	}
	w.Header().Set("Content-Type", "text/event-stream")
	w.Header().Set("Cache-Control", "no-cache")
	w.WriteHeader(http.StatusOK)

	for i, word := range strings.Fields(text) {
		delta := word
		if i > 0 {
			delta = " " + word
		}
		payload, err := json.Marshal(map[string]any{
			"id":      "cmpl-mock",
			"object":  "text_completion",
			"created": time.Now().Unix(),
			"model":   reqModel,
			"choices": []any{map[string]any{"index": 0, "text": delta, "finish_reason": nil}},
		})
		if err != nil {
			return
		}
		fmt.Fprintf(w, "data: %s\n\n", payload)
		flusher.Flush()
		if !s.pause(ctx) {
			return
		}
	}
	// Same reasoning as streamChat: a closing frame, carrying usage when the
	// request asked for it.
	closing := map[string]any{
		"id":      "cmpl-mock",
		"object":  "text_completion",
		"created": time.Now().Unix(),
		"model":   reqModel,
		"choices": []any{map[string]any{"index": 0, "text": "", "finish_reason": "stop"}},
	}
	if includeUsage {
		closing["usage"] = usage(prompt, text)
	}
	final, err := json.Marshal(closing)
	if err == nil {
		fmt.Fprintf(w, "data: %s\n\n", final)
		flusher.Flush()
	}
	fmt.Fprint(w, "data: [DONE]\n\n")
	flusher.Flush()
}

// pause sleeps between chunks, returning false if the caller went away.
func (s *Server) pause(ctx context.Context) bool {
	if s.Config.Delay <= 0 {
		return ctx.Err() == nil
	}
	select {
	case <-ctx.Done():
		return false
	case <-time.After(s.Config.Delay):
		return true
	}
}

// writeChunk emits one SSE frame. usage is only attached when non-nil, so the
// delta frames stay the shape clients already expect.
func writeChunk(w http.ResponseWriter, f http.Flusher, object, reqModel, delta string, finish any, usage map[string]any) {
	frame := map[string]any{
		"id":      "chatcmpl-mock",
		"object":  object,
		"created": time.Now().Unix(),
		"model":   reqModel,
		"choices": []any{map[string]any{
			"index":         0,
			"delta":         map[string]any{"content": delta},
			"finish_reason": finish,
		}},
	}
	if usage != nil {
		frame["usage"] = usage
	}
	payload, err := json.Marshal(frame)
	if err != nil {
		return
	}
	fmt.Fprintf(w, "data: %s\n\n", payload)
	f.Flush()
}

// lastUserMessage returns the final user turn, which is what a real engine would
// answer.
func lastUserMessage(messages []message) string {
	for i := len(messages) - 1; i >= 0; i-- {
		if messages[i].Role == "user" || messages[i].Role == "" {
			return textOf(messages[i].Content)
		}
	}
	if len(messages) > 0 {
		return textOf(messages[len(messages)-1].Content)
	}
	return ""
}

// textOf flattens an OpenAI content field, which may be a plain string or a list
// of typed parts.
func textOf(v any) string {
	switch t := v.(type) {
	case string:
		return t
	case []any:
		var b strings.Builder
		for _, part := range t {
			m, ok := part.(map[string]any)
			if !ok {
				continue
			}
			if s, ok := m["text"].(string); ok {
				b.WriteString(s)
			}
		}
		return b.String()
	}
	return ""
}

// usage reports a rough word count, which is all a mock needs to look plausible.
func usage(prompt, reply string) map[string]any {
	p := len(strings.Fields(prompt))
	c := len(strings.Fields(reply))
	return map[string]any{"prompt_tokens": p, "completion_tokens": c, "total_tokens": p + c}
}
