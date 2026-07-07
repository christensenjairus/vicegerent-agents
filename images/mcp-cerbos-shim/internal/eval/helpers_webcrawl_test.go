package eval

import (
	"testing"

	config "github.com/jchristensen/vicegerent-agents/images/mcp-cerbos-shim/internal"
)

func TestWebCrawlHelperSelfRegisters(t *testing.T) {
	if _, ok := helperOptions("webCrawlAttr"); !ok {
		t.Fatal("webCrawlAttr not registered; helpers_webcrawl.go init() did not run")
	}
}

func TestUrlIsInternalTarget(t *testing.T) {
	cases := []struct {
		url  string
		want bool
	}{
		{"https://example.com/blog", false},
		{"https://docs.example.com/api/v1", false},
		{"http://localhost/", true},
		{"http://localhost:8080/", true},
		{"http://sub.localhost/", true},
		{"http://host.docker.internal:4483/mcp", true},
		{"http://169.254.169.254/latest/meta-data/", true},
		{"http://169.254.1.1/", true},
		{"http://10.0.0.5/", true},
		{"http://192.168.1.1/", true},
		{"http://172.16.0.1/", true},
		{"http://172.31.255.255/", true},
		{"http://172.32.0.1/", false}, // outside RFC1918 172.16.0.0/12
		{"http://127.0.0.1/", true},
		{"http://[::1]/", true},
		{"http://foo.svc/", true},
		{"http://foo.svc.cluster.local/", true},
		{"http://something.internal/", true},
		{"http://something.local/", true},
		{"http://8.8.8.8/", false},
		{"", false},
		{"::not a url::", true}, // unparseable fails closed
	}
	for _, c := range cases {
		got := urlIsInternalTarget(c.url)
		if got != c.want {
			t.Errorf("urlIsInternalTarget(%q) = %v, want %v", c.url, got, c.want)
		}
	}
}

func TestPatternLooksInternal(t *testing.T) {
	cases := []struct {
		pattern string
		want    bool
	}{
		{`^docs\.example\.com$`, false},
		{`^.*\.internal$`, true},
		{`169\.254\..*`, true},
		{`^localhost$`, true},
		{`^10\..*`, true},
		{`^api\.example\.com$`, false},
	}
	for _, c := range cases {
		got := patternLooksInternal(c.pattern)
		if got != c.want {
			t.Errorf("patternLooksInternal(%q) = %v, want %v", c.pattern, got, c.want)
		}
	}
}

func compileWebCrawlTestEngine(t *testing.T) *Engine {
	t.Helper()
	m := &config.Mapping{
		Backends: map[string]config.Backend{
			"vmcp": {
				DefaultAction: config.ActionAllow,
				Helpers:       []string{"webCrawlAttr"},
				Tools: map[string]config.Tool{
					"web_crawl_tool": {
						ResourceType: "web_crawl",
						Action:       "crawl",
						ID:           "get(args,'url','')",
						AttrFrom:     "webCrawlAttr(args)",
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

func TestWebCrawlAttr_ExternalURLWithinCaps(t *testing.T) {
	e := compileWebCrawlTestEngine(t)
	res, err := e.Eval(CallInput{
		Backend: "vmcp",
		Tool:    "web_crawl_tool",
		Args: map[string]any{
			"url":       "https://example.com",
			"limit":     50.0,
			"max_depth": 2.0,
		},
	})
	if err != nil {
		t.Fatalf("Eval: %v", err)
	}
	if res.Attr["isInternalTarget"] != "false" {
		t.Errorf("isInternalTarget = %q, want false", res.Attr["isInternalTarget"])
	}
	if res.Attr["limit"] != "50" {
		t.Errorf("limit = %q, want 50", res.Attr["limit"])
	}
	if res.Attr["maxDepth"] != "2" {
		t.Errorf("maxDepth = %q, want 2", res.Attr["maxDepth"])
	}
}

func TestWebCrawlAttr_InternalURLFlagged(t *testing.T) {
	e := compileWebCrawlTestEngine(t)
	res, err := e.Eval(CallInput{
		Backend: "vmcp",
		Tool:    "web_crawl_tool",
		Args: map[string]any{
			"url": "http://169.254.169.254/latest/meta-data/",
		},
	})
	if err != nil {
		t.Fatalf("Eval: %v", err)
	}
	if res.Attr["isInternalTarget"] != "true" {
		t.Errorf("isInternalTarget = %q, want true", res.Attr["isInternalTarget"])
	}
}

func TestWebCrawlAttr_SelectDomainsInternalFlagged(t *testing.T) {
	e := compileWebCrawlTestEngine(t)
	res, err := e.Eval(CallInput{
		Backend: "vmcp",
		Tool:    "web_crawl_tool",
		Args: map[string]any{
			"url":            "https://example.com",
			"select_domains": []any{`^api\.example\.com$`, `.*\.internal`},
		},
	})
	if err != nil {
		t.Fatalf("Eval: %v", err)
	}
	if res.Attr["isInternalTarget"] != "true" {
		t.Errorf("isInternalTarget = %q, want true (select_domains contains an internal pattern)", res.Attr["isInternalTarget"])
	}
}

func TestWebCrawlAttr_FirecrawlMaxDiscoveryDepthFieldName(t *testing.T) {
	e := compileWebCrawlTestEngine(t)
	res, err := e.Eval(CallInput{
		Backend: "vmcp",
		Tool:    "web_crawl_tool",
		Args: map[string]any{
			"url":               "https://example.com",
			"maxDiscoveryDepth": 5.0,
		},
	})
	if err != nil {
		t.Fatalf("Eval: %v", err)
	}
	if res.Attr["maxDepth"] != "5" {
		t.Errorf("maxDepth = %q, want 5 (from maxDiscoveryDepth)", res.Attr["maxDepth"])
	}
}

func TestWebCrawlAttr_UnsetNumericFieldsAreZero(t *testing.T) {
	e := compileWebCrawlTestEngine(t)
	res, err := e.Eval(CallInput{
		Backend: "vmcp",
		Tool:    "web_crawl_tool",
		Args: map[string]any{
			"url": "https://example.com",
		},
	})
	if err != nil {
		t.Fatalf("Eval: %v", err)
	}
	for _, k := range []string{"limit", "maxDepth", "maxBreadth"} {
		if res.Attr[k] != "0" {
			t.Errorf("%s = %q, want 0 when unset", k, res.Attr[k])
		}
	}
}

func TestWebFetchHelperSelfRegisters(t *testing.T) {
	if _, ok := helperOptions("webFetchAttr"); !ok {
		t.Fatal("webFetchAttr not registered; helpers_webcrawl.go init() did not run")
	}
}

func compileWebFetchTestEngine(t *testing.T) *Engine {
	t.Helper()
	m := &config.Mapping{
		Backends: map[string]config.Backend{
			"vmcp": {
				DefaultAction: config.ActionAllow,
				Helpers:       []string{"webFetchAttr"},
				Tools: map[string]config.Tool{
					"web_fetch_tool": {
						ResourceType: "web_crawl",
						Action:       "fetch",
						ID:           "get(args,'url','')",
						AttrFrom:     "webFetchAttr(args)",
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

func TestWebFetchAttr_ExternalURL(t *testing.T) {
	e := compileWebFetchTestEngine(t)
	res, err := e.Eval(CallInput{
		Backend: "vmcp",
		Tool:    "web_fetch_tool",
		Args:    map[string]any{"url": "https://example.com"},
	})
	if err != nil {
		t.Fatalf("Eval: %v", err)
	}
	if res.Attr["isInternalTarget"] != "false" {
		t.Errorf("isInternalTarget = %q, want false", res.Attr["isInternalTarget"])
	}
}

func TestWebFetchAttr_InternalSingleURLFlagged(t *testing.T) {
	e := compileWebFetchTestEngine(t)
	res, err := e.Eval(CallInput{
		Backend: "vmcp",
		Tool:    "web_fetch_tool",
		Args:    map[string]any{"url": "http://host.docker.internal:4483/mcp"},
	})
	if err != nil {
		t.Fatalf("Eval: %v", err)
	}
	if res.Attr["isInternalTarget"] != "true" {
		t.Errorf("isInternalTarget = %q, want true", res.Attr["isInternalTarget"])
	}
}

func TestWebFetchAttr_InternalURLInArrayFlagged(t *testing.T) {
	e := compileWebFetchTestEngine(t)
	res, err := e.Eval(CallInput{
		Backend: "vmcp",
		Tool:    "web_fetch_tool",
		Args: map[string]any{
			"urls": []any{"https://example.com", "http://169.254.169.254/latest/meta-data/"},
		},
	})
	if err != nil {
		t.Fatalf("Eval: %v", err)
	}
	if res.Attr["isInternalTarget"] != "true" {
		t.Errorf("isInternalTarget = %q, want true (one of urls[] is internal)", res.Attr["isInternalTarget"])
	}
}

func TestWebFetchAttr_AllExternalURLsInArray(t *testing.T) {
	e := compileWebFetchTestEngine(t)
	res, err := e.Eval(CallInput{
		Backend: "vmcp",
		Tool:    "web_fetch_tool",
		Args: map[string]any{
			"urls": []any{"https://example.com", "https://docs.example.com"},
		},
	})
	if err != nil {
		t.Fatalf("Eval: %v", err)
	}
	if res.Attr["isInternalTarget"] != "false" {
		t.Errorf("isInternalTarget = %q, want false", res.Attr["isInternalTarget"])
	}
}
