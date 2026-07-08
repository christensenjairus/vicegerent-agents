package server

// Secret redaction for MCP tool-call traffic that never touches the
// egress-proxy at all. The egress-proxy (charts/egress-proxy) scrubs the
// same credential-shaped patterns from EVERY outbound request/response, but
// deliberately skips anything classified internal (host ends in
// ".cluster.local"/".svc") -- agentgateway/vMCP traffic matches that
// exclusion, since agentgateway's own Bearer auth is legitimate and must
// pass through unscrubbed. The unintended side effect: nothing scans the
// actual MCP tool-call ARGUMENT/RESULT payloads riding on that same
// connection. If an agent reads a credential-shaped string from anywhere
// (a file, a log, a prior tool result) and passes it into a Jira comment
// body, a GitHub PR description, a Linear issue, etc., it sails through
// completely untouched -- the egress-proxy's own exclusion for "trusted"
// internal traffic doesn't distinguish "agentgateway's auth header" from
// "an arbitrary string an agent chose to put in a tool-call argument".
//
// This closes that gap at the one place that sees every tool call in both
// directions regardless of destination backend: CheckRequest (before a
// call reaches vMCP) and CheckResponse (before a tool's result reaches the
// model). Reuses the exact same pattern set the egress-proxy's scrub.py
// already carries (SSH private keys, Slack tokens) so there's one canonical
// definition of "what a secret looks like" to keep in sync, plus a Bearer/
// Basic/API-key-header-shaped pattern (egress-proxy scrubs those from HTTP
// headers directly; this shim only ever sees JSON-RPC bodies, so the same
// shapes are matched as plain substrings instead).
//
// Deliberately mutate, never deny: a false-positive match on a
// legitimate-looking-but-harmless string (e.g. a Bearer-token-shaped test
// fixture, a base64 blob that happens to match a Slack token's charset)
// should not break an otherwise-valid call. This mirrors the egress-proxy's
// own posture (redact and forward, never 403 on a matched pattern alone)
// and the existing mutate() path already used for GitHub's forced-draft
// override -- redaction is just another argument rewrite. Same caveat as
// the egress-proxy's own doc comment: this is pattern-based and does NOT
// catch encoded forms (base64, hex, rot13, etc.) -- it raises the bar
// against copy-pasted plaintext secrets, not a determined exfiltration
// attempt. Cerbos-level project/team/service scoping is still the primary
// control against a MISDIRECTED call; this is a defense-in-depth layer
// against a plaintext secret riding along inside an otherwise-authorized
// one.
//
// Two layers run on every string: gitleaks' embedded default ruleset
// (github.com/zricethezav/gitleaks/v8, ~180 rules covering the broad
// universe of provider tokens/keys/connection strings) AND the hand-rolled
// secretPatternRegistry below. gitleaks is the primary net; the local
// registry is a deliberately-kept supplement for provider-specific shapes we
// want to guarantee are caught regardless of what the upstream ruleset does
// or doesn't cover, and the escape hatch for adding an arbitrary custom
// pattern the moment a new leak vector shows up (no dependency bump or
// upstream PR required). Both passes run and their counts sum; because each
// pass replaces a matched secret with the [REDACTED] placeholder before the
// next pass sees the string, a token both layers recognize is only counted
// once (the second pass finds the placeholder, not the secret).

import (
	"encoding/json"
	"log"
	"regexp"
	"strings"

	"github.com/rs/zerolog"
	"github.com/zricethezav/gitleaks/v8/detect"
)

// secretPattern is one named, independently-testable entry in the redaction
// registry. name exists so a future addition/removal/tweak can be pointed at
// directly in a test case or a log line without re-deriving "which regex is
// this" from position in a slice -- see secretPatternRegistry below for the
// add-a-new-type contract.
type secretPattern struct {
	name string
	re   *regexp.Regexp
}

// secretPatternRegistry is the list every redaction call walks. To add a new
// well-known secret type: append ONE secretPattern{name, regexp.MustCompile(...)}
// entry below and add ONE corresponding case to TestRedactString's table
// (internal/server/server_test.go) with a fake-but-shaped fixture built via
// string concatenation (see that test's own fixtures for the pattern --
// literal secret-shaped constants trip this sandbox's own commit-time
// scanner and CI's detect-secrets/detect-private-key hooks). Nothing else
// needs touching: redactString/redactValue/redactRawJSON all iterate this
// slice generically.
//
// This list is NOT exhaustive and isn't meant to be -- it's deliberately
// biased toward the credential shapes most likely to leak through an agent's
// own tool calls (API keys/tokens copied from a file, a log, or a prior tool
// result and pasted into a Jira comment, GitHub PR body, Linear issue, etc.),
// not a general-purpose secret scanner. Add an entry whenever a new leak
// vector shows up in practice; don't hold out for a "complete" list first.
//
// This whole registry is mirrored, pattern-for-pattern, in
// charts/egress-proxy/templates/addon-configmap.yaml's REDACT_PATTERNS list
// (the egress-proxy scrubs the same shapes from outbound HTTP, and pairs them
// with the same gitleaks second layer via its localhost sidecar). Keep both
// lists in sync by hand when either changes -- there is no shared source
// between the Python (mitmproxy addon) and Go (this shim) copies, since they
// run in genuinely different runtimes with no natural place to share a literal.
var secretPatternRegistry = []secretPattern{
	{
		name: "ssh_private_key",
		re: regexp.MustCompile(
			`-----BEGIN (?:RSA |EC |OPENSSH |DSA |ED25519 |ENCRYPTED )?PRIVATE KEY-----` +
				`[\s\S]+?` +
				`-----END (?:RSA |EC |OPENSSH |DSA |ED25519 |ENCRYPTED )?PRIVATE KEY-----`,
		),
	},
	{
		// Slack bot/app-level/user/refresh/socket/client tokens.
		name: "slack_bot_token",
		re:   regexp.MustCompile(`xox[bpraescd]-[A-Za-z0-9\-_]+`),
	},
	{
		name: "slack_app_token",
		re:   regexp.MustCompile(`xapp-[A-Za-z0-9\-_]+`),
	},
	{
		// Bearer/Basic auth header VALUES that ended up inside a tool-call
		// argument or result body rather than an actual HTTP Authorization
		// header (which the egress-proxy already scrubs at the transport
		// layer for external destinations). Matched as plain substrings
		// since this shim only ever sees JSON-RPC payloads, never raw
		// headers.
		name: "http_bearer_token",
		re:   regexp.MustCompile(`(?i)\bBearer\s+[A-Za-z0-9\-._~+/]+=*`),
	},
	{
		name: "http_basic_auth",
		re:   regexp.MustCompile(`(?i)\bBasic\s+[A-Za-z0-9+/]+=*`),
	},
	{
		// AWS access key IDs (IAM users, roles, EC2 instance profiles,
		// temporary STS creds all share this 20-char prefix shape). This
		// catches the identifying key ID, not the accompanying secret
		// access key -- that's a bare base64-ish string with no fixed
		// prefix and would need a much higher false-positive tolerance to
		// match generically.
		name: "aws_access_key_id",
		re:   regexp.MustCompile(`\b(?:AKIA|ASIA|AROA|AIDA)[A-Z0-9]{16}\b`),
	},
	{
		// GitHub personal access tokens (classic + fine-grained), OAuth
		// tokens, and server-to-server/user-to-server app tokens all share
		// the `gh_`-prefixed 2020+ token format.
		name: "github_token",
		re:   regexp.MustCompile(`\bgh[pousr]_[A-Za-z0-9]{36,255}\b`),
	},
	{
		// GitLab personal/project/group access tokens and CI job tokens.
		name: "gitlab_token",
		re:   regexp.MustCompile(`\bglpat-[A-Za-z0-9_\-]{20,}\b`),
	},
	{
		// Google API keys (Maps, etc. -- not OAuth client secrets, which
		// have no fixed recognizable prefix).
		name: "google_api_key",
		re:   regexp.MustCompile(`\bAIza[A-Za-z0-9_\-]{35}\b`),
	},
	{
		// OpenAI API keys (legacy sk-... and project-scoped sk-proj-...).
		name: "openai_api_key",
		re:   regexp.MustCompile(`\bsk-(?:proj-)?[A-Za-z0-9_\-]{20,}\b`),
	},
	{
		// Anthropic API keys.
		name: "anthropic_api_key",
		re:   regexp.MustCompile(`\bsk-ant-[A-Za-z0-9_\-]{20,}\b`),
	},
	{
		// Stripe live/test secret and restricted keys.
		name: "stripe_api_key",
		re:   regexp.MustCompile(`\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,}\b`),
	},
	{
		// Notion integration tokens (internal + OAuth-issued).
		name: "notion_token",
		re:   regexp.MustCompile(`\bntn_[A-Za-z0-9]{20,}\b`),
	},
	{
		// Twilio account SID-adjacent auth token / API key SIDs.
		name: "twilio_api_key",
		re:   regexp.MustCompile(`\bSK[a-f0-9]{32}\b`),
	},
	{
		// npm automation/publish tokens.
		name: "npm_token",
		re:   regexp.MustCompile(`\bnpm_[A-Za-z0-9]{36}\b`),
	},
	{
		// Generic JWT (header.payload.signature, each segment base64url).
		// Deliberately loose -- this matches ANY well-formed JWT regardless
		// of issuer, since the shape itself (not a fixed prefix) is the
		// signal. Higher false-positive risk than the prefixed patterns
		// above (a non-secret JWT, e.g. a public ID token, would also
		// match) -- acceptable given this gate only ever mutates, never
		// denies.
		name: "generic_jwt",
		re:   regexp.MustCompile(`\beyJ[A-Za-z0-9_\-]+\.eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\b`),
	},
}

const redactedPlaceholder = "[REDACTED]"

// gitleaksDetector is the single, process-wide gitleaks Detector shared across
// every redaction call. It MUST be built exactly once: NewDetectorDefaultConfig
// mutates a global viper singleton (the only data race in the whole flow), and
// construction costs ~20ms -- fine as one-time init, ruinous per-request. Once
// built, DetectString is safe for concurrent use (it accumulates into a local
// slice with no shared mutable state), which matters because redaction runs
// synchronously on every CheckRequest/CheckResponse across all backends behind
// vMCP. If construction fails, this stays nil and redactStringGitleaks degrades
// to a no-op -- the secretPatternRegistry sweep still runs, so a broken gitleaks
// init weakens coverage but never takes redaction (or the shim) down.
var gitleaksDetector *detect.Detector

func init() {
	// gitleaks logs through zerolog's global logger (e.g. a Debug line about a
	// missing .gitleaksignore). Silence it once so none of it leaks into the
	// shim's own stdlib-log container output. The shim itself logs via the
	// standard "log" package, not zerolog, so this only gags gitleaks.
	zerolog.SetGlobalLevel(zerolog.Disabled)

	d, err := detect.NewDetectorDefaultConfig()
	if err != nil {
		log.Printf("secrets_redact: gitleaks detector init failed, "+
			"falling back to the secretPatternRegistry sweep only: %v", err)
		return
	}
	gitleaksDetector = d
}

// redactString scrubs s with BOTH layers -- the hand-rolled
// secretPatternRegistry AND gitleaks' embedded default ruleset -- returning the
// scrubbed string and the total number of replacements across both passes. The
// registry sweep runs first, then gitleaks; because each pass overwrites a
// matched secret with redactedPlaceholder before the next pass sees the string,
// a token both layers recognize is counted exactly once (whichever pass reaches
// it first redacts it; the other finds only the placeholder).
func redactString(s string) (string, int) {
	total := 0
	for _, p := range secretPatternRegistry {
		var n int
		s, n = redactPattern(p.re, s)
		total += n
	}
	var gn int
	s, gn = redactStringGitleaks(s)
	total += gn
	return s, total
}

// redactStringGitleaks runs the shared gitleaks Detector over s and replaces
// every finding's secret substring with redactedPlaceholder, returning the
// scrubbed string and the replacement count. It prefers Finding.Secret (the
// exact matched credential) and falls back to Finding.Match when Secret is
// empty. A finding whose secret text is empty, is already the placeholder, or
// no longer appears in s (because an earlier finding's replacement removed a
// shared/overlapping substring) contributes nothing -- so two findings pointing
// at the same secret can't double-count.
func redactStringGitleaks(s string) (string, int) {
	if gitleaksDetector == nil {
		return s, 0
	}
	findings := gitleaksDetector.DetectString(s)
	if len(findings) == 0 {
		return s, 0
	}
	total := 0
	for _, f := range findings {
		matched := f.Secret
		if matched == "" {
			matched = f.Match
		}
		if matched == "" || matched == redactedPlaceholder {
			continue
		}
		n := strings.Count(s, matched)
		if n == 0 {
			continue
		}
		s = strings.ReplaceAll(s, matched, redactedPlaceholder)
		total += n
	}
	return s, total
}

// redactPattern is split out from redactString so the count of matches (not
// just whether ReplaceAllString changed anything) is accurate -- regexp has
// no ReplaceAllStringFunc-with-count helper, so this counts matches via
// FindAllStringIndex first.
func redactPattern(pat *regexp.Regexp, s string) (string, int) {
	matches := pat.FindAllStringIndex(s, -1)
	if len(matches) == 0 {
		return s, 0
	}
	return pat.ReplaceAllString(s, redactedPlaceholder), len(matches)
}

// redactValue walks an arbitrary JSON-decoded value (the shape
// encoding/json produces from an `any`: map[string]any, []any, string,
// float64, bool, nil) and redacts every string it finds, including strings
// nested inside maps/arrays at any depth. This also catches secrets
// embedded inside a JSON-encoded STRING value (e.g. Jira's raw
// additional_fields/fields JSON arg, or a tool result's content[].text
// that itself happens to be JSON) by attempting a nested json.Unmarshal on
// any string that parses as JSON before falling back to a flat string
// redaction -- so a secret smuggled one level of JSON-string-encoding deep
// is still caught, matching the same "don't trust a single encoding layer"
// posture jiraFieldsAttr already applies for epicKey/parent smuggling.
// Returns the (possibly rewritten) value and the total redaction count.
func redactValue(v any) (any, int) {
	switch t := v.(type) {
	case string:
		return redactStringValue(t)
	case map[string]any:
		total := 0
		out := make(map[string]any, len(t))
		for k, val := range t {
			newVal, n := redactValue(val)
			out[k] = newVal
			total += n
		}
		return out, total
	case []any:
		total := 0
		out := make([]any, len(t))
		for i, val := range t {
			newVal, n := redactValue(val)
			out[i] = newVal
			total += n
		}
		return out, total
	default:
		// number, bool, nil -- nothing to redact.
		return v, 0
	}
}

// redactStringValue handles the string case for redactValue: if the string
// itself parses as JSON (an object or array), recurse into the parsed
// structure and re-encode; otherwise apply flat pattern redaction. A
// string that merely LOOKS like JSON but fails to parse (or parses to a
// scalar) falls through to flat redaction, same as any other string.
func redactStringValue(s string) (string, int) {
	trimmed := s
	if len(trimmed) > 0 && (trimmed[0] == '{' || trimmed[0] == '[') {
		var nested any
		if err := json.Unmarshal([]byte(s), &nested); err == nil {
			if _, isMap := nested.(map[string]any); isMap {
				redacted, n := redactValue(nested)
				if n > 0 {
					if reEncoded, err := json.Marshal(redacted); err == nil {
						return string(reEncoded), n
					}
				}
				return s, 0
			}
			if _, isSlice := nested.([]any); isSlice {
				redacted, n := redactValue(nested)
				if n > 0 {
					if reEncoded, err := json.Marshal(redacted); err == nil {
						return string(reEncoded), n
					}
				}
				return s, 0
			}
		}
	}
	return redactString(s)
}

// redactArguments redacts every string value in a tool-call's arguments map,
// returning a new map (the input is not mutated in place) and the total
// redaction count.
func redactArguments(args map[string]any) (map[string]any, int) {
	redacted, n := redactValue(args)
	out, _ := redacted.(map[string]any)
	if out == nil {
		out = map[string]any{}
	}
	return out, n
}

// redactRawJSON redacts every string value found by decoding raw as JSON,
// re-encoding the result. Used for tool RESULT bytes (McpResponse's
// mcp_response field), whose top-level shape is the JSON-RPC "result"
// object, not a plain arguments map. Returns the original bytes unchanged
// (with n=0) if raw isn't valid JSON or nothing matched -- callers should
// pass raw through unmutated in that case rather than risk a matched-but-
// unparseable response subtly changing behavior.
func redactRawJSON(raw []byte) ([]byte, int) {
	var decoded any
	if err := json.Unmarshal(raw, &decoded); err != nil {
		return raw, 0
	}
	redacted, n := redactValue(decoded)
	if n == 0 {
		return raw, 0
	}
	reEncoded, err := json.Marshal(redacted)
	if err != nil {
		return raw, 0
	}
	return reEncoded, n
}
