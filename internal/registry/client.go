package registry

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/url"
	"strings"
	"time"

	"github.com/hibbault/bothy/internal/httpx"
)

// Client talks to a discovery service.
type Client struct {
	BaseURL string
	Token   string
	HTTP    *http.Client
}

// NewClient returns a client for the discovery service at baseURL. Token is used
// for registration; an empty token means registration is open.
func NewClient(baseURL, token string) *Client {
	return &Client{
		BaseURL: strings.TrimRight(baseURL, "/"),
		Token:   token,
		HTTP:    &http.Client{Timeout: 15 * time.Second},
	}
}

// Register publishes entries. Calling this once per heartbeat is the entire
// liveness protocol.
func (c *Client) Register(ctx context.Context, entries []Entry) error {
	body, err := json.Marshal(map[string][]Entry{"entries": entries})
	if err != nil {
		return err
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.BaseURL+"/register", bytes.NewReader(body))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")
	if c.Token != "" {
		req.Header.Set("Authorization", "Bearer "+c.Token)
	}
	resp, err := c.HTTP.Do(req)
	if err != nil {
		return fmt.Errorf("discovery %s: %w", c.BaseURL, err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("discovery %s: register: %s: %s", c.BaseURL, resp.Status, httpx.Snippet(resp.Body, 300))
	}
	return nil
}

// List returns live entries, filtered by model name when name is not empty.
func (c *Client) List(ctx context.Context, name string) ([]Entry, error) {
	endpoint := c.BaseURL + "/models"
	if name != "" {
		endpoint += "?model=" + url.QueryEscape(name)
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, endpoint, nil)
	if err != nil {
		return nil, err
	}
	if c.Token != "" {
		req.Header.Set("Authorization", "Bearer "+c.Token)
	}
	resp, err := c.HTTP.Do(req)
	if err != nil {
		return nil, fmt.Errorf("discovery %s: %w", c.BaseURL, err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("discovery %s: models: %s: %s", c.BaseURL, resp.Status, httpx.Snippet(resp.Body, 300))
	}
	var payload struct {
		Entries []Entry `json:"entries"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&payload); err != nil {
		return nil, fmt.Errorf("discovery %s: decode /models: %w", c.BaseURL, err)
	}
	return payload.Entries, nil
}
