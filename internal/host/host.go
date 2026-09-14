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
	"crypto/subtle"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"log/slog"
	"net"
	"net/http"
	"net/http/httputil"
	"net/url"
	"os"
	"strconv"
	"strings"
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
	// OwnerReserve is how many of MaxConcurrent are kept for you, out of reach of
	// peers. One by default: sharing your machine should not mean losing it.
	OwnerReserve int
	// PeerQuota is a per-peer request budget, as "count/period" — 200/1h. Empty
	// means no budget, and a peer may use the GPU all day a request at a time.
	PeerQuota string
	// RequestsPerMinute caps one peer's request rate. Zero means no cap.
	RequestsPerMinute int
	// AdminKey guards the /bothy/sharing control endpoint. Without it there is no
	// remote control at all. It is deliberately not a share key: peers hold
	// those, and a peer who can pause your host is worse than no control.
	AdminKey string
	// Paused starts the host refusing peers. It is what makes a schedule
	// possible — pause and resume from cron — without a stop that also drops the
	// registration.
	Paused bool
	// StreamUsage asks the engine to report token usage on streamed replies, by
	// adding stream_options.include_usage to streamed OpenAI requests. Without
	// it, a host whose peers stream would meter nothing at all. See inject.go.
	StreamUsage bool
	Engine      engine.Options
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
	// paused is the owner's hand on the tap. pausedAt is when it last went on,
	// zero when it is off.
	paused   atomic.Bool
	pausedAt atomic.Int64
	// wake asks the announce loop to re-announce now rather than at the next
	// heartbeat, so resuming does not leave clients waiting one out.
	wake chan struct{}
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
	fs.IntVar(&cfg.OwnerReserve, "owner-reserve", config.Int("BOTHY_OWNER_RESERVE", 1), "of max-concurrent, how many slots peers may not use (default 1, keeping one free for you)")
	fs.StringVar(&cfg.PeerQuota, "peer-quota", config.Str("BOTHY_PEER_QUOTA", ""), "per-peer request budget as count/period, e.g. 200/1h (empty = no budget)")
	fs.IntVar(&cfg.RequestsPerMinute, "max-requests-per-minute", config.Int("BOTHY_MAX_REQUESTS_PER_MINUTE", 0), "request rate allowed per peer (0 = no cap)")
	fs.StringVar(&cfg.AdminKey, "admin-key", config.Str("BOTHY_ADMIN_KEY", ""), "key for POST /bothy/sharing, which pauses and resumes sharing (empty = remote control disabled)")
	fs.BoolVar(&cfg.Paused, "paused", config.Bool("BOTHY_PAUSED", false), "start paused: refuse peers until resumed")
	fs.BoolVar(&cfg.StreamUsage, "stream-usage", config.Bool("BOTHY_STREAM_USAGE", true), "ask the engine for token usage on streamed replies, so they can be metered")
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
	quota, err := parseQuota(cfg.PeerQuota)
	if err != nil {
		return nil, err
	}
	// Refused rather than left to the announce loop: time.NewTicker panics on a
	// non-positive interval, so a heartbeat of 0 from an environment variable
	// would be a crash inside a goroutine instead of a startup error.
	if cfg.Heartbeat <= 0 {
		return nil, fmt.Errorf("heartbeat %s must be positive: the host re-announces on that interval, and a non-positive one would panic rather than announce", cfg.Heartbeat)
	}
	if cfg.OwnerReserve < 0 {
		return nil, fmt.Errorf("owner-reserve %d is negative, which would hand peers more slots than the cap allows", cfg.OwnerReserve)
	}
	// Refused rather than warned about: a reserve that swallows the whole cap is
	// a misconfiguration that looks like a working host serving nobody. Pausing
	// is the way to say "not right now", and it says so out loud.
	if cfg.MaxConcurrent > 0 && cfg.OwnerReserve >= cfg.MaxConcurrent {
		return nil, fmt.Errorf("owner-reserve %d leaves no slots for peers under max-concurrent %d: use -owner-reserve 0, raise -max-concurrent, or start with -paused", cfg.OwnerReserve, cfg.MaxConcurrent)
	}

	h := &Host{
		cfg:    cfg,
		engine: lister,
		log:    log,
		name:   hostname(),
		peers:  configured,
		wake:   make(chan struct{}, 1),
		meter: meter.New(meter.Options{
			MaxConcurrent:     cfg.MaxConcurrent,
			OwnerReserve:      cfg.OwnerReserve,
			PeerQuota:         quota,
			RequestsPerMinute: cfg.RequestsPerMinute,
		}),
	}
	if cfg.Paused {
		h.paused.Store(true)
		h.pausedAt.Store(time.Now().Unix())
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
	// The control endpoint sits on the root mux, outside the share-key
	// middleware, because it answers to the admin key instead. Share keys are
	// handed to peers, and a peer who can stop your host is worse than no control
	// at all.
	root.HandleFunc("POST /bothy/sharing", h.handleSharing)
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
	switch {
	case h.cfg.OwnerReserve <= 0:
		h.log.Info("no slots kept back for you: peers may use every slot the cap allows")
	case h.cfg.MaxConcurrent <= 0:
		h.log.Warn("owner-reserve has no effect without a concurrency cap", "owner_reserve", h.cfg.OwnerReserve)
	default:
		h.log.Info("slots kept for you", "owner_reserve", h.cfg.OwnerReserve, "peer_slots", h.meter.PeerSlots())
	}
	if q := h.meter.Quota(); q.Enabled() {
		h.log.Info("per-peer budget", "requests", q.Requests, "window", q.Window.String())
	} else {
		h.log.Info("no per-peer budget: a peer may use the GPU all day, a request at a time")
	}
	if h.cfg.RequestsPerMinute <= 0 {
		h.log.Info("no per-peer request rate cap")
	}
	if h.cfg.AdminKey == "" {
		h.log.Info("no admin key: sharing cannot be paused remotely")
	}
	if h.paused.Load() {
		h.log.Warn("starting paused: peers are refused until you resume")
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

	// Paused is checked before the meter, on purpose. The refusal is not the
	// peer's doing, so it must not count against their budget, and 503 with a
	// reason says "not now" rather than the 429 that means "too fast" — which is
	// also the one answer a client should treat as try-somewhere-else.
	if h.paused.Load() {
		h.log.Info("refused", "peer", peer, "reason", "paused", "path", r.URL.Path)
		httpx.Error(w, http.StatusServiceUnavailable, "the owner has paused sharing; this host is not serving peers right now")
		return
	}

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
	// Ask for streamed usage before the body goes out. Without this a streamed
	// reply arrives with no token counts and can only be metered as unreported,
	// which is most interactive use.
	if h.cfg.StreamUsage {
		injectStreamUsage(r)
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
		"owner_reserve":       h.cfg.OwnerReserve,
		"peer_slots":          h.meter.PeerSlots(),
		"requests_per_minute": h.cfg.RequestsPerMinute,
		"peer_quota":          h.cfg.PeerQuota,
		"paused":              h.paused.Load(),
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
		"owner_reserve":       h.cfg.OwnerReserve,
		"peer_slots":          h.meter.PeerSlots(),
		"requests_per_minute": h.cfg.RequestsPerMinute,
		"peer_quota":          h.cfg.PeerQuota,
		"paused":              h.paused.Load(),
		"peers":               h.meter.Snapshot(),
	})
}

// handleSharing pauses and resumes sharing, so that "not right now" does not have
// to mean stopping the process. Stopping works, but it also drops the host out of
// the registry and leaves clients with a connection error rather than an answer;
// this says what is happening.
func (h *Host) handleSharing(w http.ResponseWriter, r *http.Request) {
	if h.cfg.AdminKey == "" {
		// 404 rather than 403: without a key there is no control surface here at
		// all, and implying one exists that refused you would be a lie.
		httpx.Error(w, http.StatusNotFound, "no admin key is set, so remote control is disabled; start the host with -admin-key to enable it")
		return
	}
	presented := []byte(httpx.TokenFrom(r, httpx.KeyHeader))
	if subtle.ConstantTimeCompare(presented, []byte(h.cfg.AdminKey)) != 1 {
		httpx.Error(w, http.StatusUnauthorized, "missing or invalid admin key")
		return
	}
	var body struct {
		Paused *bool `json:"paused"`
	}
	// A bound on the body: this endpoint is meant to be reachable from wherever
	// the owner happens to be.
	if err := json.NewDecoder(io.LimitReader(r.Body, 4<<10)).Decode(&body); err != nil || body.Paused == nil {
		httpx.Error(w, http.StatusBadRequest, `send {"paused": true} to stop serving peers, or {"paused": false} to resume`)
		return
	}
	h.setPaused(*body.Paused)
	httpx.JSON(w, http.StatusOK, h.sharingState())
}

// setPaused flips the tap and nudges the announce loop, so a resume is visible to
// clients straight away rather than at the next heartbeat.
func (h *Host) setPaused(paused bool) {
	if h.paused.Swap(paused) == paused {
		return
	}
	if paused {
		h.pausedAt.Store(time.Now().Unix())
		h.log.Warn("sharing paused: peers are refused with 503, and this host will stop being advertised as its registry entry expires")
	} else {
		h.pausedAt.Store(0)
		h.log.Info("sharing resumed")
	}
	select {
	case h.wake <- struct{}{}:
	default:
	}
}

func (h *Host) sharingState() map[string]any {
	state := map[string]any{"paused": h.paused.Load()}
	if since := h.pausedAt.Load(); since != 0 {
		state["since"] = time.Unix(since, 0).UTC().Format(time.RFC3339)
	}
	return state
}

// parseQuota reads "200/1h" into a budget, at startup, so that a typo is an error
// rather than a limit that silently does nothing.
func parseQuota(spec string) (meter.Quota, error) {
	spec = strings.TrimSpace(spec)
	if spec == "" {
		return meter.Quota{}, nil
	}
	count, period, ok := strings.Cut(spec, "/")
	if !ok {
		return meter.Quota{}, fmt.Errorf("peer quota %q: want count/period, e.g. 200/1h", spec)
	}
	n, err := strconv.Atoi(strings.TrimSpace(count))
	if err != nil || n <= 0 {
		return meter.Quota{}, fmt.Errorf("peer quota %q: %q is not a positive number of requests", spec, strings.TrimSpace(count))
	}
	window, err := time.ParseDuration(strings.TrimSpace(period))
	if err != nil || window <= 0 {
		return meter.Quota{}, fmt.Errorf("peer quota %q: %q is not a duration like 1h or 24h", spec, strings.TrimSpace(period))
	}
	return meter.Quota{Requests: n, Window: window}, nil
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
		case <-h.wake:
			// Asked to rather than due: resuming should be visible now, not one
			// heartbeat from now.
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

	if h.paused.Load() {
		// Stop announcing while paused, so clients route to somebody else rather
		// than to a host that will refuse them. There is no delete in the
		// registry protocol, so "stop saying it" is the mechanism, and the entry
		// then expires on the registry's own TTL.
		h.log.Info("paused: not announcing; this host's registry entry will expire on its own")
		return
	}
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
