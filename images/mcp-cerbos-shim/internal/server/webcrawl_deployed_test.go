package server

import (
	"context"
	"testing"

	"github.com/jchristensen/vicegerent-agents/images/mcp-cerbos-shim/internal/eval"
)

// These tests run the SHIPPED mapping (not a fixture) through the request path,
// using the backend name ("vmcp") and prefixed tool names ("tavily_*"/"firecrawl_*")
// exactly as ToolHive's vMCP presents them. They prove the wiring that turns a
// crawl/map tool call into the web_crawl resource Cerbos denies for internal
// targets/out-of-cap limits, that the single-URL/multi-URL
// fetch tools left unmapped by the crawl/map gate now reach the same resource via a separate
// `fetch` action, and that firecrawl_monitor_create/firecrawl_monitor_update
// reach the same resource via a separate `monitor` action; the deny *decision*
// itself is proven by defs/webcrawl_test.yaml.

func TestDeployedWebCrawlMapping_MappedToolsReachCerbos(t *testing.T) {
	m := deployedMapping(t)
	e, err := eval.Compile(m)
	if err != nil {
		t.Fatalf("compile: %v", err)
	}

	cases := []struct {
		tool string
		args map[string]any
	}{
		{"tavily_tavily_crawl", map[string]any{"url": "http://169.254.169.254/latest/meta-data/"}},
		{"tavily_tavily_map", map[string]any{"url": "http://169.254.169.254/latest/meta-data/"}},
		{"firecrawl_firecrawl_crawl", map[string]any{"url": "http://169.254.169.254/latest/meta-data/"}},
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
			if d.calls != 1 {
				t.Fatalf("expected exactly one Cerbos check, got %d", d.calls)
			}
			if d.gotType != "web_crawl" {
				t.Errorf("resourceType = %q, want web_crawl", d.gotType)
			}
			if d.gotAct != "crawl" {
				t.Errorf("action = %q, want crawl", d.gotAct)
			}
			if d.gotAttr["isInternalTarget"] != "true" {
				t.Errorf("attr.isInternalTarget = %q, want true -- the shipped mapping must surface the SSRF check", d.gotAttr["isInternalTarget"])
			}
		})
	}
}

func TestDeployedWebCrawlMapping_ExternalURLWithinCapsPasses(t *testing.T) {
	m := deployedMapping(t)
	e, err := eval.Compile(m)
	if err != nil {
		t.Fatalf("compile: %v", err)
	}
	d := &stubDecider{allow: true}
	s := New(m, e, d, Principal{ID: "hermes", Roles: []string{"agent"}})
	res, err := s.CheckRequest(context.Background(),
		mcpReq("vmcp", "tools/call", toolCall("tavily_tavily_crawl",
			map[string]any{"url": "https://example.com", "limit": 50.0, "max_depth": 2.0})))
	if err != nil {
		t.Fatalf("CheckRequest: %v", err)
	}
	if !isPass(res) {
		t.Fatalf("expected pass for an external url within caps")
	}
	if d.gotAttr["isInternalTarget"] != "false" {
		t.Errorf("attr.isInternalTarget = %q, want false", d.gotAttr["isInternalTarget"])
	}
	if d.gotAttr["limit"] != "50" {
		t.Errorf("attr.limit = %q, want 50", d.gotAttr["limit"])
	}
}

func TestDeployedWebCrawlMapping_FirecrawlMaxDiscoveryDepthWired(t *testing.T) {
	m := deployedMapping(t)
	e, err := eval.Compile(m)
	if err != nil {
		t.Fatalf("compile: %v", err)
	}
	d := &stubDecider{allow: false}
	s := New(m, e, d, Principal{ID: "hermes", Roles: []string{"agent"}})
	res, err := s.CheckRequest(context.Background(),
		mcpReq("vmcp", "tools/call", toolCall("firecrawl_firecrawl_crawl",
			map[string]any{"url": "https://example.com", "maxDiscoveryDepth": 10.0})))
	if err != nil {
		t.Fatalf("CheckRequest: %v", err)
	}
	if !isDeny(res) {
		t.Fatalf("expected deny (Cerbos denies unconditionally in this test)")
	}
	if d.gotAttr["maxDepth"] != "10" {
		t.Errorf("attr.maxDepth = %q, want 10 (from maxDiscoveryDepth) -- confirms field-name normalization is wired", d.gotAttr["maxDepth"])
	}
}

func TestDeployedWebCrawlMapping_UnmappedToolsPass(t *testing.T) {
	m := deployedMapping(t)
	e, err := eval.Compile(m)
	if err != nil {
		t.Fatalf("compile: %v", err)
	}
	// tavily_search has no crawl/discovery surface and no target-fetch surface
	// either -- must stay unmapped even given an internal-looking url.
	// tavily_extract/firecrawl_scrape were unmapped in an earlier pass; they are now
	// covered by TestDeployedWebFetchMapping_MappedToolsReachCerbos below.
	for _, tool := range []string{"tavily_tavily_search"} {
		t.Run(tool, func(t *testing.T) {
			d := &stubDecider{allow: false}
			s := New(m, e, d, Principal{ID: "hermes", Roles: []string{"agent"}})
			res, err := s.CheckRequest(context.Background(),
				mcpReq("vmcp", "tools/call", toolCall(tool,
					map[string]any{"url": "http://169.254.169.254/latest/meta-data/"})))
			if err != nil {
				t.Fatalf("CheckRequest: %v", err)
			}
			if !isPass(res) {
				t.Fatalf("expected pass for unmapped tool %q (falls through to defaultAction: allow)", tool)
			}
			if d.calls != 0 {
				t.Errorf("unmapped tool %q must not reach Cerbos, got %d calls", tool, d.calls)
			}
		})
	}
}

// TestDeployedWebFetchMapping_MappedToolsReachCerbos proves the
// single-URL/multi-URL fetch tools left unmapped by the crawl/map gate now reach Cerbos
// via the web_crawl resource's new `fetch` action.
func TestDeployedWebFetchMapping_MappedToolsReachCerbos(t *testing.T) {
	m := deployedMapping(t)
	e, err := eval.Compile(m)
	if err != nil {
		t.Fatalf("compile: %v", err)
	}

	cases := []struct {
		tool string
		args map[string]any
	}{
		{"firecrawl_firecrawl_scrape", map[string]any{"url": "http://host.docker.internal:4483/mcp"}},
		{"firecrawl_firecrawl_extract", map[string]any{"urls": []any{"http://169.254.169.254/latest/meta-data/"}}},
		{"firecrawl_firecrawl_agent", map[string]any{"urls": []any{"http://169.254.169.254/latest/meta-data/"}}},
		{"tavily_tavily_extract", map[string]any{"urls": []any{"http://169.254.169.254/latest/meta-data/"}}},
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
			if d.calls != 1 {
				t.Fatalf("expected exactly one Cerbos check, got %d", d.calls)
			}
			if d.gotType != "web_crawl" {
				t.Errorf("resourceType = %q, want web_crawl", d.gotType)
			}
			if d.gotAct != "fetch" {
				t.Errorf("action = %q, want fetch", d.gotAct)
			}
			if d.gotAttr["isInternalTarget"] != "true" {
				t.Errorf("attr.isInternalTarget = %q, want true -- the shipped mapping must surface the SSRF check", d.gotAttr["isInternalTarget"])
			}
		})
	}
}

func TestDeployedWebFetchMapping_ExternalURLPasses(t *testing.T) {
	m := deployedMapping(t)
	e, err := eval.Compile(m)
	if err != nil {
		t.Fatalf("compile: %v", err)
	}
	d := &stubDecider{allow: true}
	s := New(m, e, d, Principal{ID: "hermes", Roles: []string{"agent"}})
	res, err := s.CheckRequest(context.Background(),
		mcpReq("vmcp", "tools/call", toolCall("firecrawl_firecrawl_scrape",
			map[string]any{"url": "https://example.com"})))
	if err != nil {
		t.Fatalf("CheckRequest: %v", err)
	}
	if !isPass(res) {
		t.Fatalf("expected pass for an external url")
	}
	if d.gotAttr["isInternalTarget"] != "false" {
		t.Errorf("attr.isInternalTarget = %q, want false", d.gotAttr["isInternalTarget"])
	}
}

// TestDeployedWebMonitorMapping_MappedToolsReachCerbos proves
// firecrawl_monitor_create/firecrawl_monitor_update reach Cerbos via the
// web_crawl resource's new `monitor` action.
func TestDeployedWebMonitorMapping_MappedToolsReachCerbos(t *testing.T) {
	m := deployedMapping(t)
	e, err := eval.Compile(m)
	if err != nil {
		t.Fatalf("compile: %v", err)
	}

	cases := []struct {
		tool string
		args map[string]any
	}{
		{"firecrawl_firecrawl_monitor_create", map[string]any{"page": "http://host.docker.internal:4483/mcp"}},
		{"firecrawl_firecrawl_monitor_update", map[string]any{"id": "monitor-123", "body": map[string]any{"url": "http://169.254.169.254/latest/meta-data/"}}},
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
			if d.calls != 1 {
				t.Fatalf("expected exactly one Cerbos check, got %d", d.calls)
			}
			if d.gotType != "web_crawl" {
				t.Errorf("resourceType = %q, want web_crawl", d.gotType)
			}
			if d.gotAct != "monitor" {
				t.Errorf("action = %q, want monitor", d.gotAct)
			}
			if d.gotAttr["isInternalTarget"] != "true" {
				t.Errorf("attr.isInternalTarget = %q, want true -- the shipped mapping must surface the SSRF check", d.gotAttr["isInternalTarget"])
			}
		})
	}
}

func TestDeployedWebMonitorMapping_ExternalTargetPasses(t *testing.T) {
	m := deployedMapping(t)
	e, err := eval.Compile(m)
	if err != nil {
		t.Fatalf("compile: %v", err)
	}
	d := &stubDecider{allow: true}
	s := New(m, e, d, Principal{ID: "hermes", Roles: []string{"agent"}})
	res, err := s.CheckRequest(context.Background(),
		mcpReq("vmcp", "tools/call", toolCall("firecrawl_firecrawl_monitor_create",
			map[string]any{"page": "https://example.com"})))
	if err != nil {
		t.Fatalf("CheckRequest: %v", err)
	}
	if !isPass(res) {
		t.Fatalf("expected pass for an external target")
	}
	if d.gotAttr["isInternalTarget"] != "false" {
		t.Errorf("attr.isInternalTarget = %q, want false", d.gotAttr["isInternalTarget"])
	}
}
