package upstream

import (
	"context"
	"encoding/json"
	"fmt"
)

// pagerdutyIncidentResult is the subset of get_incident's JSON result this
// package needs. PagerDuty's REST API models an incident as always
// belonging to exactly one service (a required, non-nullable relationship in
// PagerDuty's own data model -- an incident cannot exist without a service),
// represented as a nested {"id": ..., "summary": ...} reference object, the
// same shape PagerDuty's public REST API uses for every such reference
// throughout its schema (services, escalation policies, teams, etc.).
//
// NOTE: this field shape was originally inferred from PagerDuty's documented
// REST API conventions, NOT verified against a live call to this specific
// MCP tool (unlike linear.go's IssueTeam/ProjectTeams, which were confirmed
// against real live responses) -- this sandbox has no PagerDuty credentials
// to test against. It's since been confirmed against pagerduty-mcp 1.1.0's
// own source: Incident.service is typed as ServiceReference (id required) in
// models/incidents.py, and that package's own test fixture
// (tests/test_incidents.py) uses exactly {"service": {"id": "PSERVICE123",
// "type": "service_reference"}} as a get_incident result. A live smoke-test
// against a real account is still worth doing (source-confirmed isn't the
// same as watching a real response go by), but this is no longer a blocking
// gap -- if get_incident's actual result nests the service reference
// differently than upstream's own model/fixtures show, IncidentServiceID
// below fails closed (empty/malformed service resolves to an error, never a
// silent pass), so a shape mismatch denies every gated call rather than
// letting one through unchecked.
type pagerdutyIncidentResult struct {
	Service struct {
		ID string `json:"id"`
	} `json:"service"`
}

// IncidentServiceID resolves a PagerDuty incident id/number to its owning
// service id via ONE get_incident call, against getIncidentTool -- the
// caller's job to pass the SAME backend's own get_incident tool name
// (e.g. "pagerduty_gov_get_incident" for a pagerduty_gov-originated call),
// since this shim fronts more than one PagerDuty account and an incident
// only exists in the one it actually belongs to. That tool stays unmapped
// in Cerbos for every backend, for the same recursion-safety reason
// notion_notion-fetch/linear_get_issue are documented elsewhere in this
// package: a deny rule on it would make every manage_incidents/
// add_note_to_incident service lookup fail closed unconditionally instead
// of the intended per-call, service-scoping-tied check.
//
// Returns an error on any lookup failure (timeout, non-200, malformed
// result, tool-reported error, or an incident with no resolvable service
// id) so the caller can fail closed -- mirrors IssueTeam/ProjectTeams's
// contract in linear.go.
func IncidentServiceID(ctx context.Context, getIncidentTool string, client ToolCaller, incidentID string) (string, error) {
	result, err := client.CallTool(ctx, getIncidentTool, map[string]any{"incident_id": incidentID})
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
