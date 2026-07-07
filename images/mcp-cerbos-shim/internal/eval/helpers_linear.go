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
	registerHelper("linearLabelAttr", linearLabelAttrOption)
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
//
// isCreate + assignee (added for the force-self-assignee guardrail): unlike
// team, assignee is surfaced on EVERY call that carries it, create or
// update, since a deliberate reassignment on update should be checked just
// as much as an initial assignment on create. isCreate additionally lets
// Cerbos deny a create call that OMITS assignee entirely (every new issue
// must be self-assigned), without also denying an ordinary update that
// doesn't touch assignee at all -- an update with no assignee arg gets an
// empty assignee attr AND isCreate="false", so the create-only deny rule
// never fires for it.
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

					out := map[string]string{
						"isCreate": boolStr(!isUpdate),
					}
					// teamId: only include the key when verifiable (create,
					// or update with an explicit reassignment) -- has() in
					// Cerbos checks key PRESENCE, not value truthiness, so an
					// empty-but-present key would wrongly trip the existing
					// deny-non-devops-team rule on every ordinary update.
					if !isUpdate || team != "" {
						out["teamId"] = team
					}
					// assignee: include only when the call actually carries
					// one, same has()-presence reasoning -- an ordinary
					// update that doesn't touch assignee must get no
					// assignee key at all, not an empty one.
					if assignee := lookupCI(m, "assignee", ""); assignee != "" {
						out["assignee"] = assignee
					}
					return types.NewStringStringMap(types.DefaultTypeAdapter, out)
				}),
			),
		),
	}
}

// boolStr renders a bool as Cerbos-CEL-friendly "true"/"false" string, since
// this helper's overload returns map[string]string (mixing string/bool
// values in one native Go map isn't representable via NewStringStringMap).
func boolStr(b bool) string {
	if b {
		return "true"
	}
	return "false"
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

// linearLabelAttrOption: create_issue_label's teamId arg is optional --
// omitted entirely for a workspace-scoped label, set for a team-scoped one
// (HAH-91). A plain `attr: {teamId: get(args,'teamId',”)}` in mapping.yaml
// would put an empty-but-present teamId key on every workspace label
// (teamId omitted) and Cerbos's has()-based deny-non-devops-team check would
// trip on all of them, same failure mode linearIssueAttr's doc comment
// already warns about for save_issue. Surface teamId only when the call
// actually carries a non-empty value, same has()-presence reasoning as
// linearIssueAttr/linearProjectAttr.
func linearLabelAttrOption() []cel.EnvOption {
	return []cel.EnvOption{
		cel.Function("linearLabelAttr",
			cel.Overload("linearLabelAttr_map",
				[]*cel.Type{cel.MapType(cel.StringType, cel.DynType)},
				cel.MapType(cel.StringType, cel.StringType),
				cel.UnaryBinding(func(arg ref.Val) ref.Val {
					m := toAnyMap(arg)
					out := map[string]string{}
					if teamID := lookupCI(m, "teamId", ""); teamID != "" {
						out["teamId"] = teamID
					}
					return types.NewStringStringMap(types.DefaultTypeAdapter, out)
				}),
			),
		),
	}
}
