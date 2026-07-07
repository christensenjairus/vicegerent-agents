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

import (
	"encoding/json"
	"regexp"
)

// secretPatterns mirrors charts/egress-proxy/templates/addon-configmap.yaml's
// REDACT_PATTERNS list. Keep both lists in sync when either changes -- there
// is currently no shared source between the Python (mitmproxy addon) and Go
// (this shim) copies, since they run in genuinely different runtimes with no
// natural place to share a literal.
var secretPatterns = []*regexp.Regexp{
	// SSH private keys -- all PEM formats including PKCS#8 encrypted variant.
	regexp.MustCompile(
		`-----BEGIN (?:RSA |EC |OPENSSH |DSA |ED25519 |ENCRYPTED )?PRIVATE KEY-----` +
			`[\s\S]+?` +
			`-----END (?:RSA |EC |OPENSSH |DSA |ED25519 |ENCRYPTED )?PRIVATE KEY-----`,
	),
	// Slack tokens -- xox* family (bot, app-level, user, refresh, socket, client)
	// and xapp-* (app-configuration tokens).
	regexp.MustCompile(`xox[bpraescd]-[A-Za-z0-9\-_]+`),
	regexp.MustCompile(`xapp-[A-Za-z0-9\-_]+`),
	// Bearer/Basic auth header VALUES that ended up inside a tool-call
	// argument or result body rather than an actual HTTP Authorization
	// header (which the egress-proxy already scrubs at the transport
	// layer for external destinations). Matched as plain substrings since
	// this shim only ever sees JSON-RPC payloads, never raw headers.
	regexp.MustCompile(`(?i)\bBearer\s+[A-Za-z0-9\-._~+/]+=*`),
	regexp.MustCompile(`(?i)\bBasic\s+[A-Za-z0-9+/]+=*`),
}

const redactedPlaceholder = "[REDACTED]"

// redactString applies every secretPatterns entry to s, returning the
// scrubbed string and how many replacements were made across all patterns.
func redactString(s string) (string, int) {
	total := 0
	for _, pat := range secretPatterns {
		var n int
		s, n = redactPattern(pat, s)
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
