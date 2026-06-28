package handlers

import (
	"context"
	"fmt"
	"os"
	"runtime/debug"

	"github.com/mark3labs/mcp-go/mcp"
)

// SafeHandler wraps a tool handler with panic recovery so that a panic in any
// handler returns an error to the caller instead of crashing the server process.
// Without this, a single panicking goroutine (e.g. nil pointer in an error path
// under concurrent load) takes down the entire stdio/SSE/HTTP server.
func SafeHandler(h func(ctx context.Context, r mcp.CallToolRequest) (*mcp.CallToolResult, error)) func(ctx context.Context, r mcp.CallToolRequest) (*mcp.CallToolResult, error) {
	return func(ctx context.Context, r mcp.CallToolRequest) (result *mcp.CallToolResult, err error) {
		defer func() {
			if rec := recover(); rec != nil {
				fmt.Fprintf(os.Stderr, "panic in handler %q: %v\n%s\n", r.Params.Name, rec, debug.Stack())
				err = fmt.Errorf("internal server error: %v", rec)
			}
		}()
		return h(ctx, r)
	}
}
