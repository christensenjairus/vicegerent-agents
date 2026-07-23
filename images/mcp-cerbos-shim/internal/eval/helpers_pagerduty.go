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
// [...], "status": "acknowledged"|"resolved"|null, "urgency": ..., "escalation_level":
// ..., "assignement": ...} (note: "assignement" is the upstream tool
// schema's own spelling -- see pagerduty-mcp 1.1.0's models/incidents.py).
// This is NOT PagerDuty's raw REST API body -- there is no "incidents"
// array, no "priority", no "resolution", no "incident_type", no
// "conference_bridge" field on this call at all.
//
// The operator only wants ack/resolve via this tool -- incident_ids and
// status are the only two keys manage_request should ever legitimately
// carry. This is deliberately an ALLOWLIST of that shape (deny the presence
// of any OTHER key at all) rather than a denylist of specific field names
// to avoid. Two separate incidents already proved a denylist is the wrong
// shape for this check: an earlier version of this helper assumed the
// general PagerDuty REST API shape (a batch "incidents" array of
// per-incident objects) instead of this MCP tool's actual flat schema, so
// its per-field checks against that assumed shape never ran at all --
// hasOutOfScopeChange was always "false", letting urgency/escalation_level
// through unchecked in production. Separately, pagerduty-mcp 1.1.0's tool
// description was reworded to call the reassignment field "assignment"
// while the actual wire field stayed "assignement" -- a caller trusting the
// description over the real schema needed its own denylist entry added by
// hand to stay caught. An allowlist of the two fields actually wanted needs
// no such per-field maintenance: any key that isn't incident_ids/status is
// out of scope regardless of its name, known or not -- including a future
// field this helper has never heard of.
//
// status's own VALUE still needs a dedicated check: the KEY is allowed, but
// only "acknowledged"/"resolved"/absent are legitimate values for it
// ("triggered" reopens a resolved incident).
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

					for k, v := range req {
						if strings.EqualFold(k, "incident_ids") || strings.EqualFold(k, "status") {
							continue
						}
						if !isEmptyValue(v) {
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
