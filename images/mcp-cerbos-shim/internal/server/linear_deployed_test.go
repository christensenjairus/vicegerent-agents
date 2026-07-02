package server

import (
	"context"
	"testing"

	"github.com/jchristensen/vicegerent-agents/images/mcp-cerbos-shim/internal/eval"
)

// These tests run the SHIPPED mapping (not a fixture) through the request path.
// Only linear_create_issue is mapped (teamId is a required, verifiable field);
// update_issue/create_comment target an existing issue by id and carry no
// teamId the shim can check, so they must pass unmapped. The deny *decision*
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
		mcpReq("vmcp", "tools/call", toolCall("linear_create_issue",
			map[string]any{"teamId": "some-other-team-id", "title": "t"})))
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

func TestDeployedLinearMapping_UpdateIssueAndCreateCommentAreUnmapped(t *testing.T) {
	m := deployedMapping(t)
	e, err := eval.Compile(m)
	if err != nil {
		t.Fatalf("compile: %v", err)
	}
	for _, tool := range []string{"linear_update_issue", "linear_create_comment"} {
		t.Run(tool, func(t *testing.T) {
			d := &stubDecider{allow: false}
			s := New(m, e, d, Principal{ID: "hermes", Roles: []string{"agent"}})
			res, err := s.CheckRequest(context.Background(),
				mcpReq("vmcp", "tools/call", toolCall(tool, map[string]any{"id": "issue-1"})))
			if err != nil {
				t.Fatalf("CheckRequest: %v", err)
			}
			if !isPass(res) {
				t.Fatalf("expected pass for unmapped tool %q (no verifiable teamId)", tool)
			}
			if d.calls != 0 {
				t.Errorf("unmapped tool must not call Cerbos, got %d calls", d.calls)
			}
		})
	}
}
