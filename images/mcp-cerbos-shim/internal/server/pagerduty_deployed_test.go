package server

import (
	"context"
	"testing"

	"github.com/jchristensen/vicegerent-agents/images/mcp-cerbos-shim/internal/eval"
)

// These tests run the SHIPPED mapping (not a fixture) through the request path.
// They prove the wiring that turns a PagerDuty manage_incidents/
// add_note_to_incident call into the pagerduty_incident resource Cerbos
// restricts to ack/resolve-only field changes; the deny *decision* itself is
// proven by defs/pagerduty_test.yaml.

func TestDeployedPagerdutyMapping_ManageIncidentsReachesCerbosWithStatus(t *testing.T) {
	m := deployedMapping(t)
	e, err := eval.Compile(m)
	if err != nil {
		t.Fatalf("compile: %v", err)
	}
	d := &stubDecider{allow: true}
	s := New(m, e, d, Principal{ID: "hermes", Roles: []string{"agent"}})
	res, err := s.CheckRequest(context.Background(),
		mcpReq("vmcp", "tools/call", toolCall("pagerduty_manage_incidents",
			map[string]any{
				"manage_request": map[string]any{
					"incidents": []any{
						map[string]any{"id": "PT1", "type": "incident_reference", "status": "acknowledged"},
						map[string]any{"id": "PT2", "type": "incident_reference", "status": "resolved", "resolution": "fixed"},
					},
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
	s := New(m, e, d, Principal{ID: "hermes", Roles: []string{"agent"}})
	res, err := s.CheckRequest(context.Background(),
		mcpReq("vmcp", "tools/call", toolCall("pagerduty_manage_incidents",
			map[string]any{
				"manage_request": map[string]any{
					"incidents": []any{
						map[string]any{"id": "PT1", "type": "incident_reference", "status": "triggered"},
					},
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

func TestDeployedPagerdutyMapping_ManageIncidentsTitleChangeFlagged(t *testing.T) {
	m := deployedMapping(t)
	e, err := eval.Compile(m)
	if err != nil {
		t.Fatalf("compile: %v", err)
	}
	d := &stubDecider{allow: false}
	s := New(m, e, d, Principal{ID: "hermes", Roles: []string{"agent"}})
	res, err := s.CheckRequest(context.Background(),
		mcpReq("vmcp", "tools/call", toolCall("pagerduty_manage_incidents",
			map[string]any{
				"manage_request": map[string]any{
					"incidents": []any{
						map[string]any{"id": "PT1", "type": "incident_reference", "status": "acknowledged", "title": "New title"},
					},
				},
			})))
	if err != nil {
		t.Fatalf("CheckRequest: %v", err)
	}
	if !isDeny(res) {
		t.Fatalf("expected deny when Cerbos denies, got pass")
	}
	if d.gotAttr["hasOutOfScopeChange"] != "true" {
		t.Errorf("attr.hasOutOfScopeChange = %q, want true for a title change alongside an ack", d.gotAttr["hasOutOfScopeChange"])
	}
}

func TestDeployedPagerdutyMapping_AddNoteToIncidentReachesCerbos(t *testing.T) {
	m := deployedMapping(t)
	e, err := eval.Compile(m)
	if err != nil {
		t.Fatalf("compile: %v", err)
	}
	d := &stubDecider{allow: true}
	s := New(m, e, d, Principal{ID: "hermes", Roles: []string{"agent"}})
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
