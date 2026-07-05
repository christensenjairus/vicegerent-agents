package eval

import (
	"testing"

	config "github.com/jchristensen/vicegerent-agents/images/mcp-cerbos-shim/internal"
)

func TestPagerdutyHelperSelfRegisters(t *testing.T) {
	if _, ok := helperOptions("pagerdutyManageAttr"); !ok {
		t.Fatal("pagerdutyManageAttr not registered; helpers_pagerduty.go init() did not run")
	}
}

func compilePagerdutyTestEngine(t *testing.T) *Engine {
	t.Helper()
	m := &config.Mapping{
		Backends: map[string]config.Backend{
			"vmcp": {
				DefaultAction: config.ActionAllow,
				Helpers:       []string{"pagerdutyManageAttr"},
				Tools: map[string]config.Tool{
					"pagerduty_manage_incidents": {
						ResourceType: "pagerduty_incident",
						Action:       "manage",
						ID:           "'manage_incidents'",
						AttrFrom:     "pagerdutyManageAttr(args)",
					},
				},
			},
		},
	}
	e, err := Compile(m)
	if err != nil {
		t.Fatalf("compile: %v", err)
	}
	return e
}

func TestPagerdutyManageAttr_AckResolveOnlyBatch(t *testing.T) {
	e := compilePagerdutyTestEngine(t)
	res, err := e.Eval(CallInput{
		Backend: "vmcp",
		Tool:    "pagerduty_manage_incidents",
		Args: map[string]any{
			"manage_request": map[string]any{
				"incidents": []any{
					map[string]any{"id": "PT1", "type": "incident_reference", "status": "acknowledged"},
					map[string]any{"id": "PT2", "type": "incident_reference", "status": "resolved", "resolution": "fixed via rollback"},
				},
			},
		},
	})
	if err != nil {
		t.Fatalf("Eval: %v", err)
	}
	if res.Attr["hasOutOfScopeChange"] != "false" {
		t.Errorf("hasOutOfScopeChange = %q, want false", res.Attr["hasOutOfScopeChange"])
	}
}

func TestPagerdutyManageAttr_LargeAckOnlyBatchStillAllowed(t *testing.T) {
	// No bulk-count cap: a large batch of pure acks is not out of scope.
	e := compilePagerdutyTestEngine(t)
	incidents := make([]any, 0, 50)
	for i := 0; i < 50; i++ {
		incidents = append(incidents, map[string]any{"id": "PT", "type": "incident_reference", "status": "acknowledged"})
	}
	res, err := e.Eval(CallInput{
		Backend: "vmcp",
		Tool:    "pagerduty_manage_incidents",
		Args: map[string]any{
			"manage_request": map[string]any{"incidents": incidents},
		},
	})
	if err != nil {
		t.Fatalf("Eval: %v", err)
	}
	if res.Attr["hasOutOfScopeChange"] != "false" {
		t.Errorf("hasOutOfScopeChange = %q, want false for a large but ack-only batch", res.Attr["hasOutOfScopeChange"])
	}
}

func TestPagerdutyManageAttr_TriggeredStatusFlagged(t *testing.T) {
	e := compilePagerdutyTestEngine(t)
	res, err := e.Eval(CallInput{
		Backend: "vmcp",
		Tool:    "pagerduty_manage_incidents",
		Args: map[string]any{
			"manage_request": map[string]any{
				"incidents": []any{
					map[string]any{"id": "PT1", "type": "incident_reference", "status": "triggered"},
				},
			},
		},
	})
	if err != nil {
		t.Fatalf("Eval: %v", err)
	}
	if res.Attr["hasOutOfScopeChange"] != "true" {
		t.Errorf("hasOutOfScopeChange = %q, want true for status=triggered", res.Attr["hasOutOfScopeChange"])
	}
}

func TestPagerdutyManageAttr_PriorityChangeFlaggedEvenWithoutStatus(t *testing.T) {
	e := compilePagerdutyTestEngine(t)
	res, err := e.Eval(CallInput{
		Backend: "vmcp",
		Tool:    "pagerduty_manage_incidents",
		Args: map[string]any{
			"manage_request": map[string]any{
				"incidents": []any{
					map[string]any{"id": "PT1", "type": "incident_reference", "priority": map[string]any{"id": "P1", "type": "priority_reference"}},
				},
			},
		},
	})
	if err != nil {
		t.Fatalf("Eval: %v", err)
	}
	if res.Attr["hasOutOfScopeChange"] != "true" {
		t.Errorf("hasOutOfScopeChange = %q, want true for a priority-only change", res.Attr["hasOutOfScopeChange"])
	}
}

func TestPagerdutyManageAttr_TitleAndEscalationAndAssignmentsFlagged(t *testing.T) {
	e := compilePagerdutyTestEngine(t)
	cases := []struct {
		name  string
		field string
		value any
	}{
		{"title", "title", "New title"},
		{"escalation_level", "escalation_level", 2},
		{"assignments", "assignments", []any{map[string]any{"assignee": map[string]any{"id": "PXPGF42", "type": "user_reference"}}}},
		{"incident_type", "incident_type", map[string]any{"name": "major_incident"}},
		{"conference_bridge", "conference_bridge", map[string]any{"conference_number": "+1-555"}},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			res, err := e.Eval(CallInput{
				Backend: "vmcp",
				Tool:    "pagerduty_manage_incidents",
				Args: map[string]any{
					"manage_request": map[string]any{
						"incidents": []any{
							map[string]any{"id": "PT1", "type": "incident_reference", "status": "acknowledged", c.field: c.value},
						},
					},
				},
			})
			if err != nil {
				t.Fatalf("Eval: %v", err)
			}
			if res.Attr["hasOutOfScopeChange"] != "true" {
				t.Errorf("hasOutOfScopeChange = %q, want true when %s is set", res.Attr["hasOutOfScopeChange"], c.field)
			}
		})
	}
}
