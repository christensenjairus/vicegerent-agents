package eval

// Notion-specific helper; self-registers via init().

import (
	"strconv"
	"strings"

	"github.com/google/cel-go/cel"
	"github.com/google/cel-go/common/types"
	"github.com/google/cel-go/common/types/ref"
)

func init() {
	registerHelper("notionAttr", notionAttrOption)
}

// notionAttrOption: notion-update-page's `allow_deleting_content` is a real
// JSON boolean on the wire (per the tool's own schema), not a string --
// lookupCI/get() only match string-typed values and would silently return
// the "" default for a bool, letting a true value through unchecked (the
// same class of bug the PagerDuty/Linear helpers' comments warn about: a
// helper that assumes the wrong wire shape looks like it's checking
// something but never actually fires). Read it directly off the arg map
// and stringify it explicitly so Cerbos's `== "true"` comparison works
// regardless of whether the caller sent a bool, a "true"/"false" string,
// or omitted it.
func notionAttrOption() []cel.EnvOption {
	return []cel.EnvOption{
		cel.Function("notionAttr",
			cel.Overload("notionAttr_map",
				[]*cel.Type{cel.MapType(cel.StringType, cel.DynType)},
				cel.MapType(cel.StringType, cel.StringType),
				cel.UnaryBinding(func(arg ref.Val) ref.Val {
					m := toAnyMap(arg)
					return types.NewStringStringMap(types.DefaultTypeAdapter, map[string]string{
						"command":              lookupCI(m, "command", ""),
						"allowDeletingContent": strconv.FormatBool(anyMapBool(m, "allow_deleting_content")),
					})
				}),
			),
		),
	}
}

// anyMapBool: case-insensitive bool lookup; accepts a real bool or a
// "true"/"false" string, defaults to false for anything else (absent,
// wrong type, or unparseable).
func anyMapBool(m map[string]any, key string) bool {
	for k, v := range m {
		if !strings.EqualFold(k, key) {
			continue
		}
		switch val := v.(type) {
		case bool:
			return val
		case string:
			b, err := strconv.ParseBool(val)
			if err == nil {
				return b
			}
		}
	}
	return false
}
