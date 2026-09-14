// Package host is the "share" side: it announces what a GPU can serve, meters
// who uses it, and proxies requests to the engine that owns it.
package host

import (
	"context"
	"crypto/subtle"
	"fmt"
	"net"
	"net/http"
	"strings"

	"github.com/hibbault/bothy/internal/httpx"
)

// peers resolves the key a request presents to a name.
//
// Names matter because usage is only actionable per person: "someone used two
// million tokens" tells you nothing, "alice used two million tokens" does. A host
// with no keys configured still meters, attributing usage to the caller's
// address, so limits and accounting apply even in the open case.
type peers struct {
	byKey map[string]string
}

// parsePeers accepts "alice:key1,bob:key2". Keys may themselves contain colons;
// the name is everything before the first one.
func parsePeers(spec string) (*peers, error) {
	p := &peers{byKey: make(map[string]string)}
	for _, item := range strings.Split(spec, ",") {
		item = strings.TrimSpace(item)
		if item == "" {
			continue
		}
		name, key, ok := strings.Cut(item, ":")
		name, key = strings.TrimSpace(name), strings.TrimSpace(key)
		if !ok || name == "" || key == "" {
			return nil, fmt.Errorf("bad share key %q: want name:key", item)
		}
		if _, taken := p.byKey[key]; taken {
			return nil, fmt.Errorf("the key for %q is already used by another peer", name)
		}
		p.byKey[key] = name
	}
	if len(p.byKey) == 0 {
		return nil, fmt.Errorf("no share keys found in %q", spec)
	}
	return p, nil
}

// resolvePeers prefers named keys, falls back to a single key, and finally to
// metering by address.
func resolvePeers(named, single string) (*peers, error) {
	if strings.TrimSpace(named) != "" {
		return parsePeers(named)
	}
	if strings.TrimSpace(single) != "" {
		return &peers{byKey: map[string]string{single: "default"}}, nil
	}
	return &peers{byKey: make(map[string]string)}, nil
}

// open reports whether the port is unauthenticated.
func (p *peers) open() bool { return len(p.byKey) == 0 }

// resolve returns which peer a request belongs to, and whether it is allowed in.
func (p *peers) resolve(r *http.Request) (string, bool) {
	if p.open() {
		return "addr:" + callerHost(r), true
	}
	presented := []byte(httpx.TokenFrom(r, httpx.KeyHeader))
	for key, name := range p.byKey {
		if subtle.ConstantTimeCompare(presented, []byte(key)) == 1 {
			return name, true
		}
	}
	return "", false
}

func callerHost(r *http.Request) string {
	if host, _, err := net.SplitHostPort(r.RemoteAddr); err == nil {
		return host
	}
	return r.RemoteAddr
}

type peerContextKey struct{}

func withPeer(ctx context.Context, name string) context.Context {
	return context.WithValue(ctx, peerContextKey{}, name)
}

// peerFrom returns the peer resolved by the host's auth middleware.
func peerFrom(ctx context.Context) string {
	name, _ := ctx.Value(peerContextKey{}).(string)
	return name
}
