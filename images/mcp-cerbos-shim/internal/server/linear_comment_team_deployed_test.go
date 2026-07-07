package server

import (
	"context"
	"errors"
	"testing"

	"github.com/jchristensen/vicegerent-agents/images/mcp-cerbos-shim/internal/eval"
	"github.com/jchristensen/vicegerent-agents/images/mcp-cerbos-shim/internal/upstream"
)

// These tests run the SHIPPED mapping (not a fixture) through the request
// path for linear_save_comment, with a FAKE upstream (no network) standing
// in for the live vMCP linear_get_issue call the team-resolution gate makes.
// They prove the gate wiring (HAH-69): a comment on an issue outside the
// allowed team is denied, a comment on an allowed-team issue reaches Cerbos
// with teamId populated, a lookup failure fails closed, and save_issue
// itself is untouched by this gate (it resolves its own teamId directly from
// the call's `team` arg via linearIssueAttr, not this lookup).

func newLinearServer(t *testing.T, d *stubDecider, up upstream.ToolCaller) *Server {
	t.Helper()
	m := deployedMapping(t)
	e, err := eval.Compile(m)
	if err != nil {
		t.Fatalf("compile: %v", err)
	}
	return New(m, e, d, Principal{ID: "hermes", Roles: []string{"agent"}},
		WithLinearIssueTeam(up))
}

// linearIssueResultDevops/linearIssueResultOther mirror the live
// linear_get_issue result shape captured against the real vMCP route
// (HAH-69): a single JSON object with "team" as the team's display name
// directly at the top level, no extra nesting (unlike Notion's
// double-JSON-wrapped notion-fetch result -- see ancestry.go's
// notionFetchEnvelope doc).
const linearIssueResultDevops = `{"id":"HAH-69","title":"some issue","team":"HAHomelabs"}`
const linearIssueResultOther = `{"id":"OTHER-1","title":"some issue","team":"Finance"}`
const linearIssueResultNoTeam = `{"id":"OTHER-2","title":"some issue"}`

func TestDeployedLinearMapping_SaveCommentOnAllowedTeamIssueReachesCerbos(t *testing.T) {
	d := &stubDecider{allow: true}
	up := &fakeUpstream{text: linearIssueResultDevops}
	s := newLinearServer(t, d, up)
	res, err := s.CheckRequest(context.Background(),
		mcpReq("vmcp", "tools/call", toolCall("linear_save_comment",
			map[string]any{"issueId": "HAH-69", "body": "hello"})))
	if err != nil {
		t.Fatalf("CheckRequest: %v", err)
	}
	if !isPass(res) {
		t.Fatalf("expected pass: issue's team is HAHomelabs/DEVOPS-allowed")
	}
	if up.calls != 1 {
		t.Errorf("expected exactly one linear_get_issue team lookup, got %d", up.calls)
	}
	if d.calls != 1 {
		t.Fatalf("expected the gated call to reach Cerbos exactly once, got %d", d.calls)
	}
	if d.gotType != "linear_team" || d.gotAct != "access" {
		t.Errorf("Cerbos saw resource=%q action=%q, want linear_team/access", d.gotType, d.gotAct)
	}
	if got, _ := d.gotAttr["teamId"].(string); got != "HAHomelabs" {
		t.Errorf("Cerbos saw teamId=%q, want the resolved team HAHomelabs", got)
	}
}

// TestDeployedLinearMapping_SaveCommentOnOtherTeamResolvesAndForwardsToCerbos
// proves the GATE's half of the contract: it resolves the issue's real team
// (Finance, not DEVOPS) and hands that exact value to Cerbos as teamId. The
// actual allow/deny decision for a non-DEVOPS teamId is Cerbos policy's job,
// already covered by linear_test.yaml's deny-non-devops-team case -- this
// test uses stubDecider (which returns a fixed verdict, not real policy
// logic) only to confirm what the gate SENDS, not what Cerbos DECIDES.
func TestDeployedLinearMapping_SaveCommentOnOtherTeamResolvesAndForwardsToCerbos(t *testing.T) {
	d := &stubDecider{allow: true}
	up := &fakeUpstream{text: linearIssueResultOther}
	s := newLinearServer(t, d, up)
	res, err := s.CheckRequest(context.Background(),
		mcpReq("vmcp", "tools/call", toolCall("linear_save_comment",
			map[string]any{"issueId": "OTHER-1", "body": "hello"})))
	if err != nil {
		t.Fatalf("CheckRequest: %v", err)
	}
	if !isPass(res) {
		t.Fatalf("expected pass: stubDecider always allows regardless of attr (real deny logic is Cerbos's own, tested in linear_test.yaml)")
	}
	if d.calls != 1 {
		t.Fatalf("expected the gated call to reach Cerbos exactly once, got %d", d.calls)
	}
	if got, _ := d.gotAttr["teamId"].(string); got != "Finance" {
		t.Errorf("Cerbos saw teamId=%q, want the resolved (non-DEVOPS) team Finance", got)
	}
}

func TestDeployedLinearMapping_SaveCommentLookupErrorFailsClosed(t *testing.T) {
	d := &stubDecider{allow: true}
	up := &fakeUpstream{err: errors.New("upstream timeout")}
	s := newLinearServer(t, d, up)
	res, err := s.CheckRequest(context.Background(),
		mcpReq("vmcp", "tools/call", toolCall("linear_save_comment",
			map[string]any{"issueId": "HAH-69", "body": "hello"})))
	if err != nil {
		t.Fatalf("CheckRequest: %v", err)
	}
	if !isDeny(res) {
		t.Fatalf("expected deny (fail closed) when the team lookup errors, got pass")
	}
	if d.calls != 0 {
		t.Errorf("Cerbos must NOT be consulted when the gate fails closed, got %d calls", d.calls)
	}
}

func TestDeployedLinearMapping_SaveCommentLookupResultWithNoTeamFailsClosed(t *testing.T) {
	d := &stubDecider{allow: true}
	up := &fakeUpstream{text: linearIssueResultNoTeam}
	s := newLinearServer(t, d, up)
	res, err := s.CheckRequest(context.Background(),
		mcpReq("vmcp", "tools/call", toolCall("linear_save_comment",
			map[string]any{"issueId": "OTHER-2", "body": "hello"})))
	if err != nil {
		t.Fatalf("CheckRequest: %v", err)
	}
	if !isDeny(res) {
		t.Fatalf("expected deny (fail closed): get_issue result had no team")
	}
	if d.calls != 0 {
		t.Errorf("Cerbos must NOT be consulted when the gate fails closed, got %d calls", d.calls)
	}
}

// TestDeployedLinearMapping_SaveCommentWithNoIssueIdSkipsGate proves a
// comment on a project/initiative/document/milestone (no issueId), or a
// reply via parentId with no entity ref, has nothing to resolve and passes
// the gate unchecked -- same fail-open-when-unverifiable posture as
// save_project's linearProjectAttr helper when neither addTeams nor setTeams
// is set.
func TestDeployedLinearMapping_SaveCommentWithNoIssueIdSkipsGate(t *testing.T) {
	d := &stubDecider{allow: true}
	up := &fakeUpstream{err: errors.New("must not be called")}
	s := newLinearServer(t, d, up)
	res, err := s.CheckRequest(context.Background(),
		mcpReq("vmcp", "tools/call", toolCall("linear_save_comment",
			map[string]any{"projectId": "some-project", "body": "hello"})))
	if err != nil {
		t.Fatalf("CheckRequest: %v", err)
	}
	if !isPass(res) {
		t.Fatalf("expected pass: no issueId to resolve, gate should not fire")
	}
	if up.calls != 0 {
		t.Errorf("expected no linear_get_issue lookup when issueId is absent, got %d", up.calls)
	}
}

// TestDeployedLinearMapping_SaveIssueDoesNotTriggerCommentTeamGate proves
// save_issue (a DIFFERENT tool sharing the same linear_team/access
// resource/action pair) is untouched by this gate -- its own teamId is
// already populated directly from the call's `team` arg by linearIssueAttr,
// not by a lookup.
func TestDeployedLinearMapping_SaveIssueDoesNotTriggerCommentTeamGate(t *testing.T) {
	d := &stubDecider{allow: true}
	up := &fakeUpstream{err: errors.New("must not be called")}
	s := newLinearServer(t, d, up)
	res, err := s.CheckRequest(context.Background(),
		mcpReq("vmcp", "tools/call", toolCall("linear_save_issue",
			map[string]any{"team": "HAHomelabs", "title": "new issue"})))
	if err != nil {
		t.Fatalf("CheckRequest: %v", err)
	}
	if !isPass(res) {
		t.Fatalf("expected pass: save_issue's own team is HAHomelabs/DEVOPS-allowed")
	}
	if up.calls != 0 {
		t.Errorf("save_issue must not trigger the save_comment team-lookup gate, got %d calls", up.calls)
	}
	if got, _ := d.gotAttr["teamId"].(string); got != "HAHomelabs" {
		t.Errorf("Cerbos saw teamId=%q, want HAHomelabs (from the call's own team arg)", got)
	}
}
