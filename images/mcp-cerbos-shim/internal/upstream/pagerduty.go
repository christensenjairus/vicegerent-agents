package upstream

import (
	"context"
	"encoding/json"
	"fmt"
)

// pagerdutyGetIncidentTool is the vMCP tool name for PagerDuty's read-only
// incident fetch -- backend-prefixed the same way mapping.yaml keys its
// tools (pagerduty_get_incident), since that's the name this call re-enters
// CheckRequest as. Kept unmapped in Cerbos for the same recursion-safety
// reason notion_notion-fetch/linear_get_issue are documented elsewhere in
// this package: a future deny rule on this tool would make every
// manage_incidents/add_note_to_incident service lookup fail closed instead
// of the intended per-call fail-closed behavior tied to the actual service
// scoping check.
const pagerdutyGetIncidentTool = "pagerduty_get_incident"

// pagerdutyIncidentResult is the subset of get_incident's JSON result this
// package needs. PagerDuty's REST API models an incident as always
// belonging to exactly one service (a required, non-nullable relationship in
// PagerDuty's own data model -- an incident cannot exist without a service),
// represented as a nested {"id": ..., "summary": ...} reference object, the
// same shape PagerDuty's public REST API uses for every such reference
// throughout its schema (services, escalation policies, teams, etc.).
//
// NOTE: this field shape is inferred from PagerDuty's documented REST API
// conventions, NOT verified against a live call to this specific MCP tool
// (unlike linear.go's IssueTeam/ProjectTeams, which were confirmed
// against real live responses) -- this sandbox has no PagerDuty credentials
// to test against. If get_incident's actual result nests the service
// reference differently, IncidentServiceID below fails closed (empty/
// malformed service resolves to an error, never a silent pass), so a shape
// mismatch denies every gated call rather than letting one through
// unchecked. Live verification against a real PagerDuty account is a
// mandatory follow-up before relying on this in production -- see the MR's
// own follow-up section.
type pagerdutyIncidentResult struct {
	Service struct {
		ID string `json:"id"`
	} `json:"service"`
}

// IncidentServiceID resolves a PagerDuty incident id/number to its owning
// service id via ONE get_incident call. Returns an error on any lookup
// failure (timeout, non-200, malformed result, tool-reported error, or an
// incident with no resolvable service id) so the caller can fail closed --
// mirrors IssueTeam/ProjectTeams's contract in linear.go.
func IncidentServiceID(ctx context.Context, client ToolCaller, incidentID string) (string, error) {
	result, err := client.CallTool(ctx, pagerdutyGetIncidentTool, map[string]any{"incident_id": incidentID})
	if err != nil {
		return "", fmt.Errorf("pagerduty incident service lookup for %q: %w", incidentID, err)
	}
	var parsed pagerdutyIncidentResult
	if err := json.Unmarshal([]byte(result.Text()), &parsed); err != nil {
		return "", fmt.Errorf("pagerduty incident service lookup for %q: malformed get_incident result: %w", incidentID, err)
	}
	if parsed.Service.ID == "" {
		return "", fmt.Errorf("pagerduty incident service lookup for %q: get_incident result has no resolvable service id", incidentID)
	}
	return parsed.Service.ID, nil
}
