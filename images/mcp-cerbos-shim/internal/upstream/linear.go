package upstream

import (
	"context"
	"encoding/json"
	"fmt"
)

// linearGetIssueTool is the vMCP tool name for Linear's read-only issue
// fetch -- backend-prefixed the same way mapping.yaml keys its tools
// (linear_get_issue), since that's the name this call re-enters CheckRequest
// as. Kept unmapped in Cerbos for the same recursion-safety reason
// notion_notion-fetch is documented in client.go's package doc: a future
// deny rule on this tool would make every save_comment team lookup fail
// closed (not silently allow -- IssueTeam already returns an error on any
// lookup failure), but it would look like an unrelated regression if
// someone maps it without reading this comment first.
const linearGetIssueTool = "linear_get_issue"

// linearIssueResult is the subset of linear_get_issue's JSON result this
// package needs. The live tool result carries "team" as the team's display
// name directly at the top level (verified live against the real vMCP
// route, HAH-69) -- e.g. {"id":"HAH-69",...,"team":"HAHomelabs",...}. This
// is a single JSON object, NOT the double-JSON-wrapped shape Notion's
// notion-fetch uses (see ancestry.go's notionFetchEnvelope) -- Linear's own
// MCP server returns its tool result as plain JSON text, no extra nesting.
type linearIssueResult struct {
	Team string `json:"team"`
}

// IssueTeam resolves a Linear issue/comment-parent id (e.g. "HAH-69") to its
// team's display name via ONE linear_get_issue call. Returns an error on any
// lookup failure (timeout, non-200, malformed result, tool-reported error,
// or an issue with no team) so the caller can fail closed -- mirrors
// PageIsUnderAnyAncestor's contract in ancestry.go.
func IssueTeam(ctx context.Context, client ToolCaller, issueID string) (string, error) {
	result, err := client.CallTool(ctx, linearGetIssueTool, map[string]any{"id": issueID})
	if err != nil {
		return "", fmt.Errorf("linear issue team lookup for %q: %w", issueID, err)
	}
	var parsed linearIssueResult
	if err := json.Unmarshal([]byte(result.Text()), &parsed); err != nil {
		return "", fmt.Errorf("linear issue team lookup for %q: malformed get_issue result: %w", issueID, err)
	}
	if parsed.Team == "" {
		return "", fmt.Errorf("linear issue team lookup for %q: get_issue result has no team", issueID)
	}
	return parsed.Team, nil
}
