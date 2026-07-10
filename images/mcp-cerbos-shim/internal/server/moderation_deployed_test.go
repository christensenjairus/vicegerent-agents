package server

import (
	"context"
	"strings"
	"testing"

	"github.com/jchristensen/vicegerent-agents/images/mcp-cerbos-shim/internal/eval"
)

// These tests run the SHIPPED mapping (not a fixture) through the request
// path with a stub moderation.Checker standing in for the live OpenAI
// Moderations call, proving the gate is actually wired into production
// config: a real write-shaped deployed tool trips it, a real read-only
// deployed tool never calls the checker at all, and Cerbos is only reached
// once the gate itself passes. The Check() classification logic itself is
// proven separately by internal/moderation and by server_test.go's
// newTestServerWithModeration cases.

func newDeployedServerWithModeration(t *testing.T, d *stubDecider, m *stubModerationChecker) *Server {
	t.Helper()
	mapping := deployedMapping(t)
	e, err := eval.Compile(mapping)
	if err != nil {
		t.Fatalf("compile: %v", err)
	}
	return New(mapping, e, d, Principal{ID: "hermes", Roles: []string{"agent"}},
		WithModeration(m))
}

func TestDeployedMapping_GitHubCreatePullRequestFlaggedContentIsDeniedBeforeCerbos(t *testing.T) {
	d := &stubDecider{allow: true} // would allow if consulted -- proves the gate denies first
	m := &stubModerationChecker{flagged: true, categories: []string{"harassment"}}
	s := newDeployedServerWithModeration(t, d, m)

	res, err := s.CheckRequest(context.Background(),
		mcpReq("vmcp", "tools/call", toolCall("github_create_pull_request",
			map[string]any{"owner": "jchristensen", "repo": "vicegerent-agents", "title": "some flagged content", "body": "body text"})))
	if err != nil {
		t.Fatalf("CheckRequest: %v", err)
	}
	if !isDeny(res) {
		t.Fatalf("expected deny: flagged content on a real deployed write-shaped tool")
	}
	if m.calls != 1 {
		t.Errorf("expected exactly one moderation check, got %d", m.calls)
	}
	if d.calls != 0 {
		t.Errorf("Cerbos must NOT be consulted once the moderation gate denies, got %d calls", d.calls)
	}
}

func TestDeployedMapping_GitHubCreatePullRequestCleanContentReachesCerbos(t *testing.T) {
	d := &stubDecider{allow: true}
	m := &stubModerationChecker{flagged: false}
	s := newDeployedServerWithModeration(t, d, m)

	res, err := s.CheckRequest(context.Background(),
		mcpReq("vmcp", "tools/call", toolCall("github_create_pull_request",
			map[string]any{"owner": "jchristensen", "repo": "vicegerent-agents", "title": "an ordinary PR title", "body": "body text"})))
	if err != nil {
		t.Fatalf("CheckRequest: %v", err)
	}
	if isDeny(res) {
		t.Fatalf("expected pass/mutate: clean content, Cerbos allows -- got deny")
	}
	if m.calls != 1 {
		t.Errorf("expected exactly one moderation check, got %d", m.calls)
	}
	if d.calls != 1 {
		t.Errorf("expected the gated call to reach Cerbos exactly once, got %d", d.calls)
	}
}

// A real read-only deployed tool (kubernetes_resources_get) must never trip
// the moderation gate at all -- isModeratedWriteTool's verb heuristic
// ("create"/"update"/"save"/"add_note"/"add_comment") doesn't match "get",
// so the checker should never even be invoked, deployed-mapping tool names
// included.
func TestDeployedMapping_ReadOnlyToolNeverInvokesModerationChecker(t *testing.T) {
	d := &stubDecider{allow: true}
	m := &stubModerationChecker{flagged: true} // would deny if it ran
	s := newDeployedServerWithModeration(t, d, m)

	res, err := s.CheckRequest(context.Background(),
		mcpReq("vmcp", "tools/call", toolCall("kubernetes_resources_get",
			map[string]any{"name": "some-pod"})))
	if err != nil {
		t.Fatalf("CheckRequest: %v", err)
	}
	if isDeny(res) {
		t.Fatalf("expected pass: a read-only deployed tool must never reach the moderation gate")
	}
	if m.calls != 0 {
		t.Errorf("expected the moderation checker never called for a read-only tool, got %d calls", m.calls)
	}
}

// GitLab has zero mapping.yaml entries by design (no Cerbos policy at all --
// see AGENTS.md/README.md). This proves the moderation gate still fires for
// a GitLab-shaped write tool: it must reach the moderation checker BEFORE
// the "tool not mapped" pass()/deny() branch, not be silently skipped by it.
func TestDeployedMapping_UnmappedGitLabWriteToolStillInvokesModerationChecker(t *testing.T) {
	d := &stubDecider{allow: true}
	m := &stubModerationChecker{flagged: true, categories: []string{"harassment"}}
	s := newDeployedServerWithModeration(t, d, m)

	res, err := s.CheckRequest(context.Background(),
		mcpReq("vmcp", "tools/call", toolCall("gitlab_create_issue",
			map[string]any{"project_id": "123", "title": "some flagged content", "description": "body text"})))
	if err != nil {
		t.Fatalf("CheckRequest: %v", err)
	}
	if !isDeny(res) {
		t.Fatalf("expected deny: flagged content on an unmapped GitLab write tool")
	}
	if reason := res.GetError().GetReason(); !strings.Contains(reason, "flagged by the moderation gate") {
		t.Fatalf("expected the moderation deny reason, got %q (looks like the unmapped-tool pass()/deny() branch fired instead)", reason)
	}
	if m.calls != 1 {
		t.Errorf("expected exactly one moderation check even though gitlab_create_issue has no mapping.yaml entry, got %d", m.calls)
	}
	if d.calls != 0 {
		t.Errorf("Cerbos must NOT be consulted once the moderation gate denies, got %d calls", d.calls)
	}
}

// jira_jira_transition_issue doesn't match create/update/save/add_note/
// add_comment on its own -- "transition" was added to
// DefaultModeratedWriteVerbs specifically to cover it (Jira transitions
// commonly carry an optional free-text comment, e.g. "resolve with comment").
func TestDeployedMapping_JiraTransitionIssueMatchesModerationVerb(t *testing.T) {
	d := &stubDecider{allow: true}
	m := &stubModerationChecker{flagged: true, categories: []string{"harassment"}}
	s := newDeployedServerWithModeration(t, d, m)

	res, err := s.CheckRequest(context.Background(),
		mcpReq("vmcp", "tools/call", toolCall("jira_jira_transition_issue",
			map[string]any{"issue_key": "CHANGE-1", "transition_id": "31", "comment": "some flagged content"})))
	if err != nil {
		t.Fatalf("CheckRequest: %v", err)
	}
	if !isDeny(res) {
		t.Fatalf("expected deny: flagged content on jira_jira_transition_issue")
	}
	if m.calls != 1 {
		t.Errorf("expected exactly one moderation check, got %d", m.calls)
	}
}

// Proves the gate is disabled entirely when moderationChecker is nil (the
// CONTENT_MODERATION=disabled posture main.go wires per-cluster) -- a real
// write-shaped deployed tool must reach Cerbos untouched.
func TestDeployedMapping_ModerationDisabledSkipsGateEntirely(t *testing.T) {
	d := &stubDecider{allow: true}
	mapping := deployedMapping(t)
	e, err := eval.Compile(mapping)
	if err != nil {
		t.Fatalf("compile: %v", err)
	}
	s := New(mapping, e, d, Principal{ID: "hermes", Roles: []string{"agent"}}) // no WithModeration

	res, err := s.CheckRequest(context.Background(),
		mcpReq("vmcp", "tools/call", toolCall("github_create_pull_request",
			map[string]any{"owner": "jchristensen", "repo": "vicegerent-agents", "title": "anything at all", "body": "body text"})))
	if err != nil {
		t.Fatalf("CheckRequest: %v", err)
	}
	if isDeny(res) {
		t.Fatalf("expected pass: moderation gate disabled, Cerbos allows -- got deny")
	}
	if d.calls != 1 {
		t.Errorf("expected the call to reach Cerbos exactly once, got %d", d.calls)
	}
}
