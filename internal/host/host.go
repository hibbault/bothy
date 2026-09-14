// Package host is the "share" side: it announces what a GPU can serve, meters
// who uses it, and proxies requests to the engine that owns it.
//
// It is a reverse proxy rather than a reimplementation. Everything Bothy does not
// handle itself goes straight through to the engine, which is what keeps
// streaming, token accounting and every OpenAI endpoint working without writing
// any protocol code at all.
package host

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"log/slog"
	"net"
	"net/http"
	"net/http/httputil"
	"net/url"
	"os"
	"strconv"
	"sync/atomic"
	"time"

	"github.com/hibbault/bothy/internal/config"
	"github.com/hibbault/bothy/internal/engine"
	"github.com/hibbault/bothy/internal/httpx"
	"github.com/hibbault/bothy/internal/meter"
	"github.com/hibbault/bothy/internal/model"
	"github.com/hibbault/bothy/internal/registry"
)

// Config describes one host.
type Config struct {
	Listen        string
	EngineURL     string
	EngineKind    string
	DiscoveryURL  string
	RegisterToken string
	// ShareKey is a single key, attributed to the peer named "default".
	ShareKey string
	// ShareKeys replaces ShareKey with per-peer keys, "alice:key,bob:key", so
	// that usage is attributed to a person instead of to everyone who shares a
	// key.
	ShareKeys string
	// PublicAddress is what gets registered: the address a client should dial.
	// It has to be the address peers can actually reach, which is usually not the
	// address this process listens on.
	PublicAddress string
	Heartbeat     time.Duration
	// MaxConcurrent caps requests in flight across the whole host. A GPU
	// serialises work anyway, so this is the limit that really protects it.
	// Zero means no cap.
	MaxConcurrent int
	// RequestsPerMinute caps one peer's request rate. Zero means no cap.
	RequestsPerMinute int
	Engine            engine.Options
}

// Host announces an engine's models, meters usage, and proxies to the engine.
type Host struct {
	cfg    Config
	engine engine.Lister
	proxy  *httputil.ReverseProxy
	disc   *registry.Client
	peers  *peers
	meter  *meter.Meter
	log    *slog.Logger
	name   string
	models atomic.Pointer[[]model.Model]
}

// Run parses flags for the "share" command and serves until ctx is cancelled.
func Run(ctx context.Context, log *slog.Logger, args []string) error {
	fs := flag.NewFlagSet("share", flag.ExitOnError)
	cfg := Config{}
	fs.StringVar(&cfg.Listen, "listen", config.Str("BOTHY_LISTEN", ":7777"), "address to listen on for peers")
	fs.StringVar(&cfg.EngineURL, "engine-url", config.Str("BOTHY_ENGINE_URL", "http://localhost:11434"), "your inference engine's base URL")
	fs.StringVar(&cfg.EngineKind, "engine-kind", config.Str("BOTHY_ENGINE_KIND", "auto"), "auto, ollama, openai, mock or static")
	fs.StringVar(&cfg.DiscoveryURL, "discovery-url", config.Str("BOTHY_DISCOVERY_URL", ""), "registry to announce to (optional)")
	fs.StringVar(&cfg.RegisterToken, "register-token", config.Str("BOTHY_REGISTRY_TOKEN", ""), "token for registering with the registry")
	fs.StringVar(&cfg.ShareKey, "share-key", config.Str("BOTHY_SHARE_KEY", ""), "single key peers must present")
	fs.StringVar(&cfg.ShareKeys, "share-keys", config.Str("BOTHY_SHARE_KEYS", ""), "per-peer keys as name:key,name:key; wins over -share-key")
	fs.StringVar(&cfg.PublicAddress, "address", config.Str("BOTHY_PUBLIC_ADDRESS", ""), "address to advertise to clients (default: hostname plus listen port)")
	fs.DurationVar(&cfg.Heartbeat, "heartbeat", config.Dur("BOTHY_HEARTBEAT", 20*time.Second), "how often to re-announce")
	fs.IntVar(&cfg.MaxConcurrent, "max-concurrent", config.Int("BOTHY_MAX_CONCURRENT", 4), "requests to serve at once across all peers (0 = no cap)")
	fs.IntVar(&cfg.RequestsPerMinute, "max-requests-per-minute", config.Int("BOTHY_MAX_REQUESTS_PER_MINUTE", 0), "request rate allowed per peer (0 = no cap)")
	fs.StringVar(&cfg.Engine.ModelsDir, "models-dir", config.Str("BOTHY_MODELS_DIR", ""), "Ollama models directory, for real weights digests")
	fs.StringVar(&cfg.Engine.WeightsPath, "weights", config.Str("BOTHY_WEIGHTS_PATH", ""), "weights file to hash (.gguf/.safetensors)")
	fs.StringVar(&cfg.Engine.WeightsModel, "model", config.Str("BOTHY_MODEL", ""), "model name the weights file belongs to")
	modelsFlag := fs.String("models", config.Str("BOTHY_MODELS", ""), "static model list as name=digest,name=digest")
	if err := fs.Parse(args); err != nil {
		return err
	}
	models, err := model.ParseList(*modelsFlag)
	if err != nil {
		return err
	}
	cfg.Engine.Static = models
	if cfg.PublicAddress == "" {
		cfg.PublicAddress = defaultAddress(cfg.Listen)
	}

	h, err := New(cfg, log)
	if err != nil {
		return err
	}
	return h.Serve(ctx)
}

// New builds a host. The engine is probed rather than connected to here, so a
// host can start before its engine is ready.
func New(cfg Config, log *slog.Logger) (*Host, error) {
	lister, err := engine.New(cfg.EngineKind, cfg.EngineURL, cfg.Engine)
	if err != nil {
		return nil, err
	}
	target, err := url.Parse(cfg.EngineURL)
	if err != nil {
		return nil, fmt.Errorf("engine URL %q: %w", cfg.EngineURL, err)
	}
	if target.Scheme == "" {
		target.Scheme = "http"
	}
	configured, err := resolvePeers(cfg.ShareKeys, cfg.ShareKey)
	if err != nil {
		return nil, err
	}

	h := &Host{
		cfg:    cfg,
		engine: lister,
		log:    log,
		name:   hostname(),
		peers:  configured,
		meter: meter.New(meter.Options{
			MaxConcurrent:     cfg.MaxConcurrent,
			RequestsPerMinute: cfg.RequestsPerMinute,
		}),
	}
	h.proxy = newEngineProxy(target)
	if cfg.DiscoveryURL != "" {
		h.disc = registry.NewClient(cfg.DiscoveryURL, cfg.RegisterToken)
	}
	return h, nil
}

// Handler returns the host's routes. Health is unauthenticated so a container
// healthcheck needs no key; everything else is attributed to a peer.
func (h *Host) Handler() http.Handler {
	api := http.NewServeMux()
	api.HandleFunc("GET /bothy/models", h.handleModels)
	api.HandleFunc("GET /bothy/usage", h.handleUsage)
	api.HandleFunc("/", h.handleProxy)

	root := http.NewServeMux()
	root.HandleFunc("GET /bothy/healthz", h.handleHealth)
	root.Handle("/", h.authenticate(api))
	return httpx.LogRequests(h.log, root)
}

// Serve runs the proxy and the announce loop until ctx is cancelled.
func (h *Host) Serve(ctx context.Context) error {
	h.describeLimits()
	h.log.Info("sharing",
		"listen", h.cfg.Listen,
		"engine", h.cfg.EngineURL,
		"kind", h.engine.Kind(),
		"discovery", h.cfg.DiscoveryURL,
		"address", h.cfg.PublicAddress,
	)
	go h.announceLoop(ctx)
	return httpx.Serve(ctx, h.cfg.Listen, h.Handler(), h.log)
}

func (h *Host) describeLimits() {
	switch {
	case h.peers.open():
		h.log.Warn("no share key set: anyone who can reach this port can use your GPU, metered by address")
	case len(h.peers.byKey) == 1:
		h.log.Info("share key required", "peers", 1)
	default:
		h.log.Info("per-peer share keys required", "peers", len(h.peers.byKey))
	}
	if h.cfg.MaxConcurrent <= 0 {
		h.log.Warn("no concurrency cap: a single peer can occupy the GPU indefinitely")
	}
	if h.cfg.RequestsPerMinute <= 0 {
		h.log.Info("no per-peer request rate cap")
	}
}

// authenticate resolves the presenting key to a peer and passes it down. The
// meter needs a name, so this runs before anything that counts.
func (h *Host) authenticate(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		peer, ok := h.peers.resolve(r)
		if !ok {
			httpx.Error(w, http.StatusUnauthorized, "missing or invalid key")
			return
		}
		next.ServeHTTP(w, r.WithContext(withPeer(r.Context(), peer)))
	})
}

// handleProxy is where the meter, the limiter and the engine proxy meet.
//
// The slot is taken before the request goes out and released only once the whole
// response has been streamed, so a slow completion counts against its peer for
// as long as it occupies the GPU.
func (h *Host) handleProxy(w http.ResponseWriter, r *http.Request) {
	peer := peerFrom(r.Context())
	start := time.Now()

	if err := h.meter.Begin(peer, start); err != nil {
		var limit *meter.LimitError
		if !errors.As(err, &limit) {
			httpx.Error(w, http.StatusInternalServerError, err.Error())
			return
		}
		if limit.RetryAfter > 0 {
			w.Header().Set("Retry-After", strconv.Itoa(int(limit.RetryAfter.Seconds()+0.5)))
		}
		h.log.Warn("refused", "peer", peer, "reason", limit.Reason, "path", r.URL.Path)
		httpx.Error(w, http.StatusTooManyRequests, err.Error())
		return
	}

	// Copy the template so this request can hang its own sniffer off the response
	// without racing every other request.
	proxy := *h.proxy
	var sniffer *meter.Sniffer
	proxy.ModifyResponse = func(resp *http.Response) error {
		if resp.StatusCode != http.StatusOK {
			return nil
		}
		sniffer = meter.NewSniffer(resp.Body, resp.Header.Get("Content-Type"))
		resp.Body = sniffer
		return nil
	}
	proxy.ServeHTTP(w, r)

	var usage meter.Usage
	var reported bool
	var responseBytes int64
	if sniffer != nil {
		usage, reported = sniffer.Usage()
		responseBytes = sniffer.Bytes()
	}
	h.meter.End(peer, usage, reported, responseBytes, time.Now())

	if reported {
		h.log.Info("served",
			"peer", peer,
			"prompt_tokens", usage.PromptTokens,
			"completion_tokens", usage.CompletionTokens,
			"duration", time.Since(start).Round(time.Millisecond).String(),
		)
	} else {
		h.log.Info("served", "peer", peer, "tokens", "unreported",
			"duration", time.Since(start).Round(time.Millisecond).String())
	}
}

func (h *Host) handleHealth(w http.ResponseWriter, _ *http.Request) {
	httpx.JSON(w, http.StatusOK, map[string]any{
		"ok":                  true,
		"host":                h.name,
		"engine_kind":         h.engine.Kind(),
		"address":             h.cfg.PublicAddress,
		"model_count":         len(h.currentModels()),
		"discovery":           h.cfg.DiscoveryURL,
		"key_required":        !h.peers.open(),
		"in_flight":           h.meter.InFlight(),
		"capacity":            h.meter.Capacity(),
		"max_concurrent":      h.cfg.MaxConcurrent,
		"requests_per_minute": h.cfg.RequestsPerMinute,
	})
}

// handleModels reports what this host serves, with digests. A client pointed
// straight at an address uses this to learn what it can verify.
func (h *Host) handleModels(w http.ResponseWriter, _ *http.Request) {
	httpx.JSON(w, http.StatusOK, map[string]any{
		"host":     h.name,
		"address":  h.cfg.PublicAddress,
		"capacity": h.meter.Capacity(),
		"models":   h.currentModels(),
	})
}

// handleUsage is the answer to "who is using my GPU?".
func (h *Host) handleUsage(w http.ResponseWriter, _ *http.Request) {
	httpx.JSON(w, http.StatusOK, map[string]any{
		"host":                h.name,
		"address":             h.cfg.PublicAddress,
		"in_flight":           h.meter.InFlight(),
		"capacity":            h.meter.Capacity(),
		"max_concurrent":      h.cfg.MaxConcurrent,
		"requests_per_minute": h.cfg.RequestsPerMinute,
		"peers":               h.meter.Snapshot(),
	})
}

func (h *Host) currentModels() []model.Model {
	if p := h.models.Load(); p != nil {
		return *p
	}
	return nil
}

// announceLoop re-registers on a timer. Registration is the heartbeat, so a host
// that dies stops being advertised after the registry's TTL.
func (h *Host) announceLoop(ctx context.Context) {
	h.announce(ctx)
	ticker := time.NewTicker(h.cfg.Heartbeat)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			h.announce(ctx)
		}
	}
}

func (h *Host) announce(ctx context.Context) {
	models, err := h.engine.ListModels(ctx)
	if err != nil {
		h.log.Warn("cannot list engine models", "engine", h.cfg.EngineURL, "err", err)
		return
	}
	h.models.Store(&models)
	h.log.Info("engine models", "models", model.FormatList(models))

	if h.disc == nil {
		return
	}
	// Capacity is how busy this host is right now, which is what lets clients
	// route to whoever is least loaded rather than to whoever was listed first.
	capacity := h.meter.Capacity()
	entries := make([]registry.Entry, 0, len(models))
	for _, m := range models {
		if m.Digest == "" {
			h.log.Warn("model has no digest; clients cannot verify it", "model", m.Name)
		}
		entries = append(entries, registry.Entry{
			Model:    m.Name,
			Digest:   m.Digest,
			Address:  h.cfg.PublicAddress,
			Host:     h.name,
			Capacity: capacity,
		})
	}
	if err := h.disc.Register(ctx, entries); err != nil {
		h.log.Warn("registration failed", "err", err)
		return
	}
	h.log.Info("announced", "models", model.FormatList(models), "address", h.cfg.PublicAddress, "capacity", capacity)
}

// newEngineProxy forwards unhandled requests to the engine.
func newEngineProxy(target *url.URL) *httputil.ReverseProxy {
	proxy := httputil.NewSingleHostReverseProxy(target)
	inner := proxy.Director
	proxy.Director = func(r *http.Request) {
		inner(r)
		// Send the engine its own host name, not ours, and never leak our key to
		// the engine or into its logs.
		r.Host = target.Host
		r.Header.Del(httpx.KeyHeader)
		r.Header.Del("Authorization")
	}
	// Flush immediately, or streamed tokens arrive in one lump at the end.
	proxy.FlushInterval = -1
	proxy.ErrorHandler = func(w http.ResponseWriter, _ *http.Request, err error) {
		httpx.Error(w, http.StatusBadGateway, "engine unreachable: "+err.Error())
	}
	return proxy
}

// defaultAddress builds the address to advertise when none was given: this
// machine's hostname plus the port we listen on.
func defaultAddress(listen string) string {
	port := listen
	if _, p, err := net.SplitHostPort(listen); err == nil {
		port = p
	}
	host, err := os.Hostname()
	if err != nil || host == "" {
		host = "localhost"
	}
	return net.JoinHostPort(host, port)
}

func hostname() string {
	if h, err := os.Hostname(); err == nil && h != "" {
		return h
	}
	return "unknown"
}
