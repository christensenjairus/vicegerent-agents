package eval

// Linear-specific helper; self-registers via init().

import (
	"strings"

	"github.com/google/cel-go/cel"
	"github.com/google/cel-go/common/types"
	"github.com/google/cel-go/common/types/ref"
)

func init() {
	registerHelper("linearIssueAttr", linearIssueAttrOption)
	registerHelper("linearProjectAttr", linearProjectAttrOption)
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

// linearProjectAttrOption: save_project's addTeams/setTeams args are the only
// verifiable team signal on a project call (unlike save_issue, there's no
// single `team`/`id` shape here) -- addTeams appends to a project's team set,
// setTeams replaces it outright, and either can be omitted depending on
// whether the call is creating vs. reassigning a project. Union whichever are
// present into one `teams` list attr so Cerbos can deny if any entry isn't in
// ${linearAllowedTeams} (resource_linear.yaml); a call that sets neither arg
// has nothing to verify and gets no `teams` key at all, falling through to
// allow-all -- same fail-open-when-unverifiable pattern as linearIssueAttr.
func linearProjectAttrOption() []cel.EnvOption {
	return []cel.EnvOption{
		cel.Function("linearProjectAttr",
			cel.Overload("linearProjectAttr_map",
				[]*cel.Type{cel.MapType(cel.StringType, cel.DynType)},
				cel.MapType(cel.StringType, cel.DynType),
				cel.UnaryBinding(func(arg ref.Val) ref.Val {
					m := toAnyMap(arg)
					teams := append(lookupCIStringSlice(m, "addTeams"), lookupCIStringSlice(m, "setTeams")...)
					if len(teams) == 0 {
						return types.NewDynamicMap(types.DefaultTypeAdapter, map[string]any{})
					}
					return types.NewDynamicMap(types.DefaultTypeAdapter, map[string]any{
						"teams": teams,
					})
				}),
			),
		),
	}
}

// lookupCIStringSlice: case-insensitive lookup of a []string-shaped arg. The
// wire value decodes as []any (JSON array), so each element is coerced
// individually; a non-string element is skipped rather than failing the
// whole call (better to check what we can than to fail-open on the entire
// key over one malformed entry).
func lookupCIStringSlice(m map[string]any, key string) []string {
	for k, v := range m {
		if !strings.EqualFold(k, key) {
			continue
		}
		items, ok := v.([]any)
		if !ok {
			return nil
		}
		out := make([]string, 0, len(items))
		for _, item := range items {
			if s, ok := item.(string); ok {
				out = append(out, s)
			}
		}
		return out
	}
	return nil
}
