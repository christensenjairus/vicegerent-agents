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

// parentPageIDRe pulls each ancestor id out of the ancestor-path block.
// Notion's tag name is NOT always <parent-page> -- that's only the immediate
// parent. Ancestors further up the chain are tagged <ancestor-2-page>,
// <ancestor-3-page>, etc. (verified live: a page 3 levels under "Teamspace
// Home" showed <parent-page>, <ancestor-2-page>, <ancestor-3-page url=
// ".../Teamspace Home">, in that order, nearest-first). The tag name element
// itself varies (parent-page|ancestor-N-page|parent-database|ancestor-N-database);
// only the "-page"/"-database" suffix and the
// url="https://app.notion.com/p/<ID>" attribute are stable, so match on
// those rather than hardcoding "parent-page" -- the original regex silently
// missed every ancestor beyond the immediate parent, a false-negative deny
// discovered live testing HAH's multi-parent scoping against a real
// multi-level nested team folder. The -database variant was added after a
// second false-negative deny discovered live testing the work-cluster
// notionAllowedParentPageIds rollout (MR !390): a page whose immediate
// parent is a wiki DATABASE (not a plain page -- e.g. DevSecOps/DevOps WIP,
// both wiki databases) is tagged <parent-database>, which the original
// page-only regex never matched, denying writes to pages under an
// allowlisted database folder even though the database's own id was
// correctly configured. Operator confirmed (2026-07-06) that a database
// parent should be treated the same as a page parent for this ancestry
// check -- there is no policy reason to distinguish them here.
var parentPageIDRe = regexp.MustCompile(`<(?:parent-page|ancestor-\d+-page|parent-database|ancestor-\d+-database)\s+url="https://app\.notion\.com/p/([0-9a-fA-F-]+)"`)

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
	return PageIsUnderAnyAncestor(ctx, client, pageID, []string{ancestorPageID})
}

// PageIsUnderAnyAncestor reports whether pageID descends from ANY of
// ancestorPageIDs (a caller-scoped allowlist of parent folders, e.g. the
// Scratchpad page plus a set of team-folder pages -- HAH's multi-parent
// scoping). Same single-fetch/fail-closed contract as PageIsUnderAncestor;
// this just checks the flattened ancestor chain against a set instead of one
// id. An empty ancestorPageIDs is a caller bug (misconfiguration, not "no
// restriction") and errors rather than silently allowing everything.
func PageIsUnderAnyAncestor(ctx context.Context, client ToolCaller, pageID string, ancestorPageIDs []string) (bool, error) {
	if len(ancestorPageIDs) == 0 {
		return false, fmt.Errorf("no allowed ancestor page ids configured")
	}
	result, err := client.CallTool(ctx, notionFetchTool, map[string]any{"id": pageID})
	if err != nil {
		return false, fmt.Errorf("notion ancestry lookup for page %q: %w", pageID, err)
	}
	return pageIsUnderAnyAncestor(extractNotionFetchText(result.Text()), ancestorPageIDs)
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
	return pageIsUnderAnyAncestor(fetchText, []string{ancestorPageID})
}

// pageIsUnderAnyAncestor is pageIsUnderAncestor generalized to a set of
// allowed ancestors -- true if any one of them appears in the page's
// flattened ancestor chain.
func pageIsUnderAnyAncestor(fetchText string, ancestorPageIDs []string) (bool, error) {
	targets := make(map[string]struct{}, len(ancestorPageIDs))
	for _, id := range ancestorPageIDs {
		if norm := normalizeID(id); norm != "" {
			targets[norm] = struct{}{}
		}
	}
	if len(targets) == 0 {
		return false, fmt.Errorf("no non-empty ancestor page ids to check against")
	}
	m := ancestorPathRe.FindStringSubmatch(fetchText)
	if m == nil {
		// No ancestor-path block at all is an unexpected shape (not a valid
		// root page, which still emits an empty block) -- treat as a lookup
		// failure so the caller fails closed rather than silently allowing.
		return false, fmt.Errorf("notion-fetch result has no <ancestor-path> block")
	}
	for _, pm := range parentPageIDRe.FindAllStringSubmatch(m[1], -1) {
		if _, ok := targets[normalizeID(pm[1])]; ok {
			return true, nil
		}
	}
	// Empty ancestor-path (root page) or no matching ancestor -- a legitimate
	// "not under any allowed target", not an error.
	return false, nil
}
