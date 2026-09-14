//go:build swarm

package swarm

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strings"
)

// Chat is the inference the swarm borrows: an OpenAI-compatible
// /v1/chat/completions endpoint.
//
// That is the same surface `bothy connect` exposes and `bothy share` proxies, so
// solving a task on somebody else's GPU is a matter of pointing -engine-url at
// the client's local endpoint rather than at a local Ollama. No new transport and
// no new protocol — which is also why the swarm needs nothing from PROTOCOL.md.
type Chat struct {
	BaseURL string
	// Key is sent as X-Bothy-Key when set, which is how a Bothy host
	// authenticates a peer. Real engines ignore the header.
	Key  string
	HTTP *http.Client
}

// Complete sends one prompt and returns the reply.
//
// Non-streaming on purpose: v0 wants an artifact, not a conversation, and a
// whole reply is easier to digest and store than a stream is to reassemble.
func (c *Chat) Complete(ctx context.Context, model, prompt string) (string, error) {
	if strings.TrimSpace(model) == "" {
		return "", errors.New("no model: set model on the task or the node, or pass -model")
	}
	body, err := json.Marshal(map[string]any{
		"model":  model,
		"stream": false,
		"messages": []map[string]string{
			{"role": "user", "content": prompt},
		},
	})
	if err != nil {
		return "", err
	}
	base := strings.TrimRight(strings.TrimSpace(c.BaseURL), "/")
	if base == "" {
		return "", errors.New("no engine URL: pass -engine-url")
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, base+"/v1/chat/completions", bytes.NewReader(body))
	if err != nil {
		return "", err
	}
	req.Header.Set("Content-Type", "application/json")
	if c.Key != "" {
		req.Header.Set("X-Bothy-Key", c.Key)
	}
	client := c.HTTP
	if client == nil {
		client = http.DefaultClient
	}
	resp, err := client.Do(req)
	if err != nil {
		return "", fmt.Errorf("engine %s: %w", base, err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		detail, _ := io.ReadAll(io.LimitReader(resp.Body, 2048))
		return "", fmt.Errorf("engine %s: %s: %s", base, resp.Status, strings.TrimSpace(string(detail)))
	}
	var payload struct {
		Choices []struct {
			Message struct {
				Content string `json:"content"`
			} `json:"message"`
		} `json:"choices"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&payload); err != nil {
		return "", fmt.Errorf("engine %s: decode reply: %w", base, err)
	}
	if len(payload.Choices) == 0 {
		return "", fmt.Errorf("engine %s: reply has no choices", base)
	}
	content := payload.Choices[0].Message.Content
	// An empty reply is refused rather than stored as an empty artifact: an empty
	// artifact passes an exact check whose want is empty, and would look like a
	// solved node.
	if strings.TrimSpace(content) == "" {
		return "", fmt.Errorf("engine %s: reply is empty", base)
	}
	return content, nil
}
