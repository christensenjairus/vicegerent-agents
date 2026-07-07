package server

import (
	"context"
	"testing"

	"github.com/jchristensen/vicegerent-agents/images/mcp-cerbos-shim/internal/eval"
)

// These tests run the SHIPPED mapping (not a fixture) through the request
// path, proving the wiring that turns a Jira read tool into action:"read"
// (unconditionally reaches Cerbos, which allows any project) and a Jira
// write tool into action:"write" (which resource_jira.yaml's
// deny-write-outside-allowed-projects rule can deny) -- the deny/allow
// *decision* itself is proven by defs/jira_test.yaml. Also proves the
// HAH-90-follow-up jiraFieldsAttr wiring: additional_fields/fields JSON
// smuggling an out-of-scope epicKey/parent reaches Cerbos as
// extraEpicKey/extraParentKey attrs. Also proves the HAH-92 issue-type
// wiring: create_issue's top-level issue_type, and update_issue's fields-JSON
// side channel (no top-level arg exists there), both reach Cerbos as
// issueType.

func TestDeployedJiraMapping_ReadToolsUseReadAction(t *testing.T) {
	m := deployedMapping(t)
	e, err := eval.Compile(m)
	if err != nil {
		t.Fatalf("compile: %v", err)
	}

	for _, tool := range []string{"jira_jira_get_issue", "jira_jira_get_project_issues", "jira_jira_get_transitions"} {
		t.Run(tool, func(t *testing.T) {
			d := &stubDecider{allow: true}
			s := New(m, e, d, Principal{ID: "hermes", Roles: []string{"agent"}})
			res, err := s.CheckRequest(context.Background(),
				mcpReq("vmcp", "tools/call", toolCall(tool, map[string]any{"issue_key": "FOO-9"})))
			if err != nil {
				t.Fatalf("CheckRequest: %v", err)
			}
			if !isPass(res) {
				t.Fatalf("expected pass")
			}
			if d.gotType != "jira_project" {
				t.Errorf("resourceType = %q, want jira_project", d.gotType)
			}
			if d.gotAct != "read" {
				t.Errorf("action = %q, want read", d.gotAct)
			}
		})
	}
}

func TestDeployedJiraMapping_WriteToolsUseWriteAction(t *testing.T) {
	m := deployedMapping(t)
	e, err := eval.Compile(m)
	if err != nil {
		t.Fatalf("compile: %v", err)
	}

	for _, tool := range []string{
		"jira_jira_create_issue", "jira_jira_update_issue", "jira_jira_transition_issue",
		"jira_jira_add_comment", "jira_jira_create_issue_link", "jira_jira_link_to_epic",
	} {
		t.Run(tool, func(t *testing.T) {
			// allow=false: the shim must forward a well-formed resource to
			// Cerbos and honor its deny.
			d := &stubDecider{allow: false}
			s := New(m, e, d, Principal{ID: "hermes", Roles: []string{"agent"}})
			res, err := s.CheckRequest(context.Background(),
				mcpReq("vmcp", "tools/call", toolCall(tool, map[string]any{"project_key": "FOO"})))
			if err != nil {
				t.Fatalf("CheckRequest: %v", err)
			}
			if !isDeny(res) {
				t.Fatalf("expected deny when Cerbos denies, got pass")
			}
			if d.gotType != "jira_project" {
				t.Errorf("resourceType = %q, want jira_project", d.gotType)
			}
			if d.gotAct != "write" {
				t.Errorf("action = %q, want write", d.gotAct)
			}
		})
	}
}

func TestDeployedJiraMapping_AdditionalFieldsEpicKeySmugglingReachesCerbos(t *testing.T) {
	m := deployedMapping(t)
	e, err := eval.Compile(m)
	if err != nil {
		t.Fatalf("compile: %v", err)
	}

	d := &stubDecider{allow: false}
	s := New(m, e, d, Principal{ID: "hermes", Roles: []string{"agent"}})
	res, err := s.CheckRequest(context.Background(),
		mcpReq("vmcp", "tools/call", toolCall("jira_jira_create_issue", map[string]any{
			"project_key":       "CHANGE",
			"additional_fields": `{"epicKey": "OTHER-123"}`,
		})))
	if err != nil {
		t.Fatalf("CheckRequest: %v", err)
	}
	if !isDeny(res) {
		t.Fatalf("expected deny when Cerbos denies")
	}
	if d.gotAttr["extraEpicKey"] != "OTHER-123" {
		t.Errorf("attr.extraEpicKey = %q, want OTHER-123 -- the shipped mapping must surface a smuggled epicKey", d.gotAttr["extraEpicKey"])
	}
	if d.gotAttr["projectKey"] != "CHANGE" {
		t.Errorf("attr.projectKey = %q, want CHANGE -- Attr overlay must survive attrFrom merge", d.gotAttr["projectKey"])
	}
}

func TestDeployedJiraMapping_TopLevelIssueTypeReachesCerbos(t *testing.T) {
	m := deployedMapping(t)
	e, err := eval.Compile(m)
	if err != nil {
		t.Fatalf("compile: %v", err)
	}

	d := &stubDecider{allow: false}
	s := New(m, e, d, Principal{ID: "hermes", Roles: []string{"agent"}})
	res, err := s.CheckRequest(context.Background(),
		mcpReq("vmcp", "tools/call", toolCall("jira_jira_create_issue", map[string]any{
			"project_key": "CHANGE",
			"issue_type":  "Epic",
		})))
	if err != nil {
		t.Fatalf("CheckRequest: %v", err)
	}
	if !isDeny(res) {
		t.Fatalf("expected deny when Cerbos denies")
	}
	if d.gotAttr["issueType"] != "Epic" {
		t.Errorf("attr.issueType = %q, want Epic -- the shipped mapping must surface create_issue's top-level issue_type", d.gotAttr["issueType"])
	}
}

func TestDeployedJiraMapping_SmuggledIssueTypeInFieldsReachesCerbos(t *testing.T) {
	m := deployedMapping(t)
	e, err := eval.Compile(m)
	if err != nil {
		t.Fatalf("compile: %v", err)
	}

	d := &stubDecider{allow: false}
	s := New(m, e, d, Principal{ID: "hermes", Roles: []string{"agent"}})
	res, err := s.CheckRequest(context.Background(),
		mcpReq("vmcp", "tools/call", toolCall("jira_jira_update_issue", map[string]any{
			"issue_key": "CHANGE-1",
			"fields":    `{"issuetype": "Subtask"}`,
		})))
	if err != nil {
		t.Fatalf("CheckRequest: %v", err)
	}
	if !isDeny(res) {
		t.Fatalf("expected deny when Cerbos denies")
	}
	if d.gotAttr["issueType"] != "Subtask" {
		t.Errorf("attr.issueType = %q, want Subtask -- update_issue has no top-level issue_type arg, only via fields JSON", d.gotAttr["issueType"])
	}
}
