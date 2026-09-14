// Package client is the "connect" side: it exposes a local, OpenAI-compatible
// endpoint that is really somebody else's GPU.
//
// The local port defaults to 11434, the port Ollama already uses, so a machine
// with no GPU quietly becomes an Ollama as far as every existing tool, editor
// and CLI is concerned.
package client

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"log/slog"
	"net/http"
	"net/http/httputil"
	"net/url"
	"strings"
	"sync"
	"time"

	"github.com/hibbault/bothy/internal/config"
	"github.com/hibbault/bothy/internal/httpx"
	"github.com/hibbault/bothy/internal/model"
	"github.com/hibbault/bothy/internal/registry"
)

var httpClient = &http.Client{Timeout: 20 * time.Second}

// MismatchError reports that a host's advertised digest is not the one that was
// required. Retrying will not fix it, so it is treated as fatal.
type MismatchError struct {
	Model    string
	Expected string
	Actual   string
}

func (e *MismatchError) Error() string {
	if strings.TrimSpace(e.Actual) == "" {
		return fmt.Sprintf("cannot verify %s: host advertised no digest, but %s was expected",
			e.Model, model.NormalizeDigest(e.Expected))
	}
	return fmt.Sprintf("digest mismatch for %s: expected %s, host offers %s",
		e.Model, model.NormalizeDigest(e.Expected), model.NormalizeDigest(e.Actual))
}

// Verify decides whether the digest a host advertises satisfies what was asked
// for.
//
// An empty expectation accepts anything, which is the default because most
// people just want a working model. The moment a digest is named, a host
// offering different weights is refused rather than silently used.
func Verify(expected, actual, name string) error {
	if strings.TrimSpace(expected) == "" {
		return nil
	}
	if !model.EqualDigest(expected, actual) {
		return &MismatchError{Model: name, Expected: expected, Actual: actual}
	}
	return nil
}

// Config describes one client.
type Config struct {
	Listen string
	// HostAddress points straight at a host, skipping discovery.
	HostAddress    string
	DiscoveryURL   string
	Model          string
	ShareKey       string
	ExpectedDigest string
	// LocalAPIKey, when set, is required on the local endpoint. Empty leaves it
	// open, which is normal because it only listens on loopback.
	LocalAPIKey string
}

// Client resolves a host once, then forwards local requests to it.
type Client struct {
	cfg    Config
	log    *slog.Logger
	disc   *registry.Client
	proxy  *httputil.ReverseProxy
	mu     sync.RWMutex
	entry  registry.Entry
	target *url.URL
}

// Run parses flags for the "connect" command and serves until ctx is cancelled.
func Run(ctx context.Context, log *slog.Logger, args []string) error {
	fs := flag.NewFlagSet("connect", flag.ExitOnError)
	cfg := Config{}
	fs.StringVar(&cfg.Listen, "listen", config.Str("BOTHY_LISTEN", "127.0.0.1:11434"), "local address for the OpenAI-compatible endpoint")
	fs.StringVar(&cfg.HostAddress, "host", config.Str("BOTHY_HOST", ""), "host to connect to directly, e.g. box.example:7777")
	fs.StringVar(&cfg.DiscoveryURL, "discovery-url", config.Str("BOTHY_DISCOVERY_URL", ""), "registry to look the host up in")
	fs.StringVar(&cfg.Model, "model", config.Str("BOTHY_MODEL", ""), "model to use, e.g. llama3.1:8b")
	fs.StringVar(&cfg.ShareKey, "share-key", config.Str("BOTHY_SHARE_KEY", ""), "key the host expects")
	fs.StringVar(&cfg.ExpectedDigest, "expected-digest", config.Str("BOTHY_EXPECTED_DIGEST", ""), "required weights digest; anything else is refused")
	fs.StringVar(&cfg.LocalAPIKey, "local-api-key", config.Str("BOTHY_LOCAL_API_KEY", ""), "key required on the local endpoint (optional)")
	if err := fs.Parse(args); err != nil {
		return err
	}

	c := New(cfg, log)
	// A digest mismatch is a configuration error, so fail the process instead of
	// serving requests that can never succeed. A host that is merely not up yet
	// is fine to wait for, because compose starts services in whatever order it
	// likes and we re-resolve on the first request anyway.
	if _, err := c.ensureTarget(ctx); err != nil {
		var mismatch *MismatchError
		if errors.As(err, &mismatch) {
			return err
		}
		log.Warn("no host reachable yet; will connect on the first request", "err", err)
	}
	return httpx.Serve(ctx, cfg.Listen, c.Handler(), log)
}

// New builds a client.
func New(cfg Config, log *slog.Logger) *Client {
	c := &Client{cfg: cfg, log: log}
	if cfg.DiscoveryURL != "" {
		c.disc = registry.NewClient(cfg.DiscoveryURL, "")
	}
	c.proxy = &httputil.ReverseProxy{
		Director: c.director,
		// Flush immediately so streamed tokens arrive as they are produced.
		FlushInterval: -1,
		ErrorHandler: func(w http.ResponseWriter, _ *http.Request, err error) {
			c.log.Warn("host unreachable; will re-resolve on the next request", "err", err)
			c.invalidate()
			httpx.Error(w, http.StatusBadGateway, "host unreachable: "+err.Error())
		},
	}
	return c
}

// Handler returns the client's routes. The local status route is the only thing
// Bothy answers itself; everything else goes to the host.
func (c *Client) Handler() http.Handler {
	root := http.NewServeMux()
	root.HandleFunc("GET /bothy/status", c.handleStatus)
	root.Handle("/", httpx.RequireToken(c.cfg.LocalAPIKey, http.HandlerFunc(c.forward)))
	return httpx.LogRequests(c.log, root)
}

// forward makes sure a host is resolved, then proxies.
func (c *Client) forward(w http.ResponseWriter, r *http.Request) {
	if _, err := c.ensureTarget(r.Context()); err != nil {
		httpx.Error(w, http.StatusBadGateway, err.Error())
		return
	}
	c.proxy.ServeHTTP(w, r)
}

// director points the outgoing request at whatever host is currently resolved,
// and swaps the local caller's key for the share key the host expects.
func (c *Client) director(r *http.Request) {
	c.mu.RLock()
	target := c.target
	c.mu.RUnlock()
	if target == nil {
		return
	}
	r.URL.Scheme = target.Scheme
	r.URL.Host = target.Host
	r.Host = target.Host
	r.Header.Del("Authorization")
	r.Header.Del(httpx.KeyHeader)
	if c.cfg.ShareKey != "" {
		r.Header.Set(httpx.KeyHeader, c.cfg.ShareKey)
	}
}

// ensureTarget returns the resolved host, resolving it on first use and after a
// failure.
func (c *Client) ensureTarget(ctx context.Context) (*url.URL, error) {
	c.mu.RLock()
	if c.target != nil {
		target := c.target
		c.mu.RUnlock()
		return target, nil
	}
	c.mu.RUnlock()

	c.mu.Lock()
	defer c.mu.Unlock()
	if c.target != nil {
		return c.target, nil
	}
	entry, err := c.resolve(ctx)
	if err != nil {
		return nil, err
	}
	if err := Verify(c.cfg.ExpectedDigest, entry.Digest, entry.Model); err != nil {
		return nil, err
	}
	target, err := url.Parse(withScheme(entry.Address))
	if err != nil {
		return nil, fmt.Errorf("host address %q: %w", entry.Address, err)
	}
	if target.Host == "" {
		return nil, fmt.Errorf("host address %q has no host", entry.Address)
	}
	c.entry = entry
	c.target = target
	c.log.Info("connected",
		"host", target.Host,
		"model", entry.Model,
		"digest", orUnknown(entry.Digest),
		"digest_checked", c.cfg.ExpectedDigest != "",
	)
	return target, nil
}

// invalidate forgets the current host so the next request resolves a fresh one.
func (c *Client) invalidate() {
	c.mu.Lock()
	c.target = nil
	c.mu.Unlock()
}

// resolve picks a host: the one we were pointed at, or the best live entry for
// the requested model.
func (c *Client) resolve(ctx context.Context) (registry.Entry, error) {
	if addr := strings.TrimSpace(c.cfg.HostAddress); addr != "" {
		base := withScheme(addr)
		u, err := url.Parse(base)
		if err != nil {
			return registry.Entry{}, fmt.Errorf("host %q: %w", addr, err)
		}
		entry := registry.Entry{Address: u.Host, Model: c.cfg.Model}
		// A directly-addressed host can be asked what it serves, which is how a
		// direct connection still ends up with a verifiable digest.
		models, err := c.fetchModels(ctx, base)
		if err != nil {
			c.log.Warn("cannot read the host's model list, so the digest cannot be checked",
				"host", addr, "err", err)
			return entry, nil
		}
		if m, ok := pick(models, c.cfg.Model); ok {
			entry.Model, entry.Digest = m.Name, m.Digest
		}
		return entry, nil
	}
	if c.disc == nil {
		return registry.Entry{}, fmt.Errorf("nothing to connect to: set -host or -discovery-url")
	}
	entries, err := c.disc.List(ctx, c.cfg.Model)
	if err != nil {
		return registry.Entry{}, err
	}
	if len(entries) == 0 {
		if c.cfg.Model == "" {
			return registry.Entry{}, fmt.Errorf("no hosts are registered right now")
		}
		return registry.Entry{}, fmt.Errorf("no host is offering %q right now", c.cfg.Model)
	}
	return entries[0], nil
}

// fetchModels asks a host what it serves, so digests can be checked even when
// discovery was not involved.
func (c *Client) fetchModels(ctx context.Context, base string) ([]model.Model, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, strings.TrimRight(base, "/")+"/bothy/models", nil)
	if err != nil {
		return nil, err
	}
	if c.cfg.ShareKey != "" {
		req.Header.Set(httpx.KeyHeader, c.cfg.ShareKey)
	}
	resp, err := httpClient.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("/bothy/models: %s", resp.Status)
	}
	var payload struct {
		Models []model.Model `json:"models"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&payload); err != nil {
		return nil, err
	}
	return payload.Models, nil
}

func (c *Client) handleStatus(w http.ResponseWriter, _ *http.Request) {
	c.mu.RLock()
	defer c.mu.RUnlock()

	status := map[string]any{
		"listening":       c.cfg.Listen,
		"discovery":       c.cfg.DiscoveryURL,
		"requested_model": c.cfg.Model,
		"expected_digest": c.cfg.ExpectedDigest,
		"connected":       c.target != nil,
	}
	if c.target != nil {
		status["host"] = c.target.Host
		status["model"] = c.entry.Model
		status["digest"] = c.entry.Digest
		status["digest_verified"] = model.EqualDigest(c.cfg.ExpectedDigest, c.entry.Digest)
	}
	httpx.JSON(w, http.StatusOK, status)
}

// pick returns the requested model, or the only one on offer when nothing was
// requested.
func pick(models []model.Model, name string) (model.Model, bool) {
	if len(models) == 0 {
		return model.Model{}, false
	}
	if strings.TrimSpace(name) == "" {
		return models[0], true
	}
	for _, m := range models {
		if model.Matches(name, m.Name) {
			return m, true
		}
	}
	return model.Model{}, false
}

func withScheme(addr string) string {
	if strings.Contains(addr, "://") {
		return addr
	}
	return "http://" + addr
}

func orUnknown(d string) string {
	if strings.TrimSpace(d) == "" {
		return "unknown"
	}
	return d
}
