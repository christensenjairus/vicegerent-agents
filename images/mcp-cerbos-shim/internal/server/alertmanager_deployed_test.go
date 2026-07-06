package server

import (
	"context"
	"testing"

	"github.com/jchristensen/vicegerent-agents/images/mcp-cerbos-shim/internal/eval"
)

// These tests run the SHIPPED mapping (not a fixture) through the request path.
// They prove the wiring that turns an Alertmanager createSilence call into
// the alertmanager_silence resource Cerbos caps/denies; the deny *decision*
// itself is proven by defs/alertmanager_test.yaml. deleteSilence is
// intentionally unmapped (see resource_alertmanager.yaml) so it never
// reaches Cerbos at all -- there is no deployed test for it here because
// there is nothing to check on the Cerbos path; it passes through the
// shim's default allow like any other unmapped tool.

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

func TestDeployedAlertmanagerMapping_DeleteSilenceIsUnmappedAndPasses(t *testing.T) {
	// deleteSilence has no entry in the shipped mapping at all, so it must
	// never reach Cerbos (d.calls stays 0) and passes via the shim's default
	// allow, same as any other unmapped read-only Alertmanager tool.
	m := deployedMapping(t)
	e, err := eval.Compile(m)
	if err != nil {
		t.Fatalf("compile: %v", err)
	}
	d := &stubDecider{allow: false} // if this were consulted, it would deny -- proving the "never reaches Cerbos" claim
	s := New(m, e, d, Principal{ID: "hermes", Roles: []string{"agent"}})
	res, err := s.CheckRequest(context.Background(),
		mcpReq("vmcp", "tools/call", toolCall("alertmanager_deleteSilence",
			map[string]any{"silenceId": "6f9d3a2e-1234-4567-8901-abcdef012345"})))
	if err != nil {
		t.Fatalf("CheckRequest: %v", err)
	}
	if !isPass(res) {
		t.Fatalf("expected pass (unmapped tool falls through to default allow), got deny")
	}
	if d.calls != 0 {
		t.Fatalf("expected zero Cerbos checks for an unmapped tool, got %d", d.calls)
	}
}
