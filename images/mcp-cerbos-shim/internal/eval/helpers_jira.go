package eval

// Jira-specific helper; self-registers via init().

import (
	"encoding/json"
	"strings"

	"github.com/google/cel-go/cel"
	"github.com/google/cel-go/common/types"
	"github.com/google/cel-go/common/types/ref"
)

func init() {
	registerHelper("jiraFieldsAttr", jiraFieldsAttrOption)
}

// jiraFieldsAttrOption closes two follow-up gaps on top of HAH-90's
// project-scoping widen:
//
//   - HAH-82: jira_create_issue's additional_fields and jira_update_issue's
//     fields/additional_fields are raw JSON strings, invisible to the
//     existing deny-write-outside-allowed-projects rule -- that rule only
//     inspects the top-level project_key/issue_key/epic_key args
//     mapping.yaml already captures as plain strings. But the tool's own
//     docs show additional_fields can carry {"epicKey": "OTHER-123"},
//     {"epic_link": "OTHER-123"}, or {"parent": "OTHER-456"} referencing a
//     DIFFERENT project's issue -- an actual bypass of the project-scoping
//     control, not just an unmapped extra arg (same severity class as a
//     documented security boundary being routed around via a side channel).
//   - HAH-85: assignee scoping, mirroring Linear's teamId allowlist pattern.
//     Note there is NO reporter field on either jira_create_issue or
//     jira_update_issue at all (confirmed directly against
//     docs/available-mcp-tools/jira.yaml's real argument schema) -- the
//     ticket's title mentions "assignee/reporter" but only assignee exists
//     on this tool, so reporter scoping is out of scope, not merely
//     deferred. assignee is a plain top-level arg on create, but only
//     reachable via the fields JSON string on update (fields is REQUIRED
//     there, so it's always present) -- this helper surfaces both shapes
//     as a single assignee attr.
//
// This parses BOTH raw-JSON args (additional_fields on create; fields AND
// additional_fields on update) with encoding/json (a real parse, not CEL
// string matching, which the original HAH-82 proposal correctly flagged as
// too fragile for JSON-in-a-string) and surfaces any embedded epicKey/
// epic_link/parent value as extraEpicKey/extraParentKey, so the existing
// Cerbos rule's has()-guarded prefix check can inspect it exactly like it
// already does epicKey. Malformed JSON is swallowed (empty attrs) rather
// than failing the call -- Cerbos's own deny rule only fires on a
// *populated* key, so a field the shim can't parse simply isn't checked,
// matching every other helper's fail-open-when-unverifiable posture across
// this shim -- this is a strict widening of what's checked, never a
// narrowing.
func jiraFieldsAttrOption() []cel.EnvOption {
	return []cel.EnvOption{
		cel.Function("jiraFieldsAttr",
			cel.Overload("jiraFieldsAttr_map",
				[]*cel.Type{cel.MapType(cel.StringType, cel.DynType)},
				cel.MapType(cel.StringType, cel.StringType),
				cel.UnaryBinding(func(arg ref.Val) ref.Val {
					m := toAnyMap(arg)

					extraEpicKey := ""
					extraParentKey := ""
					// Top-level assignee (create_issue's own arg).
					assignee := lookupCI(m, "assignee", "")

					for _, argName := range []string{"additional_fields", "fields"} {
						raw := lookupCI(m, argName, "")
						if raw == "" {
							continue
						}
						var parsed map[string]any
						if err := json.Unmarshal([]byte(raw), &parsed); err != nil {
							continue
						}
						if v := jsonStringField(parsed, "epicKey", "epic_link"); v != "" && extraEpicKey == "" {
							extraEpicKey = v
						}
						if v := jsonStringField(parsed, "parent"); v != "" && extraParentKey == "" {
							extraParentKey = v
						}
						// update_issue's assignee only ever arrives inside
						// `fields` (there's no top-level assignee arg on
						// that tool) -- only take it if we haven't already
						// found one from the top-level arg.
						if v := jsonStringField(parsed, "assignee"); v != "" && assignee == "" {
							assignee = v
						}
					}

					return types.NewStringStringMap(types.DefaultTypeAdapter, map[string]string{
						"extraEpicKey":   extraEpicKey,
						"extraParentKey": extraParentKey,
						"assignee":       assignee,
					})
				}),
			),
		),
	}
}

// jsonStringField reads the first present key (case-insensitive) that holds
// a plain string value; a nested-object parent form ({"key": "OTHER-123"})
// is deliberately not unwrapped here since none of the tool's documented
// examples use that shape for parent/epicKey specifically (unlike
// priority/fixVersions, which are objects/arrays and stay unchecked --
// those don't carry a project-scoping signal).
func jsonStringField(m map[string]any, keys ...string) string {
	for k, v := range m {
		for _, want := range keys {
			if strings.EqualFold(k, want) {
				if s, ok := v.(string); ok {
					return s
				}
			}
		}
	}
	return ""
}
