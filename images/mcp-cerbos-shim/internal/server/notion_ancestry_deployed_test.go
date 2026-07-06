package server

import (
	"context"
	"errors"
	"testing"

	"github.com/jchristensen/vicegerent-agents/images/mcp-cerbos-shim/internal/eval"
	"github.com/jchristensen/vicegerent-agents/images/mcp-cerbos-shim/internal/upstream"
)

// These tests run the SHIPPED mapping (not a fixture) through the request path
// for notion_notion-update-page, with a FAKE upstream (no network) standing in
// for the live vMCP notion-fetch the ancestry gate calls. They prove the gate
// wiring: a page outside the Scratchpad tree is denied before Cerbos is ever
// consulted, a page under Scratchpad passes the gate through to Cerbos, and a
// lookup failure fails closed. The Notion destructive-command deny decision
// itself is proven separately by defs/notion_test.yaml.
//
// The deployed mapping ships an unsubstituted ${notionScratchpadPageId} (Flux
// fills it at apply time), so these tests inject their own scratchpad id via
// WithNotionAncestry rather than reading it out of the mapping.

const testScratchpadID = "393de8859710809c9f5ec57a91d2c81a" // pragma: allowlist secret

// fakeUpstream is a server-package ToolCaller stub: it returns a canned
// notion-fetch text or an error, with no network.
type fakeUpstream struct {
	text  string
	err   error
	calls int
}

func (f *fakeUpstream) CallTool(_ context.Context, _ string, _ map[string]any) (*upstream.CallToolResult, error) {
	f.calls++
	if f.err != nil {
		return nil, f.err
	}
	return &upstream.CallToolResult{Content: []struct {
		Type string `json:"type"`
		Text string `json:"text"`
	}{{Type: "text", Text: f.text}}}, nil
}

const fetchUnderScratchpad = `<page url="https://app.notion.com/p/leaf0000leaf0000leaf0000leaf0000" title="Leaf">
<ancestor-path>
<parent-page url="https://app.notion.com/p/393de8859710809c9f5ec57a91d2c81a" title="Scratchpad"/>
</ancestor-path>
</page>`

const fetchElsewhere = `<page url="https://app.notion.com/p/leaf0000leaf0000leaf0000leaf0000" title="Leaf">
<ancestor-path>
<parent-page url="https://app.notion.com/p/9999000099990000999900009999abcd" title="Other Folder"/>
</ancestor-path>
</page>`

func newNotionServer(t *testing.T, d *stubDecider, up upstream.ToolCaller) *Server {
	t.Helper()
	return newNotionServerWithParents(t, d, up, []string{testScratchpadID})
}

// newNotionServerWithParents lets multi-parent-scoping tests configure more
// than one allowed parent, mirroring HAH's Scratchpad-plus-team-folders
// production setup (NOTION_ALLOWED_PARENT_PAGE_IDS in main.go).
func newNotionServerWithParents(t *testing.T, d *stubDecider, up upstream.ToolCaller, allowedParentIDs []string) *Server {
	t.Helper()
	m := deployedMapping(t)
	e, err := eval.Compile(m)
	if err != nil {
		t.Fatalf("compile: %v", err)
	}
	return New(m, e, d, Principal{ID: "hermes", Roles: []string{"agent"}},
		WithNotionAncestry(up, allowedParentIDs))
}

func TestDeployedNotionMapping_UpdatePageNotUnderScratchpadIsDeniedBeforeCerbos(t *testing.T) {
	d := &stubDecider{allow: true} // would allow if consulted — proves the gate denies first
	up := &fakeUpstream{text: fetchElsewhere}
	s := newNotionServer(t, d, up)
	res, err := s.CheckRequest(context.Background(),
		mcpReq("vmcp", "tools/call", toolCall("notion_notion-update-page",
			map[string]any{"page_id": "leaf0000leaf0000leaf0000leaf0000", "command": "update_content"})))
	if err != nil {
		t.Fatalf("CheckRequest: %v", err)
	}
	if !isDeny(res) {
		t.Fatalf("expected deny for a page outside the Scratchpad tree, got pass/mutate")
	}
	assertNoSideEffects(t, res)
	if up.calls != 1 {
		t.Errorf("expected exactly one notion-fetch ancestry lookup, got %d", up.calls)
	}
	if d.calls != 0 {
		t.Errorf("Cerbos must NOT be consulted once the ancestry gate denies, got %d calls", d.calls)
	}
}

func TestDeployedNotionMapping_UpdatePageUnderScratchpadReachesCerbos(t *testing.T) {
	d := &stubDecider{allow: true}
	up := &fakeUpstream{text: fetchUnderScratchpad}
	s := newNotionServer(t, d, up)
	res, err := s.CheckRequest(context.Background(),
		mcpReq("vmcp", "tools/call", toolCall("notion_notion-update-page",
			map[string]any{"page_id": "leaf0000leaf0000leaf0000leaf0000", "command": "update_content"})))
	if err != nil {
		t.Fatalf("CheckRequest: %v", err)
	}
	if !isPass(res) {
		t.Fatalf("expected pass: page is under Scratchpad and Cerbos allows a non-destructive command")
	}
	if up.calls != 1 {
		t.Errorf("expected exactly one notion-fetch ancestry lookup, got %d", up.calls)
	}
	if d.calls != 1 {
		t.Fatalf("expected the gated call to reach Cerbos exactly once, got %d", d.calls)
	}
	if d.gotType != "notion_page" || d.gotAct != "update" {
		t.Errorf("Cerbos saw resource=%q action=%q, want notion_page/update", d.gotType, d.gotAct)
	}
}

func TestDeployedNotionMapping_UpdatePageAncestryLookupErrorFailsClosed(t *testing.T) {
	d := &stubDecider{allow: true}
	up := &fakeUpstream{err: errors.New("upstream timeout")}
	s := newNotionServer(t, d, up)
	res, err := s.CheckRequest(context.Background(),
		mcpReq("vmcp", "tools/call", toolCall("notion_notion-update-page",
			map[string]any{"page_id": "leaf0000leaf0000leaf0000leaf0000", "command": "update_content"})))
	if err != nil {
		t.Fatalf("CheckRequest: %v", err)
	}
	if !isDeny(res) {
		t.Fatalf("expected deny (fail closed) when the ancestry lookup errors, got pass")
	}
	if d.calls != 0 {
		t.Errorf("Cerbos must NOT be consulted when the gate fails closed, got %d calls", d.calls)
	}
}

// A create-pages call must NOT trip the update-page ancestry gate (different
// action/resource path) -- it's authorized entirely by Cerbos's own
// deny-create-outside-scratchpad rule (resource_notion.yaml), which needs no
// upstream lookup since parent.page_id is already in the call's own args.
func TestDeployedNotionMapping_CreatePagesDoesNotTriggerAncestryGate(t *testing.T) {
	d := &stubDecider{allow: true}            // Cerbos allow is what's under test here, not the gate
	up := &fakeUpstream{text: fetchElsewhere} // if the update-page gate fired, this would deny
	s := newNotionServer(t, d, up)
	res, err := s.CheckRequest(context.Background(),
		mcpReq("vmcp", "tools/call", toolCall("notion_notion-create-pages",
			map[string]any{
				"pages":  []any{map[string]any{"properties": map[string]any{"title": "t"}}},
				"parent": map[string]any{"page_id": "irrelevant-to-this-test"},
			})))
	if err != nil {
		t.Fatalf("CheckRequest: %v", err)
	}
	if !isPass(res) {
		t.Fatalf("expected pass: Cerbos allows and create-pages carries no `force` to mutate")
	}
	if up.calls != 0 {
		t.Errorf("create-pages must not make an ancestry lookup, got %d", up.calls)
	}
}

const testTeamFolderID = "1ccde885971080989baffe615ee5922b" // pragma: allowlist secret

const fetchUnderTeamFolder = `<page url="https://app.notion.com/p/leaf1111leaf1111leaf1111leaf1111" title="Leaf">
<ancestor-path>
<parent-page url="https://app.notion.com/p/1ccde885971080989baffe615ee5922b" title="Teamspace Home"/>
</ancestor-path>
</page>`

// TestDeployedNotionMapping_MultiParentAllowsEitherAllowedFolder proves the
// HAH multi-parent-scoping feature: a page under the SECOND allowed parent
// (not Scratchpad) still passes the gate when both are configured.
func TestDeployedNotionMapping_MultiParentAllowsEitherAllowedFolder(t *testing.T) {
	d := &stubDecider{allow: true}
	up := &fakeUpstream{text: fetchUnderTeamFolder}
	s := newNotionServerWithParents(t, d, up, []string{testScratchpadID, testTeamFolderID})
	res, err := s.CheckRequest(context.Background(),
		mcpReq("vmcp", "tools/call", toolCall("notion_notion-update-page",
			map[string]any{"page_id": "leaf1111leaf1111leaf1111leaf1111", "command": "update_content"})))
	if err != nil {
		t.Fatalf("CheckRequest: %v", err)
	}
	if !isPass(res) {
		t.Fatalf("expected pass: page is under the second allowed parent (team folder)")
	}
	if d.calls != 1 {
		t.Fatalf("expected the gated call to reach Cerbos exactly once, got %d", d.calls)
	}
}

// TestDeployedNotionMapping_MultiParentDeniesOutsideAllAllowedFolders proves a
// page under neither Scratchpad nor the team folder (e.g. Finance) is still
// denied even with two allowed parents configured.
func TestDeployedNotionMapping_MultiParentDeniesOutsideAllAllowedFolders(t *testing.T) {
	d := &stubDecider{allow: true}
	up := &fakeUpstream{text: fetchElsewhere}
	s := newNotionServerWithParents(t, d, up, []string{testScratchpadID, testTeamFolderID})
	res, err := s.CheckRequest(context.Background(),
		mcpReq("vmcp", "tools/call", toolCall("notion_notion-update-page",
			map[string]any{"page_id": "leaf0000leaf0000leaf0000leaf0000", "command": "update_content"})))
	if err != nil {
		t.Fatalf("CheckRequest: %v", err)
	}
	if !isDeny(res) {
		t.Fatalf("expected deny: page is outside both allowed parents (e.g. Finance)")
	}
	if d.calls != 0 {
		t.Errorf("Cerbos must NOT be consulted once the ancestry gate denies, got %d calls", d.calls)
	}
}

// TestDeployedNotionMapping_CreateCommentSharesTheAncestryGate proves
// create-comment is gated exactly like update-page (HAH multi-parent scoping
// extended it to cover every existing-page write, not just update-page).
func TestDeployedNotionMapping_CreateCommentSharesTheAncestryGate(t *testing.T) {
	d := &stubDecider{allow: true}
	up := &fakeUpstream{text: fetchElsewhere}
	s := newNotionServer(t, d, up)
	res, err := s.CheckRequest(context.Background(),
		mcpReq("vmcp", "tools/call", toolCall("notion_notion-create-comment",
			map[string]any{"page_id": "leaf0000leaf0000leaf0000leaf0000", "markdown": "hello"})))
	if err != nil {
		t.Fatalf("CheckRequest: %v", err)
	}
	if !isDeny(res) {
		t.Fatalf("expected deny: create-comment on a page outside Scratchpad")
	}
	if d.calls != 0 {
		t.Errorf("Cerbos must NOT be consulted once the ancestry gate denies, got %d calls", d.calls)
	}
	if up.calls != 1 {
		t.Errorf("expected exactly one notion-fetch ancestry lookup for create-comment, got %d", up.calls)
	}
}

func TestDeployedNotionMapping_CreateCommentUnderScratchpadReachesCerbos(t *testing.T) {
	d := &stubDecider{allow: true}
	up := &fakeUpstream{text: fetchUnderScratchpad}
	s := newNotionServer(t, d, up)
	res, err := s.CheckRequest(context.Background(),
		mcpReq("vmcp", "tools/call", toolCall("notion_notion-create-comment",
			map[string]any{"page_id": "leaf0000leaf0000leaf0000leaf0000", "markdown": "hello"})))
	if err != nil {
		t.Fatalf("CheckRequest: %v", err)
	}
	if !isPass(res) {
		t.Fatalf("expected pass: comment on a page under Scratchpad, Cerbos allows")
	}
	if d.gotType != "notion_page" || d.gotAct != "comment" {
		t.Errorf("Cerbos saw resource=%q action=%q, want notion_page/comment", d.gotType, d.gotAct)
	}
}
