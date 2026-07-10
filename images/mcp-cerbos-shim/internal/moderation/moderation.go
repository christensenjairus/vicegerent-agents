// Package moderation checks free-text tool-call content against OpenAI's
// Moderations endpoint before a write reaches Notion/Linear/GitHub/Jira/
// PagerDuty. Existing Cerbos rules check WHO/WHERE; this checks WHAT.
package moderation

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"strings"
)

// DefaultModerationURL routes through the existing openai AgentgatewayBackend
// (apps/base/models/openai/backend.yaml) -- no new secret or egress rule needed.
const DefaultModerationURL = "http://agentgateway-proxy.agentgateway-system.svc.cluster.local/openai/v1/moderations"

const DefaultModel = "omni-moderation-latest"

// Checker calls OpenAI's Moderations endpoint. Production uses *Client;
// tests substitute a stub.
type Checker interface {
	Check(ctx context.Context, inputs []string) (*Result, error)
}

// Client is a minimal client for OpenAI's POST /v1/moderations, reached
// through agentgateway's Passthrough route -- auth to the real OpenAI API is
// handled entirely by agentgateway's own policies.auth.secretRef on the
// openai AgentgatewayBackend, so this client sends no credential of its own.
type Client struct {
	url        string
	model      string
	httpClient *http.Client
}

func New(url, model string, httpClient *http.Client) *Client {
	if url == "" {
		url = DefaultModerationURL
	}
	if model == "" {
		model = DefaultModel
	}
	if httpClient == nil {
		httpClient = &http.Client{}
	}
	return &Client{url: url, model: model, httpClient: httpClient}
}

type moderationRequest struct {
	Model string   `json:"model"`
	Input []string `json:"input"`
}

type moderationResponse struct {
	Results []struct {
		Flagged    bool               `json:"flagged"`
		Categories map[string]bool    `json:"categories"`
		Scores     map[string]float64 `json:"category_scores"`
	} `json:"results"`
}

// Result is the outcome of a Check call.
type Result struct {
	Flagged           bool
	FlaggedIndex      int
	FlaggedCategories []string
}

// Check batches every non-trivial string in inputs into one Moderations call
// and returns the first flagged result, or a non-flagged Result if none matched.
func (c *Client) Check(ctx context.Context, inputs []string) (*Result, error) {
	filtered := make([]string, 0, len(inputs))
	for _, s := range inputs {
		if len(strings.TrimSpace(s)) < 4 {
			continue // skip ids/enum values -- not worth moderating
		}
		filtered = append(filtered, s)
	}
	if len(filtered) == 0 {
		return &Result{}, nil
	}

	reqBody, err := json.Marshal(moderationRequest{Model: c.model, Input: filtered})
	if err != nil {
		return nil, fmt.Errorf("marshal moderation request: %w", err)
	}

	httpReq, err := http.NewRequestWithContext(ctx, http.MethodPost, c.url, bytes.NewReader(reqBody))
	if err != nil {
		return nil, fmt.Errorf("build moderation request: %w", err)
	}
	httpReq.Header.Set("Content-Type", "application/json")

	resp, err := c.httpClient.Do(httpReq)
	if err != nil {
		return nil, fmt.Errorf("moderation request: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("moderation request: unexpected status %d", resp.StatusCode)
	}

	var parsed moderationResponse
	if err := json.NewDecoder(resp.Body).Decode(&parsed); err != nil {
		return nil, fmt.Errorf("decode moderation response: %w", err)
	}

	for i, r := range parsed.Results {
		if !r.Flagged {
			continue
		}
		var cats []string
		for cat, hit := range r.Categories {
			if hit {
				cats = append(cats, cat)
			}
		}
		return &Result{Flagged: true, FlaggedIndex: i, FlaggedCategories: cats}, nil
	}
	return &Result{}, nil
}
