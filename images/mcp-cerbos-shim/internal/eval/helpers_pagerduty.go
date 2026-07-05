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
// PagerDuty's own bulk-update body — {"incidents": [{"id":..., "status":...,
// "resolution":..., "priority":..., "escalation_level":..., "assignments":...,
// "incident_type":..., "title":..., "conference_bridge":...}, ...]}. The
// operator only wants ack/resolve (+ the optional resolution note that goes
// with a resolve) — everything else PagerDuty's own docs list as settable in
// this call (priority, escalation_level, assignments, incident_type, title,
// conference_bridge) is out of scope and should deny the whole call if any
// batch entry touches it. hasOutOfScopeChange is true if ANY incident entry
// in the batch sets a status other than "acknowledged"/"resolved" (i.e.
// "triggered" — which reopens a resolved incident), OR sets any of the
// forbidden fields. Any of these on ANY entry trips the deny for the WHOLE
// call — PagerDuty applies the batch per entry, and partial policy
// enforcement (allow some entries, silently drop others) is not something
// this shim can do; deny-the-whole-call-if-any-entry-is-out-of-scope is the
// only sound option here.
func pagerdutyManageAttrOption() []cel.EnvOption {
	return []cel.EnvOption{
		cel.Function("pagerdutyManageAttr",
			cel.Overload("pagerdutyManageAttr_map",
				[]*cel.Type{cel.MapType(cel.StringType, cel.DynType)},
				cel.MapType(cel.StringType, cel.StringType),
				cel.UnaryBinding(func(arg ref.Val) ref.Val {
					m := toAnyMap(arg)
					req := anyMapValue(m, "manage_request")
					incidents := anySliceValue(req, "incidents")
					outOfScope := false
					for _, inc := range incidents {
						im, ok := inc.(map[string]any)
						if !ok {
							outOfScope = true // unparseable entry; fail closed
							continue
						}
						status := lookupCI(im, "status", "")
						if status != "" && status != "acknowledged" && status != "resolved" {
							outOfScope = true
						}
						for _, forbidden := range []string{
							"priority", "escalation_level", "assignments",
							"incident_type", "title", "conference_bridge",
						} {
							if _, present := caseInsensitiveGet(im, forbidden); present {
								outOfScope = true
							}
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

// anySliceValue reads m[key] as a []any. Missing/wrong-typed key returns nil,
// which the caller treats as no incidents to check (not out-of-scope) —
// deliberately: an empty/missing incidents list isn't a policy violation by
// itself, it just means this helper found nothing to check.
func anySliceValue(m map[string]any, key string) []any {
	for k, v := range m {
		if !strings.EqualFold(k, key) {
			continue
		}
		if s, ok := v.([]any); ok {
			return s
		}
	}
	return nil
}

func caseInsensitiveGet(m map[string]any, key string) (any, bool) {
	for k, v := range m {
		if strings.EqualFold(k, key) {
			return v, true
		}
	}
	return nil, false
}
