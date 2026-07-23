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

func TestPagerdutyManageAttr_AckOnly(t *testing.T) {
	e := compilePagerdutyTestEngine(t)
	res, err := e.Eval(CallInput{
		Backend: "vmcp",
		Tool:    "pagerduty_manage_incidents",
		Args: map[string]any{
			"manage_request": map[string]any{
				"incident_ids": []any{"PT1", "PT2"},
				"status":       "acknowledged",
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

func TestPagerdutyManageAttr_ResolveOnly(t *testing.T) {
	e := compilePagerdutyTestEngine(t)
	res, err := e.Eval(CallInput{
		Backend: "vmcp",
		Tool:    "pagerduty_manage_incidents",
		Args: map[string]any{
			"manage_request": map[string]any{
				"incident_ids": []any{"PT1"},
				"status":       "resolved",
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
	ids := make([]any, 0, 50)
	for i := 0; i < 50; i++ {
		ids = append(ids, "PT")
	}
	res, err := e.Eval(CallInput{
		Backend: "vmcp",
		Tool:    "pagerduty_manage_incidents",
		Args: map[string]any{
			"manage_request": map[string]any{
				"incident_ids": ids,
				"status":       "acknowledged",
			},
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
				"incident_ids": []any{"PT1"},
				"status":       "triggered",
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

func TestPagerdutyManageAttr_UrgencyFlaggedEvenWithAck(t *testing.T) {
	e := compilePagerdutyTestEngine(t)
	res, err := e.Eval(CallInput{
		Backend: "vmcp",
		Tool:    "pagerduty_manage_incidents",
		Args: map[string]any{
			"manage_request": map[string]any{
				"incident_ids": []any{"PT1"},
				"status":       "acknowledged",
				"urgency":      "high",
			},
		},
	})
	if err != nil {
		t.Fatalf("Eval: %v", err)
	}
	if res.Attr["hasOutOfScopeChange"] != "true" {
		t.Errorf("hasOutOfScopeChange = %q, want true when urgency is set (this was the live bug: urgency used to pass through unchecked)", res.Attr["hasOutOfScopeChange"])
	}
}

func TestPagerdutyManageAttr_EscalationLevelFlaggedEvenWithoutStatus(t *testing.T) {
	e := compilePagerdutyTestEngine(t)
	res, err := e.Eval(CallInput{
		Backend: "vmcp",
		Tool:    "pagerduty_manage_incidents",
		Args: map[string]any{
			"manage_request": map[string]any{
				"incident_ids":     []any{"PT1"},
				"escalation_level": 2,
			},
		},
	})
	if err != nil {
		t.Fatalf("Eval: %v", err)
	}
	if res.Attr["hasOutOfScopeChange"] != "true" {
		t.Errorf("hasOutOfScopeChange = %q, want true when escalation_level is set (this was the live bug: only caught by luck when PagerDuty itself rejected the call)", res.Attr["hasOutOfScopeChange"])
	}
}

func TestPagerdutyManageAttr_AssignmentSpellingsFlagged(t *testing.T) {
	// pagerduty-mcp 1.1.0's tool description now says "assignment" (correct
	// spelling) even though the wire field is still "assignement" (confirmed
	// against that version's own source, see helpers_pagerduty.go) -- a
	// caller trusting either spelling must still be caught.
	for _, key := range []string{"assignement", "assignment"} {
		t.Run(key, func(t *testing.T) {
			e := compilePagerdutyTestEngine(t)
			res, err := e.Eval(CallInput{
				Backend: "vmcp",
				Tool:    "pagerduty_manage_incidents",
				Args: map[string]any{
					"manage_request": map[string]any{
						"incident_ids": []any{"PT1"},
						"status":       "acknowledged",
						key:            map[string]any{"id": "PXPGF42", "type": "user_reference"},
					},
				},
			})
			if err != nil {
				t.Fatalf("Eval: %v", err)
			}
			if res.Attr["hasOutOfScopeChange"] != "true" {
				t.Errorf("hasOutOfScopeChange = %q, want true when %q is set", res.Attr["hasOutOfScopeChange"], key)
			}
		})
	}
}

func TestPagerdutyManageAttr_UnknownFieldFlagged(t *testing.T) {
	// The check is an allowlist of {incident_ids, status}, not a denylist of
	// urgency/escalation_level/assignement/assignment -- a field this helper
	// has never heard of (a future pagerduty-mcp addition, or PagerDuty's own
	// REST-API-shaped "priority"/"resolution", which this MCP tool doesn't
	// actually carry but a denylist approach would have had to name by hand
	// to catch) must still be flagged just by not being incident_ids/status.
	for _, key := range []string{"priority", "resolution", "some_future_field"} {
		t.Run(key, func(t *testing.T) {
			e := compilePagerdutyTestEngine(t)
			res, err := e.Eval(CallInput{
				Backend: "vmcp",
				Tool:    "pagerduty_manage_incidents",
				Args: map[string]any{
					"manage_request": map[string]any{
						"incident_ids": []any{"PT1"},
						"status":       "acknowledged",
						key:            "something",
					},
				},
			})
			if err != nil {
				t.Fatalf("Eval: %v", err)
			}
			if res.Attr["hasOutOfScopeChange"] != "true" {
				t.Errorf("hasOutOfScopeChange = %q, want true when unrecognized field %q is set", res.Attr["hasOutOfScopeChange"], key)
			}
		})
	}
}

func TestPagerdutyManageAttr_ZeroValuedFieldsDoNotFalsePositive(t *testing.T) {
	// escalation_level:0, urgency:"", assignement:{} are "not really set" --
	// a caller that always includes these keys with zero values shouldn't be
	// denied just for the keys' presence.
	e := compilePagerdutyTestEngine(t)
	res, err := e.Eval(CallInput{
		Backend: "vmcp",
		Tool:    "pagerduty_manage_incidents",
		Args: map[string]any{
			"manage_request": map[string]any{
				"incident_ids":     []any{"PT1"},
				"status":           "acknowledged",
				"urgency":          "",
				"escalation_level": 0,
				"assignement":      map[string]any{},
			},
		},
	})
	if err != nil {
		t.Fatalf("Eval: %v", err)
	}
	if res.Attr["hasOutOfScopeChange"] != "false" {
		t.Errorf("hasOutOfScopeChange = %q, want false for zero-valued optional fields", res.Attr["hasOutOfScopeChange"])
	}
}
