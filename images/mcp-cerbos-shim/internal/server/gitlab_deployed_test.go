package server

import (
	"context"
	"testing"

	"github.com/jchristensen/vicegerent-agents/images/mcp-cerbos-shim/internal/eval"
)

// These tests run the SHIPPED mapping (not a fixture) through the request path,
// using the backend name ("vmcp") and prefixed tool names ("gitlab_*") exactly
// as ToolHive's vMCP presents them. They prove the wiring that turns a
// branch-writing GitLab tool call into the gitlab_repo resource Cerbos denies
// for a protected branch; the deny *decision* itself is proven by
// defs/gitlab_test.yaml. There is no project-id allowlist for GitLab (the bot's
// PAT is already project-scoped) — unlike GitHub, this mapping only checks branch.

func TestDeployedGitlabMapping_BranchWritingToolsReachCerbos(t *testing.T) {
	m := deployedMapping(t)
	e, err := eval.Compile(m)
	if err != nil {
		t.Fatalf("compile: %v", err)
	}

	cases := []struct {
		tool string
		args map[string]any
	}{
		{"gitlab_push_files", map[string]any{"project_id": "42", "branch": "main", "commit_message": "m", "files": []any{}}},
		{"gitlab_create_or_update_file", map[string]any{"project_id": "42", "branch": "main", "file_path": "f", "content": "c", "commit_message": "m"}},
		{"gitlab_create_branch", map[string]any{"project_id": "42", "branch": "main"}},
	}

	for _, tc := range cases {
		t.Run(tc.tool, func(t *testing.T) {
			d := &stubDecider{allow: false}
			s := New(m, e, d, Principal{ID: "hermes", Roles: []string{"agent"}})
			res, err := s.CheckRequest(context.Background(),
				mcpReq("vmcp", "tools/call", toolCall(tc.tool, tc.args)))
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
			if d.gotType != "gitlab_repo" {
				t.Errorf("resourceType = %q, want gitlab_repo", d.gotType)
			}
			if d.gotAttr["branch"] != "main" {
				t.Errorf("attr.branch = %q, want main", d.gotAttr["branch"])
			}
		})
	}
}

func TestDeployedGitlabMapping_NonProtectedBranchPasses(t *testing.T) {
	m := deployedMapping(t)
	e, err := eval.Compile(m)
	if err != nil {
		t.Fatalf("compile: %v", err)
	}
	d := &stubDecider{allow: true}
	s := New(m, e, d, Principal{ID: "hermes", Roles: []string{"agent"}})
	res, err := s.CheckRequest(context.Background(),
		mcpReq("vmcp", "tools/call", toolCall("gitlab_create_branch",
			map[string]any{"project_id": "42", "branch": "feature-x"})))
	if err != nil {
		t.Fatalf("CheckRequest: %v", err)
	}
	if !isPass(res) {
		t.Fatalf("expected pass for a non-protected branch")
	}
	if d.gotAttr["branch"] != "feature-x" {
		t.Errorf("attr.branch = %q, want feature-x", d.gotAttr["branch"])
	}
}

func TestDeployedGitlabMapping_IssueAndMergeRequestToolsAreUnmapped(t *testing.T) {
	// Issue/MR tools carry no branch arg and no project allowlist applies to
	// GitLab, so they must pass without ever reaching Cerbos.
	m := deployedMapping(t)
	e, err := eval.Compile(m)
	if err != nil {
		t.Fatalf("compile: %v", err)
	}
	for _, tool := range []string{"gitlab_create_issue", "gitlab_create_merge_request", "gitlab_approve_merge_request"} {
		t.Run(tool, func(t *testing.T) {
			d := &stubDecider{allow: false}
			s := New(m, e, d, Principal{ID: "hermes", Roles: []string{"agent"}})
			res, err := s.CheckRequest(context.Background(),
				mcpReq("vmcp", "tools/call", toolCall(tool, map[string]any{"project_id": "42"})))
			if err != nil {
				t.Fatalf("CheckRequest: %v", err)
			}
			if !isPass(res) {
				t.Fatalf("expected pass for unmapped tool %q", tool)
			}
			if d.calls != 0 {
				t.Errorf("unmapped tool must not call Cerbos, got %d calls", d.calls)
			}
		})
	}
}
