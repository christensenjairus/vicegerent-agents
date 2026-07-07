package eval

// Web-crawl-specific helper (Tavily/Firecrawl); self-registers via init().
//
// tavily_crawl/tavily_map/firecrawl_crawl accept a `url` to start
// from and, for Tavily, `select_domains` regex patterns that can widen which
// domains the crawl is allowed to follow links into. None of these tools
// touch anything this platform owns (see toolhive-servers.json's own
// description: "nothing that touches anything this platform owns"), but the
// host/mcp/README.md's own documented trust-boundary gap is exactly this
// shape of risk: host.docker.internal resolves to the host loopback for
// every container on this Mac, and a crawl tool given that host (or a
// cluster-internal *.svc.cluster.local name, a cloud metadata IP, or any
// RFC1918/loopback/link-local address) would happily fetch it and hand the
// result back to the agent -- a classic SSRF vector, made worse here because
// the crawl tools are agent-instructable, not user-typed URLs. Separately,
// crawl/map's limit/max_depth/max_breadth (max_depth spelled
// maxDiscoveryDepth on Firecrawl's tool) have no enforced upper bound, so an
// agent (or a page instructing it via prompt injection) can set limit:100000
// and exhaust the crawl budget / rack up API cost.

import (
	"net"
	"net/url"
	"strconv"
	"strings"

	"github.com/google/cel-go/cel"
	"github.com/google/cel-go/common/types"
	"github.com/google/cel-go/common/types/ref"
)

func init() {
	registerHelper("webCrawlAttr", webCrawlAttrOption)
	registerHelper("webFetchAttr", webFetchAttrOption)
	registerHelper("webMonitorAttr", webMonitorAttrOption)
}

// webCrawlAttrOption computes isInternalTarget (host-based SSRF check against
// url, and a substring check against select_domains' regex patterns) and the
// three numeric limit/depth/breadth values, tolerating the tavily vs.
// firecrawl field-name difference (max_depth vs. maxDiscoveryDepth; Firecrawl
// has no max_breadth equivalent at all, so that key is simply absent/"0" for
// firecrawl_crawl and the breadth cap never fires for it).
func webCrawlAttrOption() []cel.EnvOption {
	return []cel.EnvOption{
		cel.Function("webCrawlAttr",
			cel.Overload("webCrawlAttr_map",
				[]*cel.Type{cel.MapType(cel.StringType, cel.DynType)},
				cel.MapType(cel.StringType, cel.StringType),
				cel.UnaryBinding(func(arg ref.Val) ref.Val {
					m := toAnyMap(arg)

					rawURL := lookupCI(m, "url", "")
					internal := urlIsInternalTarget(rawURL)
					if !internal {
						for _, pat := range lookupCIStringSlice(m, "select_domains") {
							if patternLooksInternal(pat) {
								internal = true
								break
							}
						}
					}

					limit := lookupCINumber(m, "limit")
					depth := lookupCINumber(m, "max_depth")
					if depth == 0 {
						depth = lookupCINumber(m, "maxDiscoveryDepth")
					}
					breadth := lookupCINumber(m, "max_breadth")

					return types.NewStringStringMap(types.DefaultTypeAdapter, map[string]string{
						"isInternalTarget": strconv.FormatBool(internal),
						"limit":            strconv.FormatInt(limit, 10),
						"maxDepth":         strconv.FormatInt(depth, 10),
						"maxBreadth":       strconv.FormatInt(breadth, 10),
					})
				}),
			),
		),
	}
}

// webFetchAttrOption computes isInternalTarget for the single-URL/
// multi-URL fetch tools that were deliberately left unmapped by the crawl/map gate above -- firecrawl_scrape's
// `url` (a single string), and firecrawl_extract/firecrawl_agent/tavily_extract's
// `urls` (a JSON array). These tools don't discover new targets the way crawl/map
// do, but the SEED target itself is exactly as caller-controlled and exactly as
// exposed to the platform's trust boundary (host.docker.internal, RFC1918/link-local
// ranges, the cloud metadata IP, *.local/*.internal/*.svc.cluster.local names) as
// crawl/map's starting url -- reusing urlIsInternalTarget here closes that gap with
// no new detection logic, just a different set of caller args to check. A call is
// flagged internal if EITHER its single `url` OR any element of `urls` resolves
// internal.
func webFetchAttrOption() []cel.EnvOption {
	return []cel.EnvOption{
		cel.Function("webFetchAttr",
			cel.Overload("webFetchAttr_map",
				[]*cel.Type{cel.MapType(cel.StringType, cel.DynType)},
				cel.MapType(cel.StringType, cel.StringType),
				cel.UnaryBinding(func(arg ref.Val) ref.Val {
					m := toAnyMap(arg)

					internal := urlIsInternalTarget(lookupCI(m, "url", ""))
					if !internal {
						for _, u := range lookupCIStringSlice(m, "urls") {
							if urlIsInternalTarget(u) {
								internal = true
								break
							}
						}
					}

					return types.NewStringStringMap(types.DefaultTypeAdapter, map[string]string{
						"isInternalTarget": strconv.FormatBool(internal),
					})
				}),
			),
		),
	}
}

// webMonitorAttrOption computes isInternalTarget for
// firecrawl_monitor_create/firecrawl_monitor_update -- these set up a
// PERSISTENT recurring fetch (per its own scheduleText/searchWindow args),
// so an internal-only target here is worse than a one-shot single-URL/multi-URL SSRF hit:
// it's a standing probe against the platform's trust boundary, and
// monitor_create's optional webhookUrl means results can be actively pushed
// back out too. Checks the top-level `page` string and every element of the
// `pages` array (both present on monitor_create; monitor_update only ever
// carries them nested inside its opaque, freeform `body` object, which this
// also best-effort-inspects for `url`/`urls` keys since that's the shape
// monitor_create's own body arg documents "advanced requests such as crawl
// targets" using).
func webMonitorAttrOption() []cel.EnvOption {
	return []cel.EnvOption{
		cel.Function("webMonitorAttr",
			cel.Overload("webMonitorAttr_map",
				[]*cel.Type{cel.MapType(cel.StringType, cel.DynType)},
				cel.MapType(cel.StringType, cel.StringType),
				cel.UnaryBinding(func(arg ref.Val) ref.Val {
					m := toAnyMap(arg)

					internal := urlIsInternalTarget(lookupCI(m, "page", ""))
					if !internal {
						for _, u := range lookupCIStringSlice(m, "pages") {
							if urlIsInternalTarget(u) {
								internal = true
								break
							}
						}
					}
					if !internal {
						body := anyMapValue(m, "body")
						if urlIsInternalTarget(lookupCI(body, "url", "")) ||
							urlIsInternalTarget(lookupCI(body, "page", "")) {
							internal = true
						}
						if !internal {
							for _, u := range append(lookupCIStringSlice(body, "urls"), lookupCIStringSlice(body, "pages")...) {
								if urlIsInternalTarget(u) {
									internal = true
									break
								}
							}
						}
					}

					return types.NewStringStringMap(types.DefaultTypeAdapter, map[string]string{
						"isInternalTarget": strconv.FormatBool(internal),
					})
				}),
			),
		),
	}
}

// lookupCINumber reads a case-insensitive numeric field. The wire value
// decodes as float64 (JSON number) or occasionally int; anything else
// (missing key, non-numeric type) returns 0, which reads as "not set" to the
// has()-less numeric cap comparisons in resource_webcrawl.yaml (a 0 limit/
// depth/breadth never exceeds a positive cap, so an unset/absent field is
// never spuriously denied).
func lookupCINumber(m map[string]any, key string) int64 {
	v, ok := caseInsensitiveGet(m, key)
	if !ok {
		return 0
	}
	switch t := v.(type) {
	case float64:
		return int64(t)
	case int64:
		return t
	case int:
		return int64(t)
	}
	return 0
}

// urlIsInternalTarget reports whether rawURL's host resolves (by literal
// hostname or literal IP) to something inside this platform's trust boundary:
// loopback, RFC1918 private ranges, IPv6 unique-local/link-local, the cloud
// metadata IP (169.254.169.254, also covered by the broader 169.254.0.0/16
// link-local range check), *.local/*.internal/*.cluster.local names, plain
// "localhost", and host.docker.internal specifically -- the exact bypass
// host/mcp/README.md documents as reaching the vMCP from any sibling
// container on this Mac regardless of which container makes the request.
// This is a literal-hostname/literal-IP check only (no DNS resolution --
// CEL/this helper has no network access here and shouldn't gain one just to
// classify a hostname); a public-looking hostname that itself resolves to an
// internal IP at request time (DNS rebinding) is NOT caught by this check and
// is a residual gap, same class as deleteSilence's unverifiable-ownership gap
// elsewhere in this shim -- there is no signal available here to close it
// without the shim making its own DNS lookup, which introduces a
// caller-controlled network side-effect into a CEL-evaluation-time helper.
func urlIsInternalTarget(rawURL string) bool {
	if rawURL == "" {
		return false
	}
	u, err := url.Parse(rawURL)
	if err != nil {
		// Unparseable is not itself "internal", but it's not "external and
		// safe" either -- fail closed the same way parseAlertmanagerDuration
		// treats a malformed value: safer to say yes (deny) than to let a
		// deliberately-mangled url string skip the check entirely.
		return true
	}
	host := strings.ToLower(u.Hostname())
	if host == "" {
		return true
	}
	if host == "localhost" || strings.HasSuffix(host, ".localhost") {
		return true
	}
	if host == "host.docker.internal" {
		return true
	}
	if strings.HasSuffix(host, ".local") || strings.HasSuffix(host, ".internal") ||
		strings.HasSuffix(host, ".cluster.local") || strings.HasSuffix(host, ".svc") {
		return true
	}
	if ip := net.ParseIP(host); ip != nil {
		if ip.IsLoopback() || ip.IsPrivate() || ip.IsLinkLocalUnicast() ||
			ip.IsLinkLocalMulticast() || ip.IsUnspecified() {
			return true
		}
	}
	return false
}

// patternLooksInternal is a conservative substring check against a
// select_domains regex pattern string -- these are regexes, not resolvable
// hostnames, so no net.ParseIP/CIDR check applies; a plain substring match on
// the same denylist terms is the honest bound here.
func patternLooksInternal(pattern string) bool {
	// Regex patterns typically escape literal dots (\.), so strip
	// backslashes before the substring check -- otherwise "169\.254\..*"
	// would never match a plain "169.254." denylist term.
	p := strings.ToLower(strings.ReplaceAll(pattern, `\`, ""))
	for _, term := range []string{
		"localhost", "host.docker.internal", ".local", ".internal",
		".cluster.local", "169.254.", "127.0.0.1", "10.", "192.168.", "172.16.",
	} {
		if strings.Contains(p, term) {
			return true
		}
	}
	return false
}
