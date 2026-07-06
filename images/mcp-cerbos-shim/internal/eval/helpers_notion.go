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
	registerHelper("notionCreateAttr", notionCreateAttrOption)
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

// notionCreateAttrOption: create-pages' `parent` decides where the new page(s)
// land. Cerbos runs its own CEL environment (the policy YAML in defs/), which
// has none of this repo's Go helpers or string functions registered, so the
// normalization (parent id dash-stripping/lowercasing) has to happen here,
// shim-side, and be handed to Cerbos as an already-normalized string attr --
// Cerbos's policy CEL only ever does a flat `==` against the cluster-var
// (also stored dash-free/lowercase in cluster-vars.yaml).
//
// parentKind is "page_id"/"database_id"/"data_source_id" (the tool's three
// parent shapes) or "" when parent is omitted entirely -- omitting parent
// creates a standalone workspace-level private page per the tool's own docs,
// which is not under Scratchpad either, so it must deny the same as a
// mismatched/wrong-kind parent, not pass silently.
func notionCreateAttrOption() []cel.EnvOption {
	return []cel.EnvOption{
		cel.Function("notionCreateAttr",
			cel.Overload("notionCreateAttr_map",
				[]*cel.Type{cel.MapType(cel.StringType, cel.DynType)},
				cel.MapType(cel.StringType, cel.StringType),
				cel.UnaryBinding(func(arg ref.Val) ref.Val {
					m := toAnyMap(arg)
					parent := anyMapValue(m, "parent")

					kind := ""
					pageID := ""
					switch {
					case hasNonEmptyKey(parent, "page_id"):
						kind = "page_id"
						pageID = normalizeNotionID(lookupCI(parent, "page_id", ""))
					case hasNonEmptyKey(parent, "database_id"):
						kind = "database_id"
					case hasNonEmptyKey(parent, "data_source_id"):
						kind = "data_source_id"
					}

					return types.NewStringStringMap(types.DefaultTypeAdapter, map[string]string{
						"parentKind":   kind,
						"parentPageId": pageID,
					})
				}),
			),
		),
	}
}

// hasNonEmptyKey: case-insensitive presence check for a non-empty string value.
func hasNonEmptyKey(m map[string]any, key string) bool {
	return lookupCI(m, key, "") != ""
}

// normalizeNotionID strips dashes and lowercases a Notion page id -- Notion
// accepts/returns ids both dashed and undashed, and cluster-vars.yaml stores
// notionScratchpadPageId dash-free/lowercase, so this must match that shape
// for the Cerbos policy's flat string `==` to work regardless of which form
// the calling agent supplied.
func normalizeNotionID(id string) string {
	return strings.ToLower(strings.ReplaceAll(id, "-", ""))
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
