package eval

// PagerDuty-specific helper; self-registers via init().

import (
	"strconv"
	"strings"

	"github.com/google/cel-go/cel"
	"github.com/google/cel-go/common/types"
	"github.com/google/cel-go/common/types/ref"
)

func init() {
	registerHelper("pagerdutyManageAttr", pagerdutyManageAttrOption)
}

// pagerdutyManageAttrOption: manage_incidents' single arg (manage_request) is
// this MCP tool's own flat IncidentManageRequest body -- {"incident_ids":
// [...], "status": "acknowledged"|"resolved"|null, "urgency": "high"|"low"|
// null, "escalation_level": int|null, "assignement": {...}|null} (note:
// "assignement" is the upstream tool schema's own spelling, not a typo
// introduced here). This is NOT PagerDuty's raw REST API body -- there is no
// "incidents" array, no "priority", no "resolution", no "title", no
// "incident_type", no "conference_bridge" field on this call at all. An
// earlier version of this helper assumed the general PagerDuty REST API
// shape (a batch "incidents" array of per-incident objects) instead of this
// MCP tool's actual flat schema; because the assumed "incidents" array never
// exists in a real call, that version's loop never executed and
// hasOutOfScopeChange was always "false" -- letting urgency and
// escalation_level through unchecked. This version reads the flat fields
// directly.
//
// The operator only wants ack/resolve via this tool. hasOutOfScopeChange is
// true if status is set to anything other than "acknowledged"/"resolved"
// (i.e. "triggered" -- which reopens a resolved incident), or if urgency,
// escalation_level, or assignement is present at all (any non-null/non-zero
// value counts as "set", since none of these have a legitimate ack/resolve
// use).
func pagerdutyManageAttrOption() []cel.EnvOption {
	return []cel.EnvOption{
		cel.Function("pagerdutyManageAttr",
			cel.Overload("pagerdutyManageAttr_map",
				[]*cel.Type{cel.MapType(cel.StringType, cel.DynType)},
				cel.MapType(cel.StringType, cel.StringType),
				cel.UnaryBinding(func(arg ref.Val) ref.Val {
					m := toAnyMap(arg)
					req := anyMapValue(m, "manage_request")

					outOfScope := false

					status := lookupCI(req, "status", "")
					if status != "" && status != "acknowledged" && status != "resolved" {
						outOfScope = true
					}

					for _, forbidden := range []string{
						"urgency", "escalation_level", "assignement", "assignment",
					} {
						if v, present := caseInsensitiveGet(req, forbidden); present && !isEmptyValue(v) {
							outOfScope = true
						}
					}

					return types.NewStringStringMap(types.DefaultTypeAdapter, map[string]string{
						"hasOutOfScopeChange": strconv.FormatBool(outOfScope),
					})
				}),
			),
		),
	}
}

// isEmptyValue treats nil, empty string, and zero as "not actually set" so a
// field present in the map but explicitly nulled/zeroed doesn't spuriously
// trip the deny. Any other value (including a populated struct/map for
// assignement, a positive escalation_level int, or a non-empty urgency
// string) counts as set.
func isEmptyValue(v any) bool {
	switch t := v.(type) {
	case nil:
		return true
	case string:
		return t == ""
	case int:
		return t == 0
	case int64:
		return t == 0
	case float64:
		return t == 0
	case map[string]any:
		return len(t) == 0
	}
	return false
}

// anyMapValue reads m[key] as a map[string]any, tolerating either shape CEL
// might hand back (already-native map, or a nested ref.Val map converted by
// toAnyMap upstream). Missing/wrong-typed key returns an empty map.
func anyMapValue(m map[string]any, key string) map[string]any {
	for k, v := range m {
		if !strings.EqualFold(k, key) {
			continue
		}
		if sub, ok := v.(map[string]any); ok {
			return sub
		}
	}
	return map[string]any{}
}

func caseInsensitiveGet(m map[string]any, key string) (any, bool) {
	for k, v := range m {
		if strings.EqualFold(k, key) {
			return v, true
		}
	}
	return nil, false
}
