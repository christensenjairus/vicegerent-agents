package server

import (
	"context"
	"errors"
	"testing"

	"github.com/jchristensen/vicegerent-agents/images/mcp-cerbos-shim/internal/eval"
)

// pagerdutyIncidentResultServiceA mirrors get_incident's inferred REST-API-
// convention result shape (see upstream/pagerduty.go's own caveat: NOT
// verified against a live call). Every existing ack/resolve-scoping test
// below now also wires WithPagerdutyIncidentService(up) with this fixture --
// they only care that the call still reaches Cerbos with the right
// hasOutOfScopeChange, not about the new serviceIds attr this gate adds
// alongside it, so a single allowed-looking service keeps their existing
// assertions unaffected.
const pagerdutyIncidentResultServiceA = `{"service":{"id":"PSERVICEA"}}`

// These tests run the SHIPPED mapping (not a fixture) through the request path.
// They prove the wiring that turns a PagerDuty manage_incidents/
// add_note_to_incident call into the pagerduty_incident resource Cerbos
// restricts to ack/resolve-only field changes; the deny *decision* itself is
// proven by defs/pagerduty_test.yaml. manage_request here matches the real
// (flat) IncidentManageRequest schema of the pagerduty_manage_incidents MCP
// tool -- incident_ids/status/urgency/escalation_level/assignement -- not
// PagerDuty's raw REST API batch body.

func TestDeployedPagerdutyMapping_ManageIncidentsReachesCerbosWithStatus(t *testing.T) {
	m := deployedMapping(t)
	e, err := eval.Compile(m)
	if err != nil {
		t.Fatalf("compile: %v", err)
	}
	d := &stubDecider{allow: true}
	up := &fakeUpstream{text: pagerdutyIncidentResultServiceA}
	s := New(m, e, d, Principal{ID: "hermes", Roles: []string{"agent"}}, WithPagerdutyIncidentService(up))
	res, err := s.CheckRequest(context.Background(),
		mcpReq("vmcp", "tools/call", toolCall("pagerduty_manage_incidents",
			map[string]any{
				"manage_request": map[string]any{
					"incident_ids": []any{"PT1", "PT2"},
					"status":       "acknowledged",
				},
			})))
	if err != nil {
		t.Fatalf("CheckRequest: %v", err)
	}
	if !isPass(res) {
		t.Fatalf("expected pass when Cerbos allows, got deny")
	}
	if d.calls != 1 {
		t.Fatalf("expected exactly one Cerbos check, got %d", d.calls)
	}
	if d.gotType != "pagerduty_incident" {
		t.Errorf("resourceType = %q, want pagerduty_incident", d.gotType)
	}
	if d.gotAttr["hasOutOfScopeChange"] != "false" {
		t.Errorf("attr.hasOutOfScopeChange = %q, want false", d.gotAttr["hasOutOfScopeChange"])
	}
}

func TestDeployedPagerdutyMapping_ManageIncidentsTriggeredStatusFlagged(t *testing.T) {
	m := deployedMapping(t)
	e, err := eval.Compile(m)
	if err != nil {
		t.Fatalf("compile: %v", err)
	}
	d := &stubDecider{allow: false}
	up := &fakeUpstream{text: pagerdutyIncidentResultServiceA}
	s := New(m, e, d, Principal{ID: "hermes", Roles: []string{"agent"}}, WithPagerdutyIncidentService(up))
	res, err := s.CheckRequest(context.Background(),
		mcpReq("vmcp", "tools/call", toolCall("pagerduty_manage_incidents",
			map[string]any{
				"manage_request": map[string]any{
					"incident_ids": []any{"PT1"},
					"status":       "triggered",
				},
			})))
	if err != nil {
		t.Fatalf("CheckRequest: %v", err)
	}
	if !isDeny(res) {
		t.Fatalf("expected deny when Cerbos denies, got pass")
	}
	if d.gotAttr["hasOutOfScopeChange"] != "true" {
		t.Errorf("attr.hasOutOfScopeChange = %q, want true for status=triggered", d.gotAttr["hasOutOfScopeChange"])
	}
}

func TestDeployedPagerdutyMapping_ManageIncidentsUrgencyFlagged(t *testing.T) {
	// Regression test for the live bug found in production: urgency was not
	// checked by the previous (incidents[]-array-shaped) helper at all, so it
	// passed through Cerbos unchecked and reached PagerDuty's real API.
	m := deployedMapping(t)
	e, err := eval.Compile(m)
	if err != nil {
		t.Fatalf("compile: %v", err)
	}
	d := &stubDecider{allow: false}
	up := &fakeUpstream{text: pagerdutyIncidentResultServiceA}
	s := New(m, e, d, Principal{ID: "hermes", Roles: []string{"agent"}}, WithPagerdutyIncidentService(up))
	res, err := s.CheckRequest(context.Background(),
		mcpReq("vmcp", "tools/call", toolCall("pagerduty_manage_incidents",
			map[string]any{
				"manage_request": map[string]any{
					"incident_ids": []any{"PT1"},
					"status":       "acknowledged",
					"urgency":      "high",
				},
			})))
	if err != nil {
		t.Fatalf("CheckRequest: %v", err)
	}
	if !isDeny(res) {
		t.Fatalf("expected deny when Cerbos denies, got pass")
	}
	if d.gotAttr["hasOutOfScopeChange"] != "true" {
		t.Errorf("attr.hasOutOfScopeChange = %q, want true for a urgency change alongside an ack", d.gotAttr["hasOutOfScopeChange"])
	}
}

func TestDeployedPagerdutyMapping_ManageIncidentsEscalationLevelFlagged(t *testing.T) {
	// Regression test: escalation_level was only caught live by luck (PagerDuty
	// itself rejected the call because the target incident was already
	// resolved) -- against a triggered/acknowledged incident it would have
	// gone through. Cerbos must deny it directly regardless of PagerDuty's own
	// API-side validation.
	m := deployedMapping(t)
	e, err := eval.Compile(m)
	if err != nil {
		t.Fatalf("compile: %v", err)
	}
	d := &stubDecider{allow: false}
	up := &fakeUpstream{text: pagerdutyIncidentResultServiceA}
	s := New(m, e, d, Principal{ID: "hermes", Roles: []string{"agent"}}, WithPagerdutyIncidentService(up))
	res, err := s.CheckRequest(context.Background(),
		mcpReq("vmcp", "tools/call", toolCall("pagerduty_manage_incidents",
			map[string]any{
				"manage_request": map[string]any{
					"incident_ids":     []any{"PT1"},
					"escalation_level": 2,
				},
			})))
	if err != nil {
		t.Fatalf("CheckRequest: %v", err)
	}
	if !isDeny(res) {
		t.Fatalf("expected deny when Cerbos denies, got pass")
	}
	if d.gotAttr["hasOutOfScopeChange"] != "true" {
		t.Errorf("attr.hasOutOfScopeChange = %q, want true for an escalation_level change", d.gotAttr["hasOutOfScopeChange"])
	}
}

func TestDeployedPagerdutyMapping_AddNoteToIncidentReachesCerbos(t *testing.T) {
	m := deployedMapping(t)
	e, err := eval.Compile(m)
	if err != nil {
		t.Fatalf("compile: %v", err)
	}
	d := &stubDecider{allow: true}
	up := &fakeUpstream{text: pagerdutyIncidentResultServiceA}
	s := New(m, e, d, Principal{ID: "hermes", Roles: []string{"agent"}}, WithPagerdutyIncidentService(up))
	res, err := s.CheckRequest(context.Background(),
		mcpReq("vmcp", "tools/call", toolCall("pagerduty_add_note_to_incident",
			map[string]any{"incident_id": "PT4KHLK", "note": "Investigating"})))
	if err != nil {
		t.Fatalf("CheckRequest: %v", err)
	}
	if !isPass(res) {
		t.Fatalf("expected pass when Cerbos allows, got deny")
	}
	if d.gotType != "pagerduty_incident" {
		t.Errorf("resourceType = %q, want pagerduty_incident", d.gotType)
	}
	if d.gotAttr["incidentId"] != "PT4KHLK" {
		t.Errorf("attr.incidentId = %q, want PT4KHLK", d.gotAttr["incidentId"])
	}
}

// pagerdutyIncidentResultServiceB is a second distinct service id, used to
// prove the allowlist actually discriminates rather than always passing.
const pagerdutyIncidentResultServiceB = `{"service":{"id":"PSERVICEB"}}`

func TestDeployedPagerdutyMapping_ServiceGateInjectsServiceIdsAttr(t *testing.T) {
	m := deployedMapping(t)
	e, err := eval.Compile(m)
	if err != nil {
		t.Fatalf("compile: %v", err)
	}
	d := &stubDecider{allow: true}
	up := &fakeUpstream{text: pagerdutyIncidentResultServiceA}
	s := New(m, e, d, Principal{ID: "hermes", Roles: []string{"agent"}}, WithPagerdutyIncidentService(up))
	res, err := s.CheckRequest(context.Background(),
		mcpReq("vmcp", "tools/call", toolCall("pagerduty_manage_incidents",
			map[string]any{
				"manage_request": map[string]any{
					"incident_ids": []any{"PT1", "PT2"},
					"status":       "acknowledged",
				},
			})))
	if err != nil {
		t.Fatalf("CheckRequest: %v", err)
	}
	if !isPass(res) {
		t.Fatalf("expected pass when Cerbos allows, got deny")
	}
	if up.calls != 2 {
		t.Errorf("expected exactly two get_incident lookups (one per targeted incident), got %d", up.calls)
	}
	gotServiceIds, ok := d.gotAttr["serviceIds"].([]string)
	if !ok || len(gotServiceIds) != 2 || gotServiceIds[0] != "PSERVICEA" || gotServiceIds[1] != "PSERVICEA" {
		t.Errorf("attr.serviceIds = %#v, want [PSERVICEA PSERVICEA] (both incidents resolved to the same service)", d.gotAttr["serviceIds"])
	}
}

func TestDeployedPagerdutyMapping_AddNoteServiceGateInjectsServiceIdsAttr(t *testing.T) {
	m := deployedMapping(t)
	e, err := eval.Compile(m)
	if err != nil {
		t.Fatalf("compile: %v", err)
	}
	d := &stubDecider{allow: true}
	up := &fakeUpstream{text: pagerdutyIncidentResultServiceB}
	s := New(m, e, d, Principal{ID: "hermes", Roles: []string{"agent"}}, WithPagerdutyIncidentService(up))
	res, err := s.CheckRequest(context.Background(),
		mcpReq("vmcp", "tools/call", toolCall("pagerduty_add_note_to_incident",
			map[string]any{"incident_id": "PT4KHLK", "note": "Investigating"})))
	if err != nil {
		t.Fatalf("CheckRequest: %v", err)
	}
	if !isPass(res) {
		t.Fatalf("expected pass when Cerbos allows, got deny")
	}
	if up.calls != 1 {
		t.Errorf("expected exactly one get_incident lookup, got %d", up.calls)
	}
	gotServiceIds, ok := d.gotAttr["serviceIds"].([]string)
	if !ok || len(gotServiceIds) != 1 || gotServiceIds[0] != "PSERVICEB" {
		t.Errorf("attr.serviceIds = %#v, want [PSERVICEB]", d.gotAttr["serviceIds"])
	}
}

func TestDeployedPagerdutyMapping_ServiceLookupFailureFailsClosed(t *testing.T) {
	m := deployedMapping(t)
	e, err := eval.Compile(m)
	if err != nil {
		t.Fatalf("compile: %v", err)
	}
	// allow=true: proves the deny comes from the shim's own fail-closed gate,
	// not from Cerbos -- Cerbos is never even reached.
	d := &stubDecider{allow: true}
	up := &fakeUpstream{err: errors.New("connection refused")}
	s := New(m, e, d, Principal{ID: "hermes", Roles: []string{"agent"}}, WithPagerdutyIncidentService(up))
	res, err := s.CheckRequest(context.Background(),
		mcpReq("vmcp", "tools/call", toolCall("pagerduty_manage_incidents",
			map[string]any{
				"manage_request": map[string]any{
					"incident_ids": []any{"PT1"},
					"status":       "acknowledged",
				},
			})))
	if err != nil {
		t.Fatalf("CheckRequest: %v", err)
	}
	if !isDeny(res) {
		t.Fatalf("expected deny on lookup failure (fail closed), got pass")
	}
	if d.calls != 0 {
		t.Errorf("expected Cerbos never reached on a fail-closed lookup error, got %d calls", d.calls)
	}
}

func TestDeployedPagerdutyMapping_UnconfiguredServiceGateFailsClosed(t *testing.T) {
	m := deployedMapping(t)
	e, err := eval.Compile(m)
	if err != nil {
		t.Fatalf("compile: %v", err)
	}
	d := &stubDecider{allow: true}
	// No WithPagerdutyIncidentService: production's main.go always wires it,
	// so reaching here unconfigured means a broken deploy, not a license to
	// allow an unscoped incident write through -- same posture as the Notion
	// ancestry gate's unconfigured-gate test.
	s := New(m, e, d, Principal{ID: "hermes", Roles: []string{"agent"}})
	res, err := s.CheckRequest(context.Background(),
		mcpReq("vmcp", "tools/call", toolCall("pagerduty_manage_incidents",
			map[string]any{
				"manage_request": map[string]any{
					"incident_ids": []any{"PT1"},
					"status":       "acknowledged",
				},
			})))
	if err != nil {
		t.Fatalf("CheckRequest: %v", err)
	}
	if !isDeny(res) {
		t.Fatalf("expected deny with the service gate unconfigured (fail closed), got pass")
	}
	if d.calls != 0 {
		t.Errorf("expected Cerbos never reached with the gate unconfigured, got %d calls", d.calls)
	}
}

func TestDeployedPagerdutyMapping_GovBackendReachesCerbosViaItsOwnGetIncidentTool(t *testing.T) {
	// pagerduty_gov mirrors pagerduty (toolhive-servers.json) and must get the
	// SAME ack/resolve-only + service-allowlist scoping, but via ITS OWN
	// get_incident tool -- an incident in the gov account doesn't exist in the
	// commercial one, so querying the wrong tool would fail-closed-deny every
	// gov manage_incidents call. fakeUpstream records exactly which tool name
	// it was called with, proving the routing (not just the mapping) is right.
	m := deployedMapping(t)
	e, err := eval.Compile(m)
	if err != nil {
		t.Fatalf("compile: %v", err)
	}
	d := &stubDecider{allow: true}
	up := &fakeUpstream{text: pagerdutyIncidentResultServiceA}
	s := New(m, e, d, Principal{ID: "hermes", Roles: []string{"agent"}}, WithPagerdutyIncidentService(up))
	res, err := s.CheckRequest(context.Background(),
		mcpReq("vmcp", "tools/call", toolCall("pagerduty_gov_manage_incidents",
			map[string]any{
				"manage_request": map[string]any{
					"incident_ids": []any{"PT1"},
					"status":       "acknowledged",
				},
			})))
	if err != nil {
		t.Fatalf("CheckRequest: %v", err)
	}
	if !isPass(res) {
		t.Fatalf("expected pass when Cerbos allows, got deny")
	}
	if d.gotType != "pagerduty_incident" {
		t.Errorf("resourceType = %q, want pagerduty_incident (mapping must resolve pagerduty_gov_manage_incidents)", d.gotType)
	}
	if up.gotTool != "pagerduty_gov_get_incident" {
		t.Errorf("get_incident lookup used tool %q, want pagerduty_gov_get_incident (looked up the wrong backend's incident)", up.gotTool)
	}
	gotServiceIds, ok := d.gotAttr["serviceIds"].([]string)
	if !ok || len(gotServiceIds) != 1 || gotServiceIds[0] != "PSERVICEA" {
		t.Errorf("attr.serviceIds = %#v, want [PSERVICEA]", d.gotAttr["serviceIds"])
	}
}

func TestDeployedPagerdutyMapping_GovAddNoteReachesCerbosViaItsOwnGetIncidentTool(t *testing.T) {
	m := deployedMapping(t)
	e, err := eval.Compile(m)
	if err != nil {
		t.Fatalf("compile: %v", err)
	}
	d := &stubDecider{allow: true}
	up := &fakeUpstream{text: pagerdutyIncidentResultServiceB}
	s := New(m, e, d, Principal{ID: "hermes", Roles: []string{"agent"}}, WithPagerdutyIncidentService(up))
	res, err := s.CheckRequest(context.Background(),
		mcpReq("vmcp", "tools/call", toolCall("pagerduty_gov_add_note_to_incident",
			map[string]any{"incident_id": "PT4KHLK", "note": "Investigating"})))
	if err != nil {
		t.Fatalf("CheckRequest: %v", err)
	}
	if !isPass(res) {
		t.Fatalf("expected pass when Cerbos allows, got deny")
	}
	if up.gotTool != "pagerduty_gov_get_incident" {
		t.Errorf("get_incident lookup used tool %q, want pagerduty_gov_get_incident", up.gotTool)
	}
	gotServiceIds, ok := d.gotAttr["serviceIds"].([]string)
	if !ok || len(gotServiceIds) != 1 || gotServiceIds[0] != "PSERVICEB" {
		t.Errorf("attr.serviceIds = %#v, want [PSERVICEB]", d.gotAttr["serviceIds"])
	}
}

func TestDeployedPagerdutyMapping_NoIncidentIdsSkipsGateAndReachesCerbosUnaffected(t *testing.T) {
	// manage_incidents with an empty incident_ids array has nothing to
	// resolve -- the gate must not fire (and must not deny), same
	// fail-open-when-genuinely-nothing-to-check posture as the Linear
	// save_comment gate's no-issueId case.
	m := deployedMapping(t)
	e, err := eval.Compile(m)
	if err != nil {
		t.Fatalf("compile: %v", err)
	}
	d := &stubDecider{allow: true}
	// No WithPagerdutyIncidentService configured at all -- proves the gate
	// genuinely never fires here (an unconfigured gate would otherwise deny,
	// per the test above).
	s := New(m, e, d, Principal{ID: "hermes", Roles: []string{"agent"}})
	res, err := s.CheckRequest(context.Background(),
		mcpReq("vmcp", "tools/call", toolCall("pagerduty_manage_incidents",
			map[string]any{
				"manage_request": map[string]any{
					"incident_ids": []any{},
					"status":       "acknowledged",
				},
			})))
	if err != nil {
		t.Fatalf("CheckRequest: %v", err)
	}
	if !isPass(res) {
		t.Fatalf("expected pass: no incident ids to resolve, gate must not fire")
	}
	if d.calls != 1 {
		t.Fatalf("expected the call to still reach Cerbos once (just without a serviceIds attr), got %d", d.calls)
	}
	if _, hasServiceIds := d.gotAttr["serviceIds"]; hasServiceIds {
		t.Errorf("attr.serviceIds should be absent when there are no incident ids to resolve, got %#v", d.gotAttr["serviceIds"])
	}
}
