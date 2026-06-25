package eval

// This file is the k8s-specific helper for the reza-gholizade/k8s-mcp-server
// backend. It is self-contained: it self-registers under the name the mapping
// references (`canonicalK8s`) and pulls no Kubernetes semantics into the generic
// core. To add a helper for another MCP server, copy this file's shape — an
// init() that calls registerHelper plus the helper's own CEL function — into a
// new helpers_<backend>.go.

import (
	"strings"

	"github.com/google/cel-go/cel"
	"github.com/google/cel-go/common/types"
	"github.com/google/cel-go/common/types/ref"
)

func init() {
	registerHelper("canonicalK8s", canonicalK8sOption)
}

// canonicalK8sOption registers canonicalK8s(args) -> map(string,string).
// It reads kind/Kind case-insensitively and returns {kind, apiResource,
// namespace}. Its ONLY job is to make a Secret reference unambiguous to the
// Cerbos deny-secrets rule (which matches kind == "Secret" || apiResource ==
// "secrets"); every other kind is passed through unchanged so Cerbos allows it.
// The shim deliberately does not maintain an allowlist of readable kinds —
// agentgateway's tool-name allowlist is the gate for what an agent can call at
// all; the shim only blocks the specific instances (Secrets) Cerbos refuses.
func canonicalK8sOption() []cel.EnvOption {
	return []cel.EnvOption{
		cel.Function("canonicalK8s",
			cel.Overload("canonicalK8s_map",
				[]*cel.Type{cel.MapType(cel.StringType, cel.DynType)},
				cel.MapType(cel.StringType, cel.StringType),
				cel.UnaryBinding(func(arg ref.Val) ref.Val {
					m := toAnyMap(arg)
					raw := firstNonEmpty(
						lookupCI(m, "kind", ""),
						lookupCI(m, "Kind", ""),
					)
					kind, apiResource := canonicalizeKind(raw)
					ns := lookupCI(m, "namespace", "")
					return types.NewStringStringMap(types.DefaultTypeAdapter, map[string]string{
						"kind":        kind,
						"apiResource": apiResource,
						"namespace":   ns,
					})
				}),
			),
		),
	}
}

// canonicalizeKind detects whether a Kubernetes resource identifier refers to
// Secrets — the one kind the shipped Cerbos policy blocks — and returns the
// canonical {kind, apiResource} the policy matches on. Detection strips any
// group/version qualifier ("v1/secrets", "core/Secret") and is case- and
// plural-insensitive, so every spelling that resolves to Secrets is caught.
//
// Any non-Secret kind is passed through (trimmed + group-stripped) rather than
// matched against an allowlist: the shim's contract is "block Secrets, pass
// everything else", not "enumerate every readable kind". An empty kind is
// returned empty and forwarded as-is; the Cerbos deny-no-kind rule denies it.
func canonicalizeKind(raw string) (kind, apiResource string) {
	s := strings.TrimSpace(raw)
	if i := strings.LastIndex(s, "/"); i >= 0 {
		s = s[i+1:]
	}
	switch strings.ToLower(s) {
	case "secret", "secrets":
		return "Secret", "secrets"
	default:
		return s, strings.ToLower(s)
	}
}
