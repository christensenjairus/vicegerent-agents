package upstream

import (
	"context"
	"errors"
	"testing"
)

// fakeCaller is a ToolCaller stub: it returns a canned notion-fetch text (or
// an error) without any network, so ancestry parsing is tested against literal
// fixtures. It also records the tool/args it was called with.
type fakeCaller struct {
	text      string
	err       error
	gotTool   string
	gotArgs   map[string]any
	callCount int
}

func (f *fakeCaller) CallTool(_ context.Context, tool string, args map[string]any) (*CallToolResult, error) {
	f.callCount++
	f.gotTool = tool
	f.gotArgs = args
	if f.err != nil {
		return nil, f.err
	}
	return &CallToolResult{Content: []struct {
		Type string `json:"type"`
		Text string `json:"text"`
	}{{Type: "text", Text: f.text}}}, nil
}

const scratchpadID = "393de8859710809c9f5ec57a91d2c81a" // pragma: allowlist secret

// realFormatUnderScratchpad matches the live notion-fetch shape: an XML-ish
// <page> wrapper whose <ancestor-path> carries the full flattened chain to
// root. Here the deep page's only ancestor is the Scratchpad page itself
// (Notion flattens intermediate pages, so even a deeply nested page shows the
// top-level ancestor directly).
const realFormatUnderScratchpad = `<page url="https://app.notion.com/p/aaaa1111bbbb2222cccc3333dddd4444" title="Deep Notes">
<ancestor-path>
<parent-page url="https://app.notion.com/p/393de8859710809c9f5ec57a91d2c81a" title="Scratchpad"/>
</ancestor-path>
<content>
some body text
</content>
</page>`

// multipleAncestors shows more than one ancestor in the chain; the match is
// the second entry, proving all entries (not just the first) are scanned.
const multipleAncestors = `<page url="https://app.notion.com/p/leaf0000leaf0000leaf0000leaf0000" title="Leaf">
<ancestor-path>
<parent-page url="https://app.notion.com/p/ffff9999ffff9999ffff9999ffff9999" title="Some Project"/>
<parent-page url="https://app.notion.com/p/393de8859710809c9f5ec57a91d2c81a" title="Scratchpad"/>
</ancestor-path>
</page>`

// rootPageEmptyAncestry is a top-level/root page: an empty ancestor-path.
const rootPageEmptyAncestry = `<page url="https://app.notion.com/p/393de8859710809c9f5ec57a91d2c81a" title="Scratchpad">
<ancestor-path></ancestor-path>
<content>root</content>
</page>`

// underDifferentTree is a valid page whose ancestors don't include Scratchpad.
const underDifferentTree = `<page url="https://app.notion.com/p/1234abcd1234abcd1234abcd1234abcd" title="Elsewhere">
<ancestor-path>
<parent-page url="https://app.notion.com/p/9999000099990000999900009999abcd" title="Other Folder"/>
</ancestor-path>
</page>`

func TestPageIsUnderAncestor(t *testing.T) {
	cases := []struct {
		name    string
		text    string
		callErr error
		want    bool
		wantErr bool
	}{
		{name: "real format under scratchpad", text: realFormatUnderScratchpad, want: true},
		{name: "match is not the first ancestor", text: multipleAncestors, want: true},
		{name: "root page empty ancestry is not under scratchpad", text: rootPageEmptyAncestry, want: false},
		{name: "populated but no matching ancestor", text: underDifferentTree, want: false},
		{name: "lookup error fails closed", callErr: errors.New("boom"), wantErr: true},
		{name: "malformed result with no ancestor-path block errors", text: "just some text, no tags", wantErr: true},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			f := &fakeCaller{text: tc.text, err: tc.callErr}
			got, err := PageIsUnderAncestor(context.Background(), f, "leaf-page-id", scratchpadID)
			if tc.wantErr {
				if err == nil {
					t.Fatalf("expected error, got nil (result=%v)", got)
				}
				return
			}
			if err != nil {
				t.Fatalf("unexpected error: %v", err)
			}
			if got != tc.want {
				t.Errorf("PageIsUnderAncestor = %v, want %v", got, tc.want)
			}
		})
	}
}

// TestPageIsUnderAncestor_CallsFetchOnce guards the "single fetch is
// authoritative" invariant: exactly one notion_notion-fetch call, on the leaf
// page id, no parent-walk loop.
func TestPageIsUnderAncestor_CallsFetchOnce(t *testing.T) {
	f := &fakeCaller{text: realFormatUnderScratchpad}
	if _, err := PageIsUnderAncestor(context.Background(), f, "leaf-page-id", scratchpadID); err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if f.callCount != 1 {
		t.Errorf("expected exactly 1 fetch, got %d", f.callCount)
	}
	if f.gotTool != notionFetchTool {
		t.Errorf("fetched tool = %q, want %q", f.gotTool, notionFetchTool)
	}
	if f.gotArgs["id"] != "leaf-page-id" {
		t.Errorf("fetch args id = %v, want leaf-page-id", f.gotArgs["id"])
	}
}

// TestPageIsUnderAncestor_NormalizesDashes proves a dashed ancestor id from the
// caller matches an undashed id in the fetched ancestor-path (and vice versa).
func TestPageIsUnderAncestor_NormalizesDashes(t *testing.T) {
	dashedTarget := "393de885-9710-809c-9f5e-c57a91d2c81a" // same id as scratchpadID, dashed
	f := &fakeCaller{text: realFormatUnderScratchpad}
	got, err := PageIsUnderAncestor(context.Background(), f, "leaf", dashedTarget)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if !got {
		t.Errorf("expected dashed target to match undashed ancestor id")
	}
}

// TestPageIsUnderAncestor_EmptyTargetErrors: an empty ancestor id is a
// misconfiguration, not a valid "match nothing"; fail closed.
func TestPageIsUnderAncestor_EmptyTargetErrors(t *testing.T) {
	f := &fakeCaller{text: realFormatUnderScratchpad}
	if _, err := PageIsUnderAncestor(context.Background(), f, "leaf", ""); err == nil {
		t.Fatal("expected error for empty ancestor page id")
	}
}
