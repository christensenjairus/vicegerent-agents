package server

import (
	"context"
	"testing"

	"github.com/jchristensen/vicegerent-agents/images/mcp-cerbos-shim/internal/eval"
)

// These tests run the SHIPPED mapping (not a fixture) through the request path,
// using the backend name ("vmcp") and prefixed tool names ("github_*") exactly
// as ToolHive's vMCP presents them. They prove the wiring that turns a GitHub
// tool call into the github_repo resource Cerbos denies outside the allowlist;
// the deny *decision* itself is proven by defs/github_test.yaml.

func TestDeployedGithubMapping_MappedToolsReachCerbos(t *testing.T) {
	m := deployedMapping(t)
	e, err := eval.Compile(m)
	if err != nil {
		t.Fatalf("compile: %v", err)
	}

	// One tool per category the GITHUB_TOOLS allowlist enables: issue, PR
	// metadata, and PR full-lifecycle (branch/file/commit) actions.
	cases := []struct {
		tool string
		args map[string]any
	}{
		{"github_issue_read", map[string]any{"owner": "someoneelse", "repo": "some-repo", "issue_number": 1}},
		{"github_create_pull_request", map[string]any{"owner": "someoneelse", "repo": "some-repo", "title": "t", "head": "h", "base": "main"}},
		{"github_push_files", map[string]any{"owner": "someoneelse", "repo": "some-repo", "branch": "b", "message": "m", "files": []any{}}},
		{"github_create_branch", map[string]any{"owner": "someoneelse", "repo": "some-repo", "branch": "b"}},
		{"github_search_issues", map[string]any{"owner": "someoneelse", "repo": "some-repo", "query": "is:open"}},
	}

	for _, tc := range cases {
		t.Run(tc.tool, func(t *testing.T) {
			// allow=false: the shim must forward a well-formed resource to Cerbos
			// and honor its deny (turning it into a PERMISSION_DENIED error).
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
			if d.gotType != "github_repo" {
				t.Errorf("resourceType = %q, want github_repo", d.gotType)
			}
			if d.gotAct != "access" {
				t.Errorf("action = %q, want access", d.gotAct)
			}
			if d.gotAttr["owner"] != "someoneelse" || d.gotAttr["repo"] != "some-repo" {
				t.Errorf("attr = %v, want owner=someoneelse repo=some-repo", d.gotAttr)
			}
			if d.gotID != "someoneelse/some-repo" {
				t.Errorf("resource id = %q, want someoneelse/some-repo", d.gotID)
			}
		})
	}
}

func TestDeployedGithubMapping_AllowedRepoPasses(t *testing.T) {
	m := deployedMapping(t)
	e, err := eval.Compile(m)
	if err != nil {
		t.Fatalf("compile: %v", err)
	}
	d := &stubDecider{allow: true}
	s := New(m, e, d, Principal{ID: "hermes", Roles: []string{"agent"}})
	res, err := s.CheckRequest(context.Background(),
		mcpReq("vmcp", "tools/call", toolCall("github_issue_write",
			map[string]any{"owner": "christensenjairus", "repo": "vicegerent-agents", "issue_number": 1, "method": "update"})))
	if err != nil {
		t.Fatalf("CheckRequest: %v", err)
	}
	if !isPass(res) {
		t.Fatalf("expected pass for an allowed repo")
	}
	if d.gotAttr["owner"] != "christensenjairus" || d.gotAttr["repo"] != "vicegerent-agents" {
		t.Errorf("attr = %v, want owner=christensenjairus repo=vicegerent-agents", d.gotAttr)
	}
}

// TestDeployedGithubMapping_PullRequestsAlwaysForceDraft proves the SHIPPED
// mapping's force block on create/update_pull_request: on an allowed repo, the
// call is forwarded as Mutated with draft rewritten to true regardless of what
// was sent — closing the "create as draft, then update to un-draft" loophole.
func TestDeployedGithubMapping_PullRequestsAlwaysForceDraft(t *testing.T) {
	m := deployedMapping(t)
	e, err := eval.Compile(m)
	if err != nil {
		t.Fatalf("compile: %v", err)
	}

	cases := []struct {
		tool string
		args map[string]any
	}{
		{"github_create_pull_request", map[string]any{
			"owner": "christensenjairus", "repo": "vicegerent-agents",
			"title": "t", "head": "feature-x", "base": "main", "draft": false,
		}},
		{"github_update_pull_request", map[string]any{
			"owner": "christensenjairus", "repo": "vicegerent-agents",
			"pullNumber": 1, "draft": false,
		}},
	}

	for _, tc := range cases {
		t.Run(tc.tool, func(t *testing.T) {
			d := &stubDecider{allow: true}
			s := New(m, e, d, Principal{ID: "hermes", Roles: []string{"agent"}})
			res, err := s.CheckRequest(context.Background(),
				mcpReq("vmcp", "tools/call", toolCall(tc.tool, tc.args)))
			if err != nil {
				t.Fatalf("CheckRequest: %v", err)
			}
			if !isMutated(res) {
				t.Fatalf("expected a mutated (forced-draft) result, got pass=%v deny=%v", isPass(res), isDeny(res))
			}
			name, args := decodeMutated(t, res)
			if name != tc.tool {
				t.Errorf("mutated name = %q, want %q", name, tc.tool)
			}
			if args["draft"] != true {
				t.Errorf("draft = %v, want true (forced)", args["draft"])
			}
			if args["owner"] != "christensenjairus" || args["repo"] != "vicegerent-agents" {
				t.Errorf("owner/repo not preserved: %v", args)
			}
		})
	}
}

// TestDeployedGithubMapping_PullRequestDraftForceDoesNotBypassRepoAllowlist
// proves force only fires after Cerbos allows — a disallowed repo still denies,
// draft or not.
func TestDeployedGithubMapping_PullRequestDraftForceDoesNotBypassRepoAllowlist(t *testing.T) {
	m := deployedMapping(t)
	e, err := eval.Compile(m)
	if err != nil {
		t.Fatalf("compile: %v", err)
	}
	d := &stubDecider{allow: false}
	s := New(m, e, d, Principal{ID: "hermes", Roles: []string{"agent"}})
	res, err := s.CheckRequest(context.Background(),
		mcpReq("vmcp", "tools/call", toolCall("github_create_pull_request",
			map[string]any{"owner": "someoneelse", "repo": "some-repo", "title": "t", "head": "h", "base": "main"})))
	if err != nil {
		t.Fatalf("CheckRequest: %v", err)
	}
	if !isDeny(res) {
		t.Fatalf("expected deny for a disallowed repo")
	}
	if isMutated(res) {
		t.Fatalf("a denied call must never carry a mutation")
	}
}

func TestDeployedGithubMapping_GetMeIsUnmappedAndPasses(t *testing.T) {
	m := deployedMapping(t)
	e, err := eval.Compile(m)
	if err != nil {
		t.Fatalf("compile: %v", err)
	}
	// get_me identifies the caller, not a repo — it carries no owner/repo and
	// must not be mapped; confirms the guardrail doesn't over-block.
	d := &stubDecider{allow: false}
	s := New(m, e, d, Principal{ID: "hermes", Roles: []string{"agent"}})
	res, err := s.CheckRequest(context.Background(),
		mcpReq("vmcp", "tools/call", toolCall("github_get_me", map[string]any{})))
	if err != nil {
		t.Fatalf("CheckRequest: %v", err)
	}
	if !isPass(res) {
		t.Fatalf("expected pass for the unmapped github_get_me tool")
	}
	if d.calls != 0 {
		t.Errorf("unmapped tool must not call Cerbos, got %d calls", d.calls)
	}
}
