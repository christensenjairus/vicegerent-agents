package eval

// k8s-specific helper; self-registers via init(). Add new backends as helpers_<backend>.go with the same shape.

import (
	"strings"

	"github.com/google/cel-go/cel"
	"github.com/google/cel-go/common/types"
	"github.com/google/cel-go/common/types/ref"
)

func init() {
	registerHelper("canonicalK8s", canonicalK8sOption)
}

// canonicalK8sOption: normalize Secret references so Cerbos deny-secrets catches every spelling; pass other kinds through unchanged.
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

// canonicalizeKind: case/plural-insensitive Secret detection; non-Secrets pass through; empty triggers deny-no-kind in Cerbos.
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
