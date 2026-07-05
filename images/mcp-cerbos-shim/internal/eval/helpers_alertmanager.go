package eval

// Alertmanager-specific helper; self-registers via init().

import (
	"strconv"
	"strings"

	"github.com/google/cel-go/cel"
	"github.com/google/cel-go/common/types"
	"github.com/google/cel-go/common/types/ref"
)

func init() {
	registerHelper("alertmanagerAttr", alertmanagerAttrOption)
}

// alertmanagerAttrOption: createSilence's `duration` arg is a free-form string
// ("30m", "2h", "1d") per mcp-alertmanager's own schema, not a number Cerbos's
// CEL can compare directly. Convert it to a second count so the policy can
// enforce a numeric cap. Alertmanager's own documented default when `duration`
// is omitted is 2h — mirror that here so an omitted duration is checked
// against the cap too, rather than silently bypassing it via an absent attr.
func alertmanagerAttrOption() []cel.EnvOption {
	return []cel.EnvOption{
		cel.Function("alertmanagerAttr",
			cel.Overload("alertmanagerAttr_map",
				[]*cel.Type{cel.MapType(cel.StringType, cel.DynType)},
				cel.MapType(cel.StringType, cel.StringType),
				cel.UnaryBinding(func(arg ref.Val) ref.Val {
					m := toAnyMap(arg)
					raw := lookupCI(m, "duration", "2h")
					secs := parseAlertmanagerDuration(raw)
					return types.NewStringStringMap(types.DefaultTypeAdapter, map[string]string{
						"durationSeconds": strconv.FormatInt(secs, 10),
					})
				}),
			),
		),
	}
}

// parseAlertmanagerDuration parses "30m"/"2h"/"1d" into seconds. Anything
// unparseable (missing/garbled unit, non-numeric magnitude) returns a huge
// sentinel value rather than 0, so a malformed duration fails closed against
// the cap instead of silently passing as if duration were 0s.
func parseAlertmanagerDuration(raw string) int64 {
	s := strings.TrimSpace(raw)
	if s == "" {
		return 1 << 40
	}
	unit := s[len(s)-1:]
	numPart := s[:len(s)-1]
	n, err := strconv.ParseInt(numPart, 10, 64)
	if err != nil || n < 0 {
		return 1 << 40
	}
	switch unit {
	case "s":
		return n
	case "m":
		return n * 60
	case "h":
		return n * 3600
	case "d":
		return n * 86400
	default:
		return 1 << 40
	}
}
