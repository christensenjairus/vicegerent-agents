package server

import (
	"context"
	"testing"

	"github.com/jchristensen/vicegerent-agents/images/mcp-cerbos-shim/internal/eval"
)

// These tests run the SHIPPED mapping (not a fixture) through the request path,
// using the backend name ("vmcp") and prefixed tool names ("gitlab_*") exactly
// as ToolHive's vMCP presents them.
//
// GitLab has zero Cerbos-mapped tools by design, and no gitlab_repo resource
// or policy exists anymore: push_files/create_or_update_file/create_branch
// (the only three tools that ever carried a 'branch' arg) were removed from
// the tool allowlist entirely (toolhive-servers.json) — the bot has direct
// SSH access to gitlab.hahomelabs.com, so routine git operations go through
// git itself now, not a GitLab-API tool. This operator isn't picky about
// GitLab behavior generally (it's their own instance), so every GitLab tool
// (issues, MR object/comments/discussions/notes/labels/todos/pipelines) is
// left unmapped and passes. This test guards against a future accidental
// re-add of a gitlab_repo mapping without the matching Cerbos policy landing
// alongside it.

func TestDeployedGitlabMapping_AllToolsAreUnmapped(t *testing.T) {
	m := deployedMapping(t)
	e, err := eval.Compile(m)
	if err != nil {
		t.Fatalf("compile: %v", err)
	}
	tools := []string{
		// Formerly branch-writing tools -- removed entirely; git-over-SSH
		// replaces them. Confirms the removal actually took (unmapped, not
		// just an accidental gap from a typo in the allowlist).
		"gitlab_push_files", "gitlab_create_or_update_file", "gitlab_create_branch",
		// Issue/MR tools -- no project allowlist applies to GitLab (the bot's
		// PAT is already project-scoped), so these were never mapped.
		"gitlab_create_issue", "gitlab_create_merge_request", "gitlab_approve_merge_request",
	}
	for _, tool := range tools {
		t.Run(tool, func(t *testing.T) {
			d := &stubDecider{allow: false}
			s := New(m, e, d, Principal{ID: "hermes", Roles: []string{"agent"}})
			res, err := s.CheckRequest(context.Background(),
				mcpReq("vmcp", "tools/call", toolCall(tool,
					map[string]any{"project_id": "42", "branch": "main"})))
			if err != nil {
				t.Fatalf("CheckRequest: %v", err)
			}
			if !isPass(res) {
				t.Fatalf("expected pass for unmapped tool %q (falls through to defaultAction: allow)", tool)
			}
			if d.calls != 0 {
				t.Errorf("unmapped tool %q must not call Cerbos, got %d calls", tool, d.calls)
			}
		})
	}
}
