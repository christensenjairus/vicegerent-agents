package server

import (
	"context"
	"testing"

	"github.com/jchristensen/vicegerent-agents/images/mcp-cerbos-shim/internal/eval"
)

// These tests run the SHIPPED mapping (not a fixture) through the request path.
// They prove the wiring that turns an Alertmanager createSilence/deleteSilence
// call into the alertmanager_silence resource Cerbos caps/denies; the deny
// *decision* itself is proven by defs/alertmanager_test.yaml.

func TestDeployedAlertmanagerMapping_CreateSilenceReachesCerbosWithDurationSeconds(t *testing.T) {
	m := deployedMapping(t)
	e, err := eval.Compile(m)
	if err != nil {
		t.Fatalf("compile: %v", err)
	}
	d := &stubDecider{allow: true}
	s := New(m, e, d, Principal{ID: "hermes", Roles: []string{"agent"}})
	res, err := s.CheckRequest(context.Background(),
		mcpReq("vmcp", "tools/call", toolCall("alertmanager_createSilence",
			map[string]any{"alertName": "HighMemoryUsage", "duration": "2h"})))
	if err != nil {
		t.Fatalf("CheckRequest: %v", err)
	}
	if !isPass(res) {
		t.Fatalf("expected pass when Cerbos allows, got deny")
	}
	if d.calls != 1 {
		t.Fatalf("expected exactly one Cerbos check, got %d", d.calls)
	}
	if d.gotType != "alertmanager_silence" {
		t.Errorf("resourceType = %q, want alertmanager_silence", d.gotType)
	}
	if d.gotAttr["durationSeconds"] != "7200" {
		t.Errorf("attr.durationSeconds = %q, want 7200 (2h)", d.gotAttr["durationSeconds"])
	}
}

func TestDeployedAlertmanagerMapping_CreateSilenceOmittedDurationDefaultsToTwoHours(t *testing.T) {
	m := deployedMapping(t)
	e, err := eval.Compile(m)
	if err != nil {
		t.Fatalf("compile: %v", err)
	}
	d := &stubDecider{allow: true}
	s := New(m, e, d, Principal{ID: "hermes", Roles: []string{"agent"}})
	res, err := s.CheckRequest(context.Background(),
		mcpReq("vmcp", "tools/call", toolCall("alertmanager_createSilence",
			map[string]any{"alertName": "HighMemoryUsage"})))
	if err != nil {
		t.Fatalf("CheckRequest: %v", err)
	}
	if !isPass(res) {
		t.Fatalf("expected pass when Cerbos allows, got deny")
	}
	if d.gotAttr["durationSeconds"] != "7200" {
		t.Errorf("attr.durationSeconds = %q, want 7200 (default 2h when duration omitted)", d.gotAttr["durationSeconds"])
	}
}

func TestDeployedAlertmanagerMapping_DeleteSilenceReachesCerbos(t *testing.T) {
	m := deployedMapping(t)
	e, err := eval.Compile(m)
	if err != nil {
		t.Fatalf("compile: %v", err)
	}
	d := &stubDecider{allow: false}
	s := New(m, e, d, Principal{ID: "hermes", Roles: []string{"agent"}})
	res, err := s.CheckRequest(context.Background(),
		mcpReq("vmcp", "tools/call", toolCall("alertmanager_deleteSilence",
			map[string]any{"silenceId": "6f9d3a2e-1234-4567-8901-abcdef012345"})))
	if err != nil {
		t.Fatalf("CheckRequest: %v", err)
	}
	if !isDeny(res) {
		t.Fatalf("expected deny when Cerbos denies, got pass")
	}
	if d.calls != 1 {
		t.Fatalf("expected exactly one Cerbos check, got %d", d.calls)
	}
	if d.gotType != "alertmanager_silence" {
		t.Errorf("resourceType = %q, want alertmanager_silence", d.gotType)
	}
	if d.gotAttr["silenceId"] != "6f9d3a2e-1234-4567-8901-abcdef012345" {
		t.Errorf("attr.silenceId = %q, want the supplied silenceId", d.gotAttr["silenceId"])
	}
}
