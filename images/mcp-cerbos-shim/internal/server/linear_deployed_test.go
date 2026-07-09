package server

import (
	"context"
	"errors"
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

// An ordinary save_issue UPDATE (no `team` arg) now resolves the
// issue's CURRENT team via a live lookup instead of omitting teamId
// entirely -- this closes the gap where such a call previously fell through
// to allow-all regardless of the issue's real team. This test proves the
// gate wiring with a fake upstream (no network); the deny/allow DECISION for
// the resolved team is Cerbos's own job, proven by defs/linear_test.yaml.
func TestDeployedLinearMapping_OrdinaryUpdateResolvesCurrentTeam(t *testing.T) {
	m := deployedMapping(t)
	e, err := eval.Compile(m)
	if err != nil {
		t.Fatalf("compile: %v", err)
	}
	d := &stubDecider{allow: true}
	up := &fakeUpstream{text: linearIssueResultDevops}
	s := New(m, e, d, Principal{ID: "hermes", Roles: []string{"agent"}}, WithLinearIssueTeam(up))
	res, err := s.CheckRequest(context.Background(),
		mcpReq("vmcp", "tools/call", toolCall("linear_save_issue",
			map[string]any{"id": "PROJ-1", "title": "t"})))
	if err != nil {
		t.Fatalf("CheckRequest: %v", err)
	}
	if !isPass(res) {
		t.Fatalf("expected pass: resolved team is HAHomelabs/DEVOPS-allowed")
	}
	if up.calls != 1 {
		t.Errorf("expected exactly one linear_get_issue team lookup, got %d", up.calls)
	}
	if d.calls != 1 {
		t.Fatalf("expected exactly one Cerbos check, got %d", d.calls)
	}
	if got, _ := d.gotAttr["teamId"].(string); got != "HAHomelabs" {
		t.Errorf("attr.teamId = %q, want the resolved team HAHomelabs", got)
	}
}

// TestDeployedLinearMapping_OrdinaryUpdateGateUnconfiguredFailsClosed proves
// the fail-closed contract when the lookup gate isn't wired at all
// (a broken deploy, not a license to allow an unverified team through).
func TestDeployedLinearMapping_OrdinaryUpdateGateUnconfiguredFailsClosed(t *testing.T) {
	m := deployedMapping(t)
	e, err := eval.Compile(m)
	if err != nil {
		t.Fatalf("compile: %v", err)
	}
	d := &stubDecider{allow: true}
	s := New(m, e, d, Principal{ID: "hermes", Roles: []string{"agent"}}) // no WithLinearIssueTeam
	res, err := s.CheckRequest(context.Background(),
		mcpReq("vmcp", "tools/call", toolCall("linear_save_issue",
			map[string]any{"id": "issue-1", "title": "t"})))
	if err != nil {
		t.Fatalf("CheckRequest: %v", err)
	}
	if !isDeny(res) {
		t.Fatalf("expected deny: lookup gate unconfigured must fail closed")
	}
	if d.calls != 0 {
		t.Errorf("expected Cerbos never consulted on a fail-closed gate error, got %d calls", d.calls)
	}
}

// TestDeployedLinearMapping_OrdinaryUpdateLookupErrorFailsClosed proves a
// live lookup failure (timeout, malformed result, etc.) also fails closed.
func TestDeployedLinearMapping_OrdinaryUpdateLookupErrorFailsClosed(t *testing.T) {
	m := deployedMapping(t)
	e, err := eval.Compile(m)
	if err != nil {
		t.Fatalf("compile: %v", err)
	}
	d := &stubDecider{allow: true}
	up := &fakeUpstream{err: errors.New("upstream timeout")}
	s := New(m, e, d, Principal{ID: "hermes", Roles: []string{"agent"}}, WithLinearIssueTeam(up))
	res, err := s.CheckRequest(context.Background(),
		mcpReq("vmcp", "tools/call", toolCall("linear_save_issue",
			map[string]any{"id": "issue-1", "title": "t"})))
	if err != nil {
		t.Fatalf("CheckRequest: %v", err)
	}
	if !isDeny(res) {
		t.Fatalf("expected deny: lookup failure must fail closed")
	}
	if d.calls != 0 {
		t.Errorf("expected Cerbos never consulted on a fail-closed lookup error, got %d calls", d.calls)
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

// linear_save_comment is now mapped and team-gated (see
// linear_comment_team_deployed_test.go for the full gate matrix: allow on a
// DEVOPS-team issue, deny on any other team, fail-closed on a lookup error,
// and pass-through for a comment with no issueId to resolve). This test only
// guards the previously-true-but-now-outdated "unmapped, no lookup wired"
// shape: without WithLinearIssueTeam configured (as production's main.go
// always wires -- see WARNING-log posture mirrored from the Notion gate),
// the gate is mandatory and fails closed rather than silently passing an
// unverified team through.
func TestDeployedLinearMapping_SaveCommentWithoutGateConfiguredFailsClosed(t *testing.T) {
	m := deployedMapping(t)
	e, err := eval.Compile(m)
	if err != nil {
		t.Fatalf("compile: %v", err)
	}
	d := &stubDecider{allow: false}
	s := New(m, e, d, Principal{ID: "hermes", Roles: []string{"agent"}}) // no WithLinearIssueTeam
	res, err := s.CheckRequest(context.Background(),
		mcpReq("vmcp", "tools/call", toolCall("linear_save_comment", map[string]any{"issueId": "issue-1"})))
	if err != nil {
		t.Fatalf("CheckRequest: %v", err)
	}
	if !isDeny(res) {
		t.Fatalf("expected deny (fail closed): gate not configured, cannot verify team")
	}
	if d.calls != 0 {
		t.Errorf("must not reach Cerbos when the gate itself is unconfigured, got %d calls", d.calls)
	}
}

// linear_save_project IS mapped (linearProjectAttr helper) — unlike
// save_comment above, every call reaches Cerbos; what varies is whether a
// `teams` attr is present. A call that sets neither addTeams nor setTeams has
// nothing to verify directly from the call args -- a lookup gate now resolves the
// project's CURRENT team(s) via a live lookup instead (defs/linear_test.yaml
// proves the allow/deny decision itself once teams is populated).
func TestDeployedLinearMapping_SaveProjectUpdateResolvesCurrentTeams(t *testing.T) {
	m := deployedMapping(t)
	e, err := eval.Compile(m)
	if err != nil {
		t.Fatalf("compile: %v", err)
	}
	d := &stubDecider{allow: true}
	up := &fakeUpstream{text: `{"teams":[{"id":"6deab0c5-9bda-4f82-b552-41f4aa9e449b","name":"DevOps","key":"DEVOPS"}]}`}
	s := New(m, e, d, Principal{ID: "hermes", Roles: []string{"agent"}}, WithLinearProjectTeam(up))
	res, err := s.CheckRequest(context.Background(),
		mcpReq("vmcp", "tools/call", toolCall("linear_save_project", map[string]any{"id": "proj-1"})))
	if err != nil {
		t.Fatalf("CheckRequest: %v", err)
	}
	if !isPass(res) {
		t.Fatalf("expected pass: resolved team is DevOps/allowed")
	}
	if up.calls != 1 {
		t.Errorf("expected exactly one linear_get_project team lookup, got %d", up.calls)
	}
	if d.calls != 1 {
		t.Fatalf("expected exactly one Cerbos check, got %d", d.calls)
	}
	if d.gotType != "linear_team" {
		t.Errorf("resourceType = %q, want linear_team", d.gotType)
	}
	teams, _ := d.gotAttr["teams"].([]string)
	if len(teams) != 1 || teams[0] != "DevOps" {
		t.Errorf("attr.teams = %v, want [DevOps]", d.gotAttr["teams"])
	}
}

// TestDeployedLinearMapping_SaveProjectUpdateGateUnconfiguredFailsClosed
// proves the fail-closed contract when the project lookup gate isn't
// wired at all.
func TestDeployedLinearMapping_SaveProjectUpdateGateUnconfiguredFailsClosed(t *testing.T) {
	m := deployedMapping(t)
	e, err := eval.Compile(m)
	if err != nil {
		t.Fatalf("compile: %v", err)
	}
	d := &stubDecider{allow: true}
	s := New(m, e, d, Principal{ID: "hermes", Roles: []string{"agent"}}) // no WithLinearProjectTeam
	res, err := s.CheckRequest(context.Background(),
		mcpReq("vmcp", "tools/call", toolCall("linear_save_project", map[string]any{"id": "proj-1"})))
	if err != nil {
		t.Fatalf("CheckRequest: %v", err)
	}
	if !isDeny(res) {
		t.Fatalf("expected deny: lookup gate unconfigured must fail closed")
	}
	if d.calls != 0 {
		t.Errorf("expected Cerbos never consulted on a fail-closed gate error, got %d calls", d.calls)
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

// linear_create_issue_label is mapped to linear_team/access, same
// resource as save_issue/save_comment/save_project, but needs no lookup:
// the caller's own teamId arg is already the directly-verifiable signal
// (linearLabelAttr). A workspace-scoped label (no teamId) must reach Cerbos
// with no teamId attr at all, so it falls through to allow-all rather than
// tripping the has()-based deny-non-devops-team rule.
func TestDeployedLinearMapping_CreateTeamScopedLabelReachesCerbos(t *testing.T) {
	m := deployedMapping(t)
	e, err := eval.Compile(m)
	if err != nil {
		t.Fatalf("compile: %v", err)
	}
	d := &stubDecider{allow: false}
	s := New(m, e, d, Principal{ID: "hermes", Roles: []string{"agent"}})
	res, err := s.CheckRequest(context.Background(),
		mcpReq("vmcp", "tools/call", toolCall("linear_create_issue_label",
			map[string]any{"name": "bug", "teamId": "some-other-team-id"})))
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
	if got, _ := d.gotAttr["teamId"].(string); got != "some-other-team-id" {
		t.Errorf("attr.teamId = %q, want some-other-team-id", got)
	}
}

func TestDeployedLinearMapping_CreateWorkspaceLabelOmitsTeamId(t *testing.T) {
	m := deployedMapping(t)
	e, err := eval.Compile(m)
	if err != nil {
		t.Fatalf("compile: %v", err)
	}
	d := &stubDecider{allow: true}
	s := New(m, e, d, Principal{ID: "hermes", Roles: []string{"agent"}})
	res, err := s.CheckRequest(context.Background(),
		mcpReq("vmcp", "tools/call", toolCall("linear_create_issue_label", map[string]any{"name": "bug"})))
	if err != nil {
		t.Fatalf("CheckRequest: %v", err)
	}
	if !isPass(res) {
		t.Fatalf("expected pass: workspace-scoped label has nothing to verify")
	}
	if d.calls != 1 {
		t.Fatalf("expected exactly one Cerbos check, got %d", d.calls)
	}
	if _, ok := d.gotAttr["teamId"]; ok {
		t.Errorf("attr.teamId = %q, want key absent for a workspace-scoped label", d.gotAttr["teamId"])
	}
}
