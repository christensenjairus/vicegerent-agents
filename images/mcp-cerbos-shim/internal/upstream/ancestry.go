package upstream

import (
	"context"
	"encoding/json"
	"fmt"
	"regexp"
	"strings"
)

// ToolCaller is the subset of Client this package depends on, so callers
// (and tests) can substitute a stub instead of standing up an HTTP server.
type ToolCaller interface {
	CallTool(ctx context.Context, tool string, arguments map[string]any) (*CallToolResult, error)
}

// notionFetchTool is the vMCP tool name for Notion's read-only fetch --
// backend-prefixed the same way mapping.yaml keys its tools
// (notion_notion-fetch), since that's the name this call re-enters
// CheckRequest as. See client.go's package doc for the recursion-safety
// note on that tool staying unmapped in Cerbos.
const notionFetchTool = "notion_notion-fetch"

// ancestorPathRe isolates the <ancestor-path>...</ancestor-path> block from a
// notion_notion-fetch text result. Notion emits the FULL flattened ancestor
// chain to the workspace root inside this single block on one fetch of the
// leaf page (verified live against nested test pages) -- so no multi-hop
// parent walk is needed, one fetch is authoritative. A root/top-level page
// has an empty block (<ancestor-path></ancestor-path>). (?s) lets . span the
// newlines Notion puts between the child <parent-page/> tags.
var ancestorPathRe = regexp.MustCompile(`(?s)<ancestor-path>(.*?)</ancestor-path>`)

// parentPageIDRe pulls each ancestor page id out of the ancestor-path block.
// The live format is <parent-page url="https://app.notion.com/p/<ID>" .../>;
// the id is the /p/ path segment (32 hex chars, dashed or not).
var parentPageIDRe = regexp.MustCompile(`<parent-page\s+url="https://app\.notion\.com/p/([0-9a-fA-F-]+)"`)

// normalizeID strips dashes and lowercases a Notion id -- Notion accepts and
// returns ids both dashed and undashed, so both sides must be canonicalized
// before comparison.
func normalizeID(id string) string {
	return strings.ToLower(strings.ReplaceAll(id, "-", ""))
}

// PageIsUnderAncestor reports whether pageID descends from ancestorPageID in
// Notion's page tree. It makes ONE notion_notion-fetch call on pageID and
// inspects the returned <ancestor-path> block, which already carries the full
// flattened ancestor chain to root (no parent-walk loop needed).
//
// Fail-closed contract for the caller: it returns a non-nil error ONLY on an
// actual lookup failure (timeout, non-200, malformed/absent ancestor-path,
// tool-reported error) so the caller can deny on error. A genuine "not under"
// result -- an empty ancestor-path (root page) or a populated one with no
// matching id -- returns (false, nil). It returns (true, nil) only when some
// ancestor's id matches ancestorPageID.
func PageIsUnderAncestor(ctx context.Context, client ToolCaller, pageID, ancestorPageID string) (bool, error) {
	result, err := client.CallTool(ctx, notionFetchTool, map[string]any{"id": pageID})
	if err != nil {
		return false, fmt.Errorf("notion ancestry lookup for page %q: %w", pageID, err)
	}
	return pageIsUnderAncestor(extractNotionFetchText(result.Text()), ancestorPageID)
}

// notionFetchEnvelope is the outer JSON object the real notion-fetch tool
// wraps its actual <page>...</page> markdown result in: a single text
// content block whose Text is itself a JSON-encoded object with a "text"
// field carrying the markdown (see live capture in ancestry_test.go's
// realWireEnvelope fixture; the CallToolResult.Content[].Text plumbing only
// unmarshals the OUTER JSON-RPC envelope, so this second, inner JSON layer
// -- the tool's own result payload shape -- is never unwrapped upstream and
// must be decoded here before the <ancestor-path> regexes can match content
// that would otherwise still be JSON-string-escaped (a literal backslash-quote
// instead of a real quote character, or a literal two-char backslash-n instead
// of a real newline). Discovered live: the original
// unit-test fixtures used bare <page> XML directly as CallTool's return,
// which is why 147 passing tests never caught this -- see HAH-88 postmortem.
type notionFetchEnvelope struct {
	Text string `json:"text"`
}

// extractNotionFetchText unwraps the notion-fetch tool's inner JSON envelope
// to recover the actual <page>...</page> markdown text pageIsUnderAncestor's
// regexes expect. Falls back to the raw input unchanged if it isn't a JSON
// object with a "text" field (keeps existing raw-XML test fixtures and any
// non-JSON tool wired up via a stub working without modification).
func extractNotionFetchText(raw string) string {
	var env notionFetchEnvelope
	if err := json.Unmarshal([]byte(raw), &env); err != nil || env.Text == "" {
		return raw
	}
	return env.Text
}

// pageIsUnderAncestor is the pure parse+compare half, split out so tests drive
// it with literal fixture strings (and via a stub ToolCaller) rather than a
// live network round trip.
func pageIsUnderAncestor(fetchText, ancestorPageID string) (bool, error) {
	target := normalizeID(ancestorPageID)
	if target == "" {
		return false, fmt.Errorf("empty ancestor page id")
	}
	m := ancestorPathRe.FindStringSubmatch(fetchText)
	if m == nil {
		// No ancestor-path block at all is an unexpected shape (not a valid
		// root page, which still emits an empty block) -- treat as a lookup
		// failure so the caller fails closed rather than silently allowing.
		return false, fmt.Errorf("notion-fetch result has no <ancestor-path> block")
	}
	for _, pm := range parentPageIDRe.FindAllStringSubmatch(m[1], -1) {
		if normalizeID(pm[1]) == target {
			return true, nil
		}
	}
	// Empty ancestor-path (root page) or no matching ancestor -- a legitimate
	// "not under target", not an error.
	return false, nil
}
