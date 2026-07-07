package eval

import (
	"testing"

	config "github.com/jchristensen/vicegerent-agents/images/mcp-cerbos-shim/internal"
)

func TestJiraHelperSelfRegisters(t *testing.T) {
	if _, ok := helperOptions("jiraFieldsAttr"); !ok {
		t.Fatal("jiraFieldsAttr not registered; helpers_jira.go init() did not run")
	}
}

func compileJiraTestEngine(t *testing.T) *Engine {
	t.Helper()
	m := &config.Mapping{
		Backends: map[string]config.Backend{
			"vmcp": {
				DefaultAction: config.ActionAllow,
				Helpers:       []string{"jiraFieldsAttr"},
				Tools: map[string]config.Tool{
					"jira_jira_create_issue": {
						ResourceType: "jira_project",
						Action:       "write",
						ID:           "get(args,'project_key','')",
						AttrFrom:     "jiraFieldsAttr(args)",
						Attr: map[string]string{
							"projectKey": "get(args,'project_key','')",
						},
					},
				},
			},
		},
	}
	e, err := Compile(m)
	if err != nil {
		t.Fatalf("compile: %v", err)
	}
	return e
}

func TestJiraFieldsAttr_EpicKeySmuggledInAdditionalFields(t *testing.T) {
	e := compileJiraTestEngine(t)
	res, err := e.Eval(CallInput{
		Backend: "vmcp",
		Tool:    "jira_jira_create_issue",
		Args: map[string]any{
			"project_key":       "CHANGE",
			"additional_fields": `{"epicKey": "OTHER-123"}`,
		},
	})
	if err != nil {
		t.Fatalf("Eval: %v", err)
	}
	if got := res.Attr["extraEpicKey"]; got != "OTHER-123" {
		t.Errorf("extraEpicKey = %q, want OTHER-123", got)
	}
	if got := res.Attr["projectKey"]; got != "CHANGE" {
		t.Errorf("projectKey = %q, want CHANGE (Attr overlay should still apply)", got)
	}
}

func TestJiraFieldsAttr_ParentSmuggledInFields(t *testing.T) {
	e := compileJiraTestEngine(t)
	res, err := e.Eval(CallInput{
		Backend: "vmcp",
		Tool:    "jira_jira_create_issue",
		Args: map[string]any{
			"project_key": "CHANGE",
			"fields":      `{"parent": "OTHER-456"}`,
		},
	})
	if err != nil {
		t.Fatalf("Eval: %v", err)
	}
	if got := res.Attr["extraParentKey"]; got != "OTHER-456" {
		t.Errorf("extraParentKey = %q, want OTHER-456", got)
	}
}

func TestJiraFieldsAttr_EpicLinkAliasAlsoDetected(t *testing.T) {
	e := compileJiraTestEngine(t)
	res, err := e.Eval(CallInput{
		Backend: "vmcp",
		Tool:    "jira_jira_create_issue",
		Args: map[string]any{
			"project_key":       "CHANGE",
			"additional_fields": `{"epic_link": "OTHER-789"}`,
		},
	})
	if err != nil {
		t.Fatalf("Eval: %v", err)
	}
	if got := res.Attr["extraEpicKey"]; got != "OTHER-789" {
		t.Errorf("extraEpicKey = %q, want OTHER-789 (epic_link alias)", got)
	}
}

func TestJiraFieldsAttr_MalformedJSONFailsOpenNotClosed(t *testing.T) {
	e := compileJiraTestEngine(t)
	res, err := e.Eval(CallInput{
		Backend: "vmcp",
		Tool:    "jira_jira_create_issue",
		Args: map[string]any{
			"project_key":       "CHANGE",
			"additional_fields": `{not valid json`,
		},
	})
	if err != nil {
		t.Fatalf("Eval: %v", err)
	}
	if got := res.Attr["extraEpicKey"]; got != "" {
		t.Errorf("extraEpicKey = %q, want empty on malformed JSON (unparseable field isn't checked, not denied)", got)
	}
}

func TestJiraFieldsAttr_NoAdditionalFieldsOrFieldsResolvesEmpty(t *testing.T) {
	e := compileJiraTestEngine(t)
	res, err := e.Eval(CallInput{
		Backend: "vmcp",
		Tool:    "jira_jira_create_issue",
		Args: map[string]any{
			"project_key": "CHANGE",
		},
	})
	if err != nil {
		t.Fatalf("Eval: %v", err)
	}
	if got := res.Attr["extraEpicKey"]; got != "" {
		t.Errorf("extraEpicKey = %q, want empty when neither JSON arg is present", got)
	}
	if got := res.Attr["extraParentKey"]; got != "" {
		t.Errorf("extraParentKey = %q, want empty when neither JSON arg is present", got)
	}
}

func TestJiraFieldsAttr_NestedObjectParentIsNotUnwrapped(t *testing.T) {
	// Documents the deliberate scope limit noted in jsonStringField's
	// comment: a {"parent": {"key": "OTHER-123"}} nested-object shape isn't
	// unwrapped -- none of the tool's documented additional_fields examples
	// use that shape for parent/epicKey specifically.
	e := compileJiraTestEngine(t)
	res, err := e.Eval(CallInput{
		Backend: "vmcp",
		Tool:    "jira_jira_create_issue",
		Args: map[string]any{
			"project_key":       "CHANGE",
			"additional_fields": `{"parent": {"key": "OTHER-123"}}`,
		},
	})
	if err != nil {
		t.Fatalf("Eval: %v", err)
	}
	if got := res.Attr["extraParentKey"]; got != "" {
		t.Errorf("extraParentKey = %q, want empty for nested-object parent (documented scope limit, not a bug)", got)
	}
}

func TestJiraFieldsAttr_TopLevelAssigneeSurfaced(t *testing.T) {
	e := compileJiraTestEngine(t)
	res, err := e.Eval(CallInput{
		Backend: "vmcp",
		Tool:    "jira_jira_create_issue",
		Args: map[string]any{
			"project_key": "CHANGE",
			"assignee":    "someone@example.com",
		},
	})
	if err != nil {
		t.Fatalf("Eval: %v", err)
	}
	if got := res.Attr["assignee"]; got != "someone@example.com" {
		t.Errorf("assignee = %q, want someone@example.com (top-level create_issue arg)", got)
	}
}

func TestJiraFieldsAttr_AssigneeInFieldsJSONSurfaced(t *testing.T) {
	// update_issue has no top-level assignee arg -- it only ever arrives
	// inside `fields`.
	e := compileJiraTestEngine(t)
	res, err := e.Eval(CallInput{
		Backend: "vmcp",
		Tool:    "jira_jira_create_issue",
		Args: map[string]any{
			"project_key": "CHANGE",
			"fields":      `{"assignee": "fields-assignee@example.com"}`,
		},
	})
	if err != nil {
		t.Fatalf("Eval: %v", err)
	}
	if got := res.Attr["assignee"]; got != "fields-assignee@example.com" {
		t.Errorf("assignee = %q, want fields-assignee@example.com", got)
	}
}

func TestJiraFieldsAttr_TopLevelAssigneeTakesPrecedenceOverFieldsJSON(t *testing.T) {
	e := compileJiraTestEngine(t)
	res, err := e.Eval(CallInput{
		Backend: "vmcp",
		Tool:    "jira_jira_create_issue",
		Args: map[string]any{
			"project_key": "CHANGE",
			"assignee":    "top-level@example.com",
			"fields":      `{"assignee": "json-assignee@example.com"}`,
		},
	})
	if err != nil {
		t.Fatalf("Eval: %v", err)
	}
	if got := res.Attr["assignee"]; got != "top-level@example.com" {
		t.Errorf("assignee = %q, want top-level@example.com (top-level arg wins)", got)
	}
}

func TestJiraFieldsAttr_NoAssigneeAnywhereResolvesEmpty(t *testing.T) {
	e := compileJiraTestEngine(t)
	res, err := e.Eval(CallInput{
		Backend: "vmcp",
		Tool:    "jira_jira_create_issue",
		Args: map[string]any{
			"project_key": "CHANGE",
		},
	})
	if err != nil {
		t.Fatalf("Eval: %v", err)
	}
	if got := res.Attr["assignee"]; got != "" {
		t.Errorf("assignee = %q, want empty when no assignee is set anywhere", got)
	}
}

func TestJiraFieldsAttr_TopLevelIssueTypeSurfaced(t *testing.T) {
	e := compileJiraTestEngine(t)
	res, err := e.Eval(CallInput{
		Backend: "vmcp",
		Tool:    "jira_jira_create_issue",
		Args: map[string]any{
			"project_key": "CHANGE",
			"issue_type":  "Epic",
		},
	})
	if err != nil {
		t.Fatalf("Eval: %v", err)
	}
	if got := res.Attr["issueType"]; got != "Epic" {
		t.Errorf("issueType = %q, want Epic (top-level create_issue arg)", got)
	}
}

func TestJiraFieldsAttr_IssueTypeSmuggledInFieldsAsPlainString(t *testing.T) {
	// update_issue has no top-level issue_type arg -- it only ever arrives
	// inside fields/additional_fields JSON, either a plain string or Jira's
	// own REST-API {"name": "Epic"} object shape.
	e := compileJiraTestEngine(t)
	res, err := e.Eval(CallInput{
		Backend: "vmcp",
		Tool:    "jira_jira_create_issue",
		Args: map[string]any{
			"project_key": "CHANGE",
			"fields":      `{"issuetype": "Subtask"}`,
		},
	})
	if err != nil {
		t.Fatalf("Eval: %v", err)
	}
	if got := res.Attr["issueType"]; got != "Subtask" {
		t.Errorf("issueType = %q, want Subtask", got)
	}
}

func TestJiraFieldsAttr_IssueTypeSmuggledAsRESTAPIObjectShape(t *testing.T) {
	e := compileJiraTestEngine(t)
	res, err := e.Eval(CallInput{
		Backend: "vmcp",
		Tool:    "jira_jira_create_issue",
		Args: map[string]any{
			"project_key":       "CHANGE",
			"additional_fields": `{"issueType": {"name": "Epic"}}`,
		},
	})
	if err != nil {
		t.Fatalf("Eval: %v", err)
	}
	if got := res.Attr["issueType"]; got != "Epic" {
		t.Errorf("issueType = %q, want Epic (Jira REST-API {\"name\": ...} object shape)", got)
	}
}

func TestJiraFieldsAttr_TopLevelIssueTypeTakesPrecedenceOverFieldsJSON(t *testing.T) {
	e := compileJiraTestEngine(t)
	res, err := e.Eval(CallInput{
		Backend: "vmcp",
		Tool:    "jira_jira_create_issue",
		Args: map[string]any{
			"project_key": "CHANGE",
			"issue_type":  "Task",
			"fields":      `{"issuetype": "Epic"}`,
		},
	})
	if err != nil {
		t.Fatalf("Eval: %v", err)
	}
	if got := res.Attr["issueType"]; got != "Task" {
		t.Errorf("issueType = %q, want Task (top-level arg wins)", got)
	}
}

func TestJiraFieldsAttr_NoIssueTypeAnywhereResolvesEmpty(t *testing.T) {
	e := compileJiraTestEngine(t)
	res, err := e.Eval(CallInput{
		Backend: "vmcp",
		Tool:    "jira_jira_create_issue",
		Args: map[string]any{
			"project_key": "CHANGE",
		},
	})
	if err != nil {
		t.Fatalf("Eval: %v", err)
	}
	if got := res.Attr["issueType"]; got != "" {
		t.Errorf("issueType = %q, want empty when no issue_type is set anywhere", got)
	}
}
