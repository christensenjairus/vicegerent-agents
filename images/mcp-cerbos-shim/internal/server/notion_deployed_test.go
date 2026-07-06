package server

import (
	"context"
	"testing"

	"github.com/jchristensen/vicegerent-agents/images/mcp-cerbos-shim/internal/eval"
)

// These tests run the SHIPPED mapping (not a fixture) through the request path.
// They prove the wiring that turns a Notion create-pages/update-page call into
// the notion_page resource Cerbos denies/allows; the deny *decision* itself is
// proven by defs/notion_test.yaml.

func TestDeployedNotionMapping_CreatePagesReachesCerbosWithParentAttrs(t *testing.T) {
	m := deployedMapping(t)
	e, err := eval.Compile(m)
	if err != nil {
		t.Fatalf("compile: %v", err)
	}
	d := &stubDecider{allow: false} // prove the shim forwards a well-formed resource and honors Cerbos's deny
	s := New(m, e, d, Principal{ID: "hermes", Roles: []string{"agent"}})
	res, err := s.CheckRequest(context.Background(),
		mcpReq("vmcp", "tools/call", toolCall("notion_notion-create-pages",
			map[string]any{
				"pages":  []any{map[string]any{"properties": map[string]any{"title": "t"}}},
				"parent": map[string]any{"page_id": "some-other-page-id"},
			})))
	if err != nil {
		t.Fatalf("CheckRequest: %v", err)
	}
	if !isDeny(res) {
		t.Fatalf("expected deny when Cerbos denies, got pass")
	}
	if d.calls != 1 {
		t.Fatalf("expected exactly one Cerbos check, got %d", d.calls)
	}
	if d.gotType != "notion_page" {
		t.Errorf("resourceType = %q, want notion_page", d.gotType)
	}
	if d.gotAct != "create" {
		t.Errorf("action = %q, want create", d.gotAct)
	}
	if d.gotAttr["parentKind"] != "page_id" {
		t.Errorf("attr.parentKind = %q, want page_id", d.gotAttr["parentKind"])
	}
	if d.gotAttr["parentPageId"] != "someotherpageid" {
		t.Errorf("attr.parentPageId = %q, want someotherpageid (dashes stripped, lowercased)", d.gotAttr["parentPageId"])
	}
}

func TestDeployedNotionMapping_CreatePagesOmittedParentStillReachesCerbos(t *testing.T) {
	m := deployedMapping(t)
	e, err := eval.Compile(m)
	if err != nil {
		t.Fatalf("compile: %v", err)
	}
	d := &stubDecider{allow: false}
	s := New(m, e, d, Principal{ID: "hermes", Roles: []string{"agent"}})
	res, err := s.CheckRequest(context.Background(),
		mcpReq("vmcp", "tools/call", toolCall("notion_notion-create-pages",
			map[string]any{"pages": []any{map[string]any{"properties": map[string]any{"title": "t"}}}})))
	if err != nil {
		t.Fatalf("CheckRequest: %v", err)
	}
	if !isDeny(res) {
		t.Fatalf("expected deny (omitted parent is not Scratchpad), got pass")
	}
	if d.gotAttr["parentKind"] != "" {
		t.Errorf("attr.parentKind = %q, want empty (no parent supplied)", d.gotAttr["parentKind"])
	}
}

func TestDeployedNotionMapping_UpdatePageReachesCerbosWithCommandAndDeleteAttrs(t *testing.T) {
	m := deployedMapping(t)
	e, err := eval.Compile(m)
	if err != nil {
		t.Fatalf("compile: %v", err)
	}
	d := &stubDecider{allow: false}
	s := New(m, e, d, Principal{ID: "hermes", Roles: []string{"agent"}})
	res, err := s.CheckRequest(context.Background(),
		mcpReq("vmcp", "tools/call", toolCall("notion_notion-update-page",
			map[string]any{"page_id": "abc123", "command": "replace_content", "allow_deleting_content": true})))
	if err != nil {
		t.Fatalf("CheckRequest: %v", err)
	}
	if !isDeny(res) {
		t.Fatalf("expected deny when Cerbos denies, got pass")
	}
	if d.gotType != "notion_page" {
		t.Errorf("resourceType = %q, want notion_page", d.gotType)
	}
	if d.gotAct != "update" {
		t.Errorf("action = %q, want update", d.gotAct)
	}
	if d.gotAttr["command"] != "replace_content" {
		t.Errorf("attr.command = %q, want replace_content", d.gotAttr["command"])
	}
	if d.gotAttr["allowDeletingContent"] != "true" {
		t.Errorf("attr.allowDeletingContent = %q, want true", d.gotAttr["allowDeletingContent"])
	}
}
