package eval

// Linear-specific helper; self-registers via init().

import (
	"github.com/google/cel-go/cel"
	"github.com/google/cel-go/common/types"
	"github.com/google/cel-go/common/types/ref"
)

func init() {
	registerHelper("linearIssueAttr", linearIssueAttrOption)
}

// linearIssueAttrOption: save_issue merges create+update behind one tool
// name. team is required on create and optional on update, so a static
// `attr: {teamId: get(args,'team',”)}` would surface an empty-but-present
// teamId on every ordinary update and trip Cerbos's has()-based deny on all
// of them. Surface teamId only when it's actually verifiable: always on
// create (no id arg), and on update only if the call itself sets team (a
// deliberate reassignment) — otherwise omit the key entirely so the call
// falls through to allow-all, matching how update_issue was left unmapped
// before Linear merged the tools.
func linearIssueAttrOption() []cel.EnvOption {
	return []cel.EnvOption{
		cel.Function("linearIssueAttr",
			cel.Overload("linearIssueAttr_map",
				[]*cel.Type{cel.MapType(cel.StringType, cel.DynType)},
				cel.MapType(cel.StringType, cel.StringType),
				cel.UnaryBinding(func(arg ref.Val) ref.Val {
					m := toAnyMap(arg)
					isUpdate := lookupCI(m, "id", "") != ""
					team := lookupCI(m, "team", "")
					if isUpdate && team == "" {
						return types.NewStringStringMap(types.DefaultTypeAdapter, map[string]string{})
					}
					return types.NewStringStringMap(types.DefaultTypeAdapter, map[string]string{
						"teamId": team,
					})
				}),
			),
		),
	}
}
