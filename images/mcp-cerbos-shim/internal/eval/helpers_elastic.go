package eval

// Elastic-specific helper; self-registers via init().

import (
	"strings"

	"github.com/google/cel-go/cel"
	"github.com/google/cel-go/common/types"
	"github.com/google/cel-go/common/types/ref"
)

func init() {
	registerHelper("elasticTargetsAttr", elasticTargetsAttrOption)
}

// elasticTargetsAttrOption surfaces every index/datastream a data-access
// Elastic (Kibana Agent Builder) tool call would touch as a single lowercased
// `targets` list, so resource_elastic.yaml can deny any call whose target
// matches a denied index token (${elasticDeniedIndexPatterns}) with one
// .exists() check — the same list-attr shape linearProjectAttr introduced.
//
// The tools name their target in several arg shapes, so one generic helper
// covers all of them (mapping.yaml maps only the data-access tools to it):
//   - index (string): platform_core_search, get_document_by_id, security_alerts
//   - indices ([]string): platform_core_get_index_mapping
//   - pattern (string): platform_core_list_indices
//   - indexPattern (string): platform_core_index_explorer
//   - name (string): platform_streams_* (the exact stream name)
//   - query (string): platform_core_execute_esql carries its target index
//     inside the ES|QL text itself (FROM <index>), so a substring match on the
//     query reliably catches it; search/index_explorer/query_documents carry a
//     natural-language query, included as a best-effort signal for the
//     no-index case (see resource_elastic.yaml for the residual gap this
//     cannot fully close).
//
// Every value is lowercased (Elasticsearch index names are lowercase, and a NL
// query saying "Snowflake" still matches) and only non-empty values are
// appended. A call with no target arg at all yields no `targets` key, so the
// has()-guarded deny rule simply falls through to allow — never denies on a
// missing signal, matching every other helper's fail-open-when-unverifiable
// posture across this shim.
func elasticTargetsAttrOption() []cel.EnvOption {
	return []cel.EnvOption{
		cel.Function("elasticTargetsAttr",
			cel.Overload("elasticTargetsAttr_map",
				[]*cel.Type{cel.MapType(cel.StringType, cel.DynType)},
				cel.MapType(cel.StringType, cel.DynType),
				cel.UnaryBinding(func(arg ref.Val) ref.Val {
					m := toAnyMap(arg)
					var targets []string
					add := func(s string) {
						if s = strings.ToLower(strings.TrimSpace(s)); s != "" {
							targets = append(targets, s)
						}
					}
					for _, k := range []string{"index", "pattern", "indexPattern", "name", "query"} {
						add(lookupCI(m, k, ""))
					}
					for _, v := range lookupCIStringSlice(m, "indices") {
						add(v)
					}
					if len(targets) == 0 {
						return types.NewDynamicMap(types.DefaultTypeAdapter, map[string]any{})
					}
					return types.NewDynamicMap(types.DefaultTypeAdapter, map[string]any{
						"targets": targets,
					})
				}),
			),
		),
	}
}
