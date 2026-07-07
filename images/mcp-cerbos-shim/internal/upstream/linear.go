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
// route) -- e.g. {"id":"PROJ-69",...,"team":"HAHomelabs",...}. This
// is a single JSON object, NOT the double-JSON-wrapped shape Notion's
// notion-fetch uses (see ancestry.go's notionFetchEnvelope) -- Linear's own
// MCP server returns its tool result as plain JSON text, no extra nesting.
type linearIssueResult struct {
	Team string `json:"team"`
}

// IssueTeam resolves a Linear issue/comment-parent id (e.g. "PROJ-69") to its
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

// linearGetProjectTool is the vMCP tool name for Linear's read-only project
// fetch, same recursion-safety posture as linearGetIssueTool above: keep it
// unmapped in Cerbos, or any future deny rule on it will silently fail-closed
// every save_project update-team lookup instead of the intended
// per-call fail-closed behavior tied to the actual project team check.
const linearGetProjectTool = "linear_get_project"

// linearProjectResult is the subset of linear_get_project's JSON result this
// package needs -- a project can belong to more than one team (verified live:
// e.g. "SN Support for Azure Re-platform Effort" carries just Infrastructure,
// "Database Migration Workflow and Visiblity" carries both DevOps and
// Infrastructure), so unlike linear_get_issue's single "team" string this is
// an array of {id, name, key} objects. Only "name" is used, to stay
// consistent with linearProjectAttrOption's existing addTeams/setTeams
// handling (which also compares by whatever form the caller supplied,
// resolved against ${linearAllowedTeams}'s three-identifier-form allowlist).
type linearProjectResult struct {
	Teams []struct {
		Name string `json:"name"`
	} `json:"teams"`
}

// ProjectTeams resolves a Linear project id/slug to the display names of
// every team it currently belongs to, via ONE linear_get_project call.
// Returns an error on any lookup failure (timeout, non-200, malformed
// result, tool-reported error) so the caller can fail closed -- mirrors
// IssueTeam's contract above. A project with zero teams is a genuine
// Linear API invariant violation (every project requires at least one team
// on creation), so an empty result also fails closed rather than silently
// passing an empty teams list through as "nothing to check."
func ProjectTeams(ctx context.Context, client ToolCaller, projectID string) ([]string, error) {
	result, err := client.CallTool(ctx, linearGetProjectTool, map[string]any{"query": projectID})
	if err != nil {
		return nil, fmt.Errorf("linear project team lookup for %q: %w", projectID, err)
	}
	var parsed linearProjectResult
	if err := json.Unmarshal([]byte(result.Text()), &parsed); err != nil {
		return nil, fmt.Errorf("linear project team lookup for %q: malformed get_project result: %w", projectID, err)
	}
	if len(parsed.Teams) == 0 {
		return nil, fmt.Errorf("linear project team lookup for %q: get_project result has no teams", projectID)
	}
	teams := make([]string, 0, len(parsed.Teams))
	for _, t := range parsed.Teams {
		teams = append(teams, t.Name)
	}
	return teams, nil
}
