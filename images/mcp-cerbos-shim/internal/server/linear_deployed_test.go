package server

import (
	"context"
	"testing"

	"github.com/jchristensen/vicegerent-agents/images/mcp-cerbos-shim/internal/eval"
)

// These tests run the SHIPPED mapping (not a fixture) through the request path.
// linear_save_issue is always mapped (unlike linear_save_comment/
// linear_save_project below, or the old create_issue/update_issue split),
// so every call reaches Cerbos — what varies is whether a teamId attr is
// present at all. `team` is required on create, so a create call always
// carries a verifiable teamId. An update call (an `id` arg names an existing
// issue) carries no `team` of its own unless the caller is deliberately
// reassigning it — an ordinary update omits `team`, and the linearIssueAttr
// helper (helpers_linear.go) omits the teamId attr entirely so Cerbos's
// has()-based check falls through to allow-all instead of tripping on an
// empty value; an update that DOES set `team` is checked exactly like a
// create. The deny *decision* for the DEVOPS-only boundary is proven by
// defs/linear_test.yaml.

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

func TestDeployedLinearMapping_OrdinaryUpdateOmitsTeamId(t *testing.T) {
	m := deployedMapping(t)
	e, err := eval.Compile(m)
	if err != nil {
		t.Fatalf("compile: %v", err)
	}
	// allow: true here (unlike the other tests) because the point of this
	// test is what attrs Cerbos SEES, not what it decides — linear_save_issue
	// is always mapped, so an ordinary update still reaches Cerbos; it must
	// just arrive with no teamId key, which is what lets the real Cerbos
	// policy's has()-based check fall through to allow-all (defs/linear_test.yaml).
	d := &stubDecider{allow: true}
	s := New(m, e, d, Principal{ID: "hermes", Roles: []string{"agent"}})
	res, err := s.CheckRequest(context.Background(),
		mcpReq("vmcp", "tools/call", toolCall("linear_save_issue",
			map[string]any{"id": "issue-1", "title": "t"})))
	if err != nil {
		t.Fatalf("CheckRequest: %v", err)
	}
	if !isPass(res) {
		t.Fatalf("expected pass when Cerbos allows, got deny")
	}
	if d.calls != 1 {
		t.Fatalf("expected exactly one Cerbos check, got %d", d.calls)
	}
	if _, ok := d.gotAttr["teamId"]; ok {
		t.Errorf("attr.teamId = %q, want key absent for an ordinary update", d.gotAttr["teamId"])
	}
}

func TestDeployedLinearMapping_UpdateReassigningTeamReachesCerbos(t *testing.T) {
	m := deployedMapping(t)
	e, err := eval.Compile(m)
	if err != nil {
		t.Fatalf("compile: %v", err)
	}
	d := &stubDecider{allow: false}
	s := New(m, e, d, Principal{ID: "hermes", Roles: []string{"agent"}})
	res, err := s.CheckRequest(context.Background(),
		mcpReq("vmcp", "tools/call", toolCall("linear_save_issue",
			map[string]any{"id": "issue-1", "team": "some-other-team-id"})))
	if err != nil {
		t.Fatalf("CheckRequest: %v", err)
	}
	// An update that deliberately sets `team` must be checked against that
	// actual value, not silently allowed just because it's an update.
	if !isDeny(res) {
		t.Fatalf("expected deny when Cerbos denies, got pass")
	}
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

func TestDeployedLinearMapping_SaveCommentIsUnmapped(t *testing.T) {
	m := deployedMapping(t)
	e, err := eval.Compile(m)
	if err != nil {
		t.Fatalf("compile: %v", err)
	}
	d := &stubDecider{allow: false}
	s := New(m, e, d, Principal{ID: "hermes", Roles: []string{"agent"}})
	res, err := s.CheckRequest(context.Background(),
		mcpReq("vmcp", "tools/call", toolCall("linear_save_comment", map[string]any{"id": "issue-1"})))
	if err != nil {
		t.Fatalf("CheckRequest: %v", err)
	}
	if !isPass(res) {
		t.Fatalf("expected pass for unmapped tool (no verifiable team)")
	}
	if d.calls != 0 {
		t.Errorf("unmapped tool must not call Cerbos, got %d calls", d.calls)
	}
}

// linear_save_project IS mapped (HAH-70, linearProjectAttr helper) — unlike
// save_comment above, every call reaches Cerbos; what varies is whether a
// `teams` attr is present. A call that sets neither addTeams nor setTeams has
// nothing to verify and Cerbos's has()-based check falls through to
// allow-all (defs/linear_test.yaml proves the deny decision itself).
func TestDeployedLinearMapping_SaveProjectReachesCerbos(t *testing.T) {
	m := deployedMapping(t)
	e, err := eval.Compile(m)
	if err != nil {
		t.Fatalf("compile: %v", err)
	}
	d := &stubDecider{allow: true}
	s := New(m, e, d, Principal{ID: "hermes", Roles: []string{"agent"}})
	res, err := s.CheckRequest(context.Background(),
		mcpReq("vmcp", "tools/call", toolCall("linear_save_project", map[string]any{"id": "proj-1"})))
	if err != nil {
		t.Fatalf("CheckRequest: %v", err)
	}
	if !isPass(res) {
		t.Fatalf("expected pass when Cerbos allows, got deny")
	}
	if d.calls != 1 {
		t.Fatalf("expected exactly one Cerbos check, got %d", d.calls)
	}
	if d.gotType != "linear_team" {
		t.Errorf("resourceType = %q, want linear_team", d.gotType)
	}
	if _, ok := d.gotAttr["teams"]; ok {
		t.Errorf("attr.teams = %v, want key absent when neither addTeams nor setTeams is set", d.gotAttr["teams"])
	}
}

func TestDeployedLinearMapping_SaveProjectDeniesNonAllowedTeam(t *testing.T) {
	m := deployedMapping(t)
	e, err := eval.Compile(m)
	if err != nil {
		t.Fatalf("compile: %v", err)
	}
	d := &stubDecider{allow: false}
	s := New(m, e, d, Principal{ID: "hermes", Roles: []string{"agent"}})
	res, err := s.CheckRequest(context.Background(),
		mcpReq("vmcp", "tools/call", toolCall("linear_save_project",
			map[string]any{"addTeams": []any{"some-other-team-id"}})))
	if err != nil {
		t.Fatalf("CheckRequest: %v", err)
	}
	if !isDeny(res) {
		t.Fatalf("expected deny when Cerbos denies, got pass")
	}
	if d.calls != 1 {
		t.Fatalf("expected exactly one Cerbos check, got %d", d.calls)
	}
	if d.gotType != "linear_team" {
		t.Errorf("resourceType = %q, want linear_team", d.gotType)
	}
	got, ok := d.gotAttr["teams"].([]string)
	if !ok || len(got) != 1 || got[0] != "some-other-team-id" {
		t.Errorf("attr.teams = %v, want [some-other-team-id]", d.gotAttr["teams"])
	}
}
