package server

import (
	"context"
	"testing"

	"github.com/jchristensen/vicegerent-agents/images/mcp-cerbos-shim/internal/eval"
)

// These tests run the SHIPPED mapping (not a fixture) through the request path.
// linear_save_issue is the live vMCP tool for both create and update; only
// `team` is required on create, so a create call is checked against the
// caller's `team` arg (name/key/uuid, verifiable). An update call (an `id`
// arg names an existing issue) carries no `team` of its own — the shim can't
// verify the existing issue's team without a live lookup — so it's mapped
// straight to the DEVOPS id and always passes Cerbos. The deny *decision*
// for the DEVOPS-only boundary is proven by defs/linear_test.yaml.

func TestDeployedLinearMapping_CreateIssueReachesCerbos(t *testing.T) {
	m := deployedMapping(t)
	e, err := eval.Compile(m)
	if err != nil {
		t.Fatalf("compile: %v", err)
	}
	d := &stubDecider{allow: false}
	s := New(m, e, d, Principal{ID: "hermes", Roles: []string{"agent"}})
	res, err := s.CheckRequest(context.Background(),
		mcpReq("vmcp", "tools/call", toolCall("linear_save_issue",
			map[string]any{"team": "some-other-team-id", "title": "t"})))
	if err != nil {
		t.Fatalf("CheckRequest: %v", err)
	}
	if !isDeny(res) {
		t.Fatalf("expected deny when Cerbos denies, got pass")
	}
	assertNoSideEffects(t, res)
	if d.calls != 1 {
		t.Fatalf("expected exactly one Cerbos check, got %d", d.calls)
	}
	if d.gotType != "linear_team" {
		t.Errorf("resourceType = %q, want linear_team", d.gotType)
	}
	if d.gotAttr["teamId"] != "some-other-team-id" {
		t.Errorf("attr.teamId = %q, want some-other-team-id", d.gotAttr["teamId"])
	}
}

func TestDeployedLinearMapping_UpdateIssueReachesCerbosAsDevops(t *testing.T) {
	m := deployedMapping(t)
	e, err := eval.Compile(m)
	if err != nil {
		t.Fatalf("compile: %v", err)
	}
	d := &stubDecider{allow: false}
	s := New(m, e, d, Principal{ID: "hermes", Roles: []string{"agent"}})
	res, err := s.CheckRequest(context.Background(),
		mcpReq("vmcp", "tools/call", toolCall("linear_save_issue",
			map[string]any{"id": "issue-1", "title": "t"})))
	if err != nil {
		t.Fatalf("CheckRequest: %v", err)
	}
	// stubDecider always denies here regardless of attrs, so this proves the
	// update path still reaches Cerbos (mapped, not unmapped-pass) and is
	// checked against the DEVOPS id rather than an empty/unverifiable teamId.
	if !isDeny(res) {
		t.Fatalf("expected deny when Cerbos denies, got pass")
	}
	if d.calls != 1 {
		t.Fatalf("expected exactly one Cerbos check, got %d", d.calls)
	}
	if d.gotType != "linear_team" {
		t.Errorf("resourceType = %q, want linear_team", d.gotType)
	}
	if d.gotAttr["teamId"] != "6deab0c5-9bda-4f82-b552-41f4aa9e449b" {
		t.Errorf("attr.teamId = %q, want the DEVOPS team uuid", d.gotAttr["teamId"])
	}
}
