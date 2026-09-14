// Package discovery is the registry service: hosts announce what they serve,
// clients ask who has it.
//
// It is deliberately dumb. It cannot check that an address is reachable, and it
// cannot check that a digest is honest, so it is a bulletin board rather than an
// authority. The one thing it does enforce is expiry, so hosts that stopped
// heartbeating fall out of the list instead of being handed to clients forever.
package discovery

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"log/slog"
	"net/http"
	"time"

	"github.com/hibbault/bothy/internal/config"
	"github.com/hibbault/bothy/internal/httpx"
	"github.com/hibbault/bothy/internal/registry"
)

// Config describes the registry service.
type Config struct {
	TTL time.Duration
	// Token, when set, is required to register. Without it anyone reachable can
	// publish entries, which is a spam problem more than a security one.
	Token string
}

// Server is the registry's HTTP surface.
type Server struct {
	store *registry.Store
	token string
	log   *slog.Logger
}

// NewServer returns a registry whose entries live for cfg.TTL without a heartbeat.
func NewServer(cfg Config, log *slog.Logger) *Server {
	return &Server{store: registry.NewStore(cfg.TTL), token: cfg.Token, log: log}
}

// Run parses flags for the "discovery" command and serves until ctx is cancelled.
func Run(ctx context.Context, log *slog.Logger, args []string) error {
	fs := flag.NewFlagSet("discovery", flag.ExitOnError)
	listen := fs.String("listen", config.Str("BOTHY_LISTEN", ":8080"), "address to listen on")
	ttl := fs.Duration("ttl", config.Dur("BOTHY_REGISTRY_TTL", 60*time.Second), "how long a registration stays live without a heartbeat")
	token := fs.String("register-token", config.Str("BOTHY_REGISTRY_TOKEN", ""), "token required to register (optional)")
	if err := fs.Parse(args); err != nil {
		return err
	}
	// A TTL of zero or less expires every registration the moment it arrives, so
	// the registry would answer every lookup with nothing while looking healthy.
	// That is a misconfiguration, and it says so here instead.
	if *ttl <= 0 {
		return fmt.Errorf("ttl %s must be positive: an entry that expires on arrival leaves the registry serving nobody", *ttl)
	}
	s := NewServer(Config{TTL: *ttl, Token: *token}, log)
	if *token == "" {
		log.Warn("registration is open: anyone who can reach this port can publish entries")
	}
	log.Info("registry ready", "ttl", ttl.String())
	return httpx.Serve(ctx, *listen, s.Handler(), log)
}

// Handler returns the registry routes.
func (s *Server) Handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", s.handleHealth)
	mux.HandleFunc("GET /models", s.handleList)
	mux.Handle("POST /register", httpx.RequireToken(s.token, http.HandlerFunc(s.handleRegister)))
	return httpx.LogRequests(s.log, mux)
}

func (s *Server) handleHealth(w http.ResponseWriter, _ *http.Request) {
	httpx.JSON(w, http.StatusOK, map[string]any{
		"ok":           true,
		"live_entries": s.store.Len(),
		"ttl":          s.store.TTL().String(),
	})
}

// handleRegister accepts a batch of entries. A host serving several models sends
// them in one call, and repeats that call as its heartbeat.
func (s *Server) handleRegister(w http.ResponseWriter, r *http.Request) {
	var payload struct {
		Entries []registry.Entry `json:"entries"`
	}
	if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 1<<20)).Decode(&payload); err != nil {
		httpx.Error(w, http.StatusBadRequest, "invalid JSON body: "+err.Error())
		return
	}
	if len(payload.Entries) == 0 {
		httpx.Error(w, http.StatusBadRequest, "no entries to register")
		return
	}
	n := s.store.Register(payload.Entries)
	s.log.Info("registered", "accepted", n, "offered", len(payload.Entries), "live", s.store.Len())
	httpx.JSON(w, http.StatusOK, map[string]any{
		"registered": n,
		"live":       s.store.Len(),
		"ttl":        s.store.TTL().String(),
	})
}

// handleList answers "who has this model?".
func (s *Server) handleList(w http.ResponseWriter, r *http.Request) {
	entries := s.store.List(r.URL.Query().Get("model"))
	httpx.JSON(w, http.StatusOK, map[string]any{"entries": entries, "count": len(entries)})
}
