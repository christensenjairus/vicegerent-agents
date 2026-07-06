package upstream

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

// rpcRequest is the subset of a JSON-RPC request test handlers need to
// distinguish initialize/notifications/initialized/tools/call.
type rpcRequest struct {
	Method string          `json:"method"`
	ID     *int            `json:"id"`
	Params json.RawMessage `json:"params"`
}

// newTestServer wires a handler that plays MCP server: responds to
// initialize with a session id, accepts the initialized notification, and
// calls toolCallHandler for tools/call.
func newTestServer(t *testing.T, toolCallHandler func(name string, args map[string]any) (int, string)) *httptest.Server {
	t.Helper()
	const sessionID = "test-session-123"
	return httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body := make([]byte, r.ContentLength)
		_, _ = r.Body.Read(body)
		var req rpcRequest
		if err := json.Unmarshal(body, &req); err != nil {
			w.WriteHeader(http.StatusBadRequest)
			return
		}
		switch req.Method {
		case "initialize":
			w.Header().Set("Mcp-Session-Id", sessionID)
			w.Header().Set("Content-Type", "application/json")
			fmt.Fprintf(w, `{"jsonrpc":"2.0","id":%d,"result":{"protocolVersion":"2025-06-18"}}`, *req.ID)
		case "notifications/initialized":
			w.WriteHeader(http.StatusAccepted)
		case "tools/call":
			// Mirrors the real vMCP optimizer (on by default -- see
			// callToolMetaName's doc in client.go): the client always wraps
			// the real tool name/args in call_tool{tool_name, parameters},
			// so this harness unwraps that same shape rather than reading a
			// raw name/arguments pair.
			var outer struct {
				Name      string `json:"name"`
				Arguments struct {
					ToolName   string         `json:"tool_name"`
					Parameters map[string]any `json:"parameters"`
				} `json:"arguments"`
			}
			_ = json.Unmarshal(req.Params, &outer)
			if outer.Name != callToolMetaName {
				w.WriteHeader(http.StatusBadRequest)
				return
			}
			status, text := toolCallHandler(outer.Arguments.ToolName, outer.Arguments.Parameters)
			w.Header().Set("Content-Type", "application/json")
			if status != http.StatusOK {
				w.WriteHeader(status)
				return
			}
			fmt.Fprintf(w, `{"jsonrpc":"2.0","id":%d,"result":{"content":[{"type":"text","text":%q}]}}`, *req.ID, text)
		default:
			w.WriteHeader(http.StatusNotFound)
		}
	}))
}

func TestCallTool_Success(t *testing.T) {
	srv := newTestServer(t, func(name string, args map[string]any) (int, string) {
		if name != "notion_notion-fetch" {
			t.Errorf("unexpected tool name %q", name)
		}
		return http.StatusOK, "# Some Page\nParent: <page url=\"https://notion.so/Parent-abc123\">Parent</page>\nbody"
	})
	defer srv.Close()

	c := New(srv.URL, nil)
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	result, err := c.CallTool(ctx, "notion_notion-fetch", map[string]any{"id": "pageid"})
	if err != nil {
		t.Fatalf("CallTool: %v", err)
	}
	if !strings.Contains(result.Text(), "Parent:") {
		t.Errorf("unexpected result text: %q", result.Text())
	}
}

func TestCallTool_Timeout(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		time.Sleep(200 * time.Millisecond)
		w.WriteHeader(http.StatusOK)
	}))
	defer srv.Close()

	c := New(srv.URL, nil)
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Millisecond)
	defer cancel()
	_, err := c.CallTool(ctx, "notion_notion-fetch", map[string]any{"id": "x"})
	if err == nil {
		t.Fatal("expected timeout error, got nil")
	}
}

func TestCallTool_NonOKStatus(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusInternalServerError)
		fmt.Fprint(w, "boom")
	}))
	defer srv.Close()

	c := New(srv.URL, nil)
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	_, err := c.CallTool(ctx, "notion_notion-fetch", map[string]any{"id": "x"})
	if err == nil {
		t.Fatal("expected error for non-200 initialize response")
	}
}

func TestCallTool_MalformedJSONRPC(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		fmt.Fprint(w, "{not json")
	}))
	defer srv.Close()

	c := New(srv.URL, nil)
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	_, err := c.CallTool(ctx, "notion_notion-fetch", map[string]any{"id": "x"})
	if err == nil {
		t.Fatal("expected error for malformed JSON-RPC body")
	}
}

func TestCallTool_JSONRPCErrorResponse(t *testing.T) {
	const sessionID = "sess"
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body := make([]byte, r.ContentLength)
		_, _ = r.Body.Read(body)
		var req rpcRequest
		_ = json.Unmarshal(body, &req)
		w.Header().Set("Content-Type", "application/json")
		switch req.Method {
		case "initialize":
			w.Header().Set("Mcp-Session-Id", sessionID)
			fmt.Fprintf(w, `{"jsonrpc":"2.0","id":%d,"result":{}}`, *req.ID)
		case "notifications/initialized":
			w.WriteHeader(http.StatusAccepted)
		case "tools/call":
			fmt.Fprintf(w, `{"jsonrpc":"2.0","id":%d,"error":{"code":-32000,"message":"tool not found"}}`, *req.ID)
		}
	}))
	defer srv.Close()

	c := New(srv.URL, nil)
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	_, err := c.CallTool(ctx, "notion_notion-fetch", map[string]any{"id": "x"})
	if err == nil {
		t.Fatal("expected error for JSON-RPC error response")
	}
	if !strings.Contains(err.Error(), "tool not found") {
		t.Errorf("error should surface JSON-RPC message, got: %v", err)
	}
}

func TestCallTool_ToolReportedError(t *testing.T) {
	const sessionID = "sess"
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body := make([]byte, r.ContentLength)
		_, _ = r.Body.Read(body)
		var req rpcRequest
		_ = json.Unmarshal(body, &req)
		w.Header().Set("Content-Type", "application/json")
		switch req.Method {
		case "initialize":
			w.Header().Set("Mcp-Session-Id", sessionID)
			fmt.Fprintf(w, `{"jsonrpc":"2.0","id":%d,"result":{}}`, *req.ID)
		case "notifications/initialized":
			w.WriteHeader(http.StatusAccepted)
		case "tools/call":
			fmt.Fprintf(w, `{"jsonrpc":"2.0","id":%d,"result":{"content":[{"type":"text","text":"page not found"}],"isError":true}}`, *req.ID)
		}
	}))
	defer srv.Close()

	c := New(srv.URL, nil)
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	_, err := c.CallTool(ctx, "notion_notion-fetch", map[string]any{"id": "x"})
	if err == nil {
		t.Fatal("expected error when tool result isError")
	}
}

func TestExtractJSONRPCBody_SSE(t *testing.T) {
	body := "event: message\ndata: {\"jsonrpc\":\"2.0\",\"id\":1,\"result\":{}}\n\n"
	got, err := extractJSONRPCBody("text/event-stream", []byte(body))
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	var resp jsonrpcResponse
	if err := json.Unmarshal(got, &resp); err != nil {
		t.Fatalf("extracted body not valid JSON: %v", err)
	}
}
