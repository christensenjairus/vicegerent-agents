// Package upstream makes the shim itself an MCP client, calling back into
// vMCP (via agentgateway, in-cluster HTTP) to resolve state the Cerbos
// CheckRequest path cannot see from the request being evaluated alone --
// e.g. a Notion page's ancestry, which isn't in the notion_notion-update-page
// call's own args. See internal/eval/eval.go's package doc for why this
// can't live in a CEL helper: CEL programs are synchronous pure functions
// with no I/O, and this needs a real network round trip.
//
// RECURSION-SAFETY NOTE (read before mapping notion_notion-fetch in Cerbos):
// every lookup this package makes is itself a tools/call that re-enters the
// shim's own CheckRequest gate exactly once (agentgateway routes it back
// through the shim before forwarding to vMCP). Today notion_notion-fetch is
// completely unmapped in mapping.yaml (falls through to defaultAction:
// allow), so that re-entry passes straight through and there is no loop.
// If a future Cerbos policy or mapping.yaml entry ever adds a deny rule (or
// any check requiring another lookup) for notion_notion-fetch, this call
// could start getting denied, silently breaking every ancestry check that
// depends on it (they fail closed -- see PageIsUnderAncestor -- so the failure
// mode is "always deny", not silent-allow, but it will look like an
// unrelated regression). Keep notion_notion-fetch unmapped, or if it ever
// needs a policy, make sure this package's calls are exempted or the
// ancestry check is redesigned.
package upstream

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"
)

// DefaultVMCPURL is the in-cluster agentgateway route to vMCP. Confirmed
// live-reachable over plain HTTP (no mTLS) -- the mTLS hop (ghostunnel) only
// covers agentgateway's own egress to the host vMCP, not this in-cluster leg.
// Same URL Hermes's own hermes-config vmcp MCP client uses.
const DefaultVMCPURL = "http://agentgateway-proxy.agentgateway-system.svc.cluster.local/mcp/vmcp"

// mcpProtocolVersion is the MCP spec date this client speaks. Sent in
// initialize and echoed in the MCP-Protocol-Version header on every
// subsequent request per the streamable-HTTP transport spec.
const mcpProtocolVersion = "2025-06-18"

// Client is a minimal MCP client over the streamable-HTTP transport: no
// vendored MCP SDK exists in this repo's go.mod, and the wire protocol
// needed here (initialize -> initialized -> tools/call, single POST per
// call) is small enough to hand-roll rather than pull in a new dependency
// for one call site.
type Client struct {
	url        string
	httpClient *http.Client
}

// New constructs a Client. httpClient may be nil to use a default
// *http.Client{} (plain HTTP, no TLS config -- see DefaultVMCPURL doc).
// Per-call timeouts are enforced via context, not a client-wide Timeout, so
// callers control the budget explicitly (see CallTool).
func New(url string, httpClient *http.Client) *Client {
	if httpClient == nil {
		httpClient = &http.Client{}
	}
	return &Client{url: url, httpClient: httpClient}
}

// jsonrpcRequest and jsonrpcResponse are the minimal JSON-RPC 2.0 envelope
// this client needs; no batching, no bidirectional server->client requests.
type jsonrpcRequest struct {
	JSONRPC string `json:"jsonrpc"`
	ID      int    `json:"id"`
	Method  string `json:"method"`
	Params  any    `json:"params,omitempty"`
}

type jsonrpcResponse struct {
	JSONRPC string          `json:"jsonrpc"`
	ID      int             `json:"id"`
	Result  json.RawMessage `json:"result,omitempty"`
	Error   *jsonrpcError   `json:"error,omitempty"`
}

type jsonrpcError struct {
	Code    int    `json:"code"`
	Message string `json:"message"`
}

func (e *jsonrpcError) Error() string {
	return fmt.Sprintf("mcp error %d: %s", e.Code, e.Message)
}

// CallToolResult is the tools/call result shape this package needs: the
// content blocks a tool returns. Notion's tools return their payload as a
// single text block of enhanced Markdown (see docs/available-mcp-tools/
// notion.yaml); other content types (image/resource) are ignored here.
type CallToolResult struct {
	Content []struct {
		Type string `json:"type"`
		Text string `json:"text"`
	} `json:"content"`
	IsError bool `json:"isError"`
}

// Text concatenates every text content block, which is all the ancestry
// walk needs from a notion_notion-fetch response.
func (r *CallToolResult) Text() string {
	var b strings.Builder
	for _, c := range r.Content {
		if c.Type == "text" {
			b.WriteString(c.Text)
		}
	}
	return b.String()
}

// callToolMetaName is the vMCP tool-discovery optimizer's (thv vmcp serve
// --optimizer, on by DEFAULT in this deployment -- see host/mcp/README.md
// "Tool discovery optimizer" and VMCP_OPTIMIZER in vicegerent_mcp.py) meta-tool
// name. With it on, vMCP's own tools/list exposes ONLY find_tool/call_tool --
// there is no raw notion_notion-fetch tool at that level to call directly.
// Every real tool invocation, including this package's own outbound ancestry
// lookup, must go through call_tool{tool_name, parameters} or vMCP returns
// "tool not found" (confirmed live: HAH-88's first deploy attempt hit exactly
// this before it was fixed here -- see git history / MR revert). This is the
// EXACT mirror, on the outbound side, of what server.go's callToolMeta
// unwrapping does on the inbound side for calls arriving at the shim.
const callToolMetaName = "call_tool"

// CallTool performs a fresh MCP handshake (initialize -> notifications/
// initialized -> tools/call) and returns the named tool's result. The
// tools/call itself is always wrapped in the optimizer's call_tool meta-tool
// (see callToolMetaName) -- there is no direct/unwrapped path in this
// deployment, so this package doesn't try to detect or fall back; if a future
// deployment ever disables the optimizer, this wrapping becomes a no-op shape
// mismatch that would need revisiting, not a silent failure (vMCP would still
// need to expose call_tool for this to work at all). Each call opens its own
// session; the ancestry check makes exactly one lookup per update-page call (a
// single notion-fetch resolves the full ancestor chain), so the extra
// handshake round trips are an acceptable per-call cost, not a hot path.
// ctx's deadline governs the whole sequence -- exceeding it, any non-200, any
// malformed JSON-RPC envelope, or a JSON-RPC error response all return a
// non-nil error; there is no partial/best-effort result.
func (c *Client) CallTool(ctx context.Context, tool string, arguments map[string]any) (*CallToolResult, error) {
	sessionID, err := c.initialize(ctx)
	if err != nil {
		return nil, fmt.Errorf("upstream initialize: %w", err)
	}

	resp, err := c.doRPC(ctx, sessionID, jsonrpcRequest{
		JSONRPC: "2.0",
		ID:      2,
		Method:  "tools/call",
		Params: map[string]any{
			"name": callToolMetaName,
			"arguments": map[string]any{
				"tool_name":  tool,
				"parameters": arguments,
			},
		},
	})
	if err != nil {
		return nil, fmt.Errorf("upstream tools/call %s: %w", tool, err)
	}
	if resp.Error != nil {
		return nil, fmt.Errorf("upstream tools/call %s: %w", tool, resp.Error)
	}

	var result CallToolResult
	if err := json.Unmarshal(resp.Result, &result); err != nil {
		return nil, fmt.Errorf("upstream tools/call %s: malformed result: %w", tool, err)
	}
	if result.IsError {
		return nil, fmt.Errorf("upstream tools/call %s: tool reported an error: %s", tool, result.Text())
	}
	return &result, nil
}

// initialize opens a new MCP session and returns the Mcp-Session-Id the
// server assigned (per the streamable-HTTP transport spec), sending the
// required notifications/initialized notification before returning.
func (c *Client) initialize(ctx context.Context) (string, error) {
	req := jsonrpcRequest{
		JSONRPC: "2.0",
		ID:      1,
		Method:  "initialize",
		Params: map[string]any{
			"protocolVersion": mcpProtocolVersion,
			"capabilities":    map[string]any{},
			"clientInfo": map[string]any{
				"name":    "mcp-cerbos-shim",
				"version": "1",
			},
		},
	}
	sessionID, resp, err := c.postJSONRPC(ctx, "", req)
	if err != nil {
		return "", err
	}
	if resp.Error != nil {
		return "", resp.Error
	}

	// Fire-and-forget notification; no response body per JSON-RPC (no id).
	notif := struct {
		JSONRPC string `json:"jsonrpc"`
		Method  string `json:"method"`
	}{JSONRPC: "2.0", Method: "notifications/initialized"}
	body, err := json.Marshal(notif)
	if err != nil {
		return "", fmt.Errorf("marshal initialized notification: %w", err)
	}
	if err := c.post(ctx, sessionID, body); err != nil {
		return "", fmt.Errorf("send initialized notification: %w", err)
	}
	return sessionID, nil
}

// doRPC sends one JSON-RPC request on an already-initialized session.
func (c *Client) doRPC(ctx context.Context, sessionID string, req jsonrpcRequest) (*jsonrpcResponse, error) {
	_, resp, err := c.postJSONRPC(ctx, sessionID, req)
	return resp, err
}

// postJSONRPC marshals req, POSTs it, and parses the (possibly
// session-establishing) response. Returns the session id from the
// Mcp-Session-Id response header, if any.
func (c *Client) postJSONRPC(ctx context.Context, sessionID string, req jsonrpcRequest) (string, *jsonrpcResponse, error) {
	body, err := json.Marshal(req)
	if err != nil {
		return "", nil, fmt.Errorf("marshal request: %w", err)
	}

	httpReq, err := http.NewRequestWithContext(ctx, http.MethodPost, c.url, bytes.NewReader(body))
	if err != nil {
		return "", nil, fmt.Errorf("build request: %w", err)
	}
	httpReq.Header.Set("Content-Type", "application/json")
	httpReq.Header.Set("Accept", "application/json, text/event-stream")
	httpReq.Header.Set("MCP-Protocol-Version", mcpProtocolVersion)
	if sessionID != "" {
		httpReq.Header.Set("Mcp-Session-Id", sessionID)
	}

	httpResp, err := c.httpClient.Do(httpReq)
	if err != nil {
		return "", nil, fmt.Errorf("do request: %w", err)
	}
	defer httpResp.Body.Close()

	respBody, err := io.ReadAll(httpResp.Body)
	if err != nil {
		return "", nil, fmt.Errorf("read response body: %w", err)
	}
	if httpResp.StatusCode != http.StatusOK {
		return "", nil, fmt.Errorf("non-200 response: %d: %s", httpResp.StatusCode, truncate(string(respBody), 300))
	}

	newSessionID := httpResp.Header.Get("Mcp-Session-Id")
	if newSessionID == "" {
		newSessionID = sessionID
	}

	rpcBody, err := extractJSONRPCBody(httpResp.Header.Get("Content-Type"), respBody)
	if err != nil {
		return "", nil, err
	}

	var resp jsonrpcResponse
	if err := json.Unmarshal(rpcBody, &resp); err != nil {
		return "", nil, fmt.Errorf("malformed JSON-RPC response: %w", err)
	}
	return newSessionID, &resp, nil
}

// post sends a body with no response expected (JSON-RPC notification).
func (c *Client) post(ctx context.Context, sessionID string, body []byte) error {
	httpReq, err := http.NewRequestWithContext(ctx, http.MethodPost, c.url, bytes.NewReader(body))
	if err != nil {
		return fmt.Errorf("build request: %w", err)
	}
	httpReq.Header.Set("Content-Type", "application/json")
	httpReq.Header.Set("Accept", "application/json, text/event-stream")
	httpReq.Header.Set("MCP-Protocol-Version", mcpProtocolVersion)
	if sessionID != "" {
		httpReq.Header.Set("Mcp-Session-Id", sessionID)
	}

	httpResp, err := c.httpClient.Do(httpReq)
	if err != nil {
		return fmt.Errorf("do request: %w", err)
	}
	defer httpResp.Body.Close()
	io.Copy(io.Discard, httpResp.Body) //nolint:errcheck // draining is best-effort, not load-bearing
	// A notification legitimately gets 202 Accepted (no body) per the
	// streamable-HTTP spec; some servers may also reply 200. Anything else
	// is a real failure.
	if httpResp.StatusCode != http.StatusOK && httpResp.StatusCode != http.StatusAccepted {
		return fmt.Errorf("non-200/202 response: %d", httpResp.StatusCode)
	}
	return nil
}

// extractJSONRPCBody handles both response shapes the streamable-HTTP
// transport allows: a plain application/json body, or a single-event
// text/event-stream body carrying one "data: <json>" line.
func extractJSONRPCBody(contentType string, body []byte) ([]byte, error) {
	if !strings.HasPrefix(strings.TrimSpace(contentType), "text/event-stream") {
		return body, nil
	}
	for _, line := range strings.Split(string(body), "\n") {
		line = strings.TrimSpace(line)
		if data, ok := strings.CutPrefix(line, "data:"); ok {
			return []byte(strings.TrimSpace(data)), nil
		}
	}
	return nil, fmt.Errorf("SSE response carried no data: line")
}

func truncate(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return s[:n] + "...(truncated)"
}
