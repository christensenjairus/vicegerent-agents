package eval

// GitHub-specific helper; self-registers via init().

import (
	"strconv"

	"github.com/google/cel-go/cel"
	"github.com/google/cel-go/common/types"
	"github.com/google/cel-go/common/types/ref"
)

func init() {
	registerHelper("githubReviewersAttr", githubReviewersAttrOption)
}

// githubReviewersAttrOption: create_pull_request's/update_pull_request's
// `reviewers` arg is a real JSON array on the wire (GitHub usernames or
// ORG/team-slug reviewers to request), not a string -- lookupCI/get() only
// match string-typed values and would silently return the "" default even
// when the call sets a non-empty array, because the type assertion inside
// lookupCI (v.(string)) fails for a Go []any and falls through with no
// error at all (the exact same silent-miss failure class the Notion
// allow_deleting_content boolean helper's comment warns about, just
// triggered by an array instead of a bool). This reads the raw `reviewers`
// value directly, type-switches for a non-empty []any (or a comma-joined
// string, in case a caller sends one instead of a real array), and
// stringifies presence/absence as hasReviewers so Cerbos's has()-guarded
// deny can fire on it exactly like every other boolean-shaped attr in this
// shim.
func githubReviewersAttrOption() []cel.EnvOption {
	return []cel.EnvOption{
		cel.Function("githubReviewersAttr",
			cel.Overload("githubReviewersAttr_map",
				[]*cel.Type{cel.MapType(cel.StringType, cel.DynType)},
				cel.MapType(cel.StringType, cel.StringType),
				cel.UnaryBinding(func(arg ref.Val) ref.Val {
					m := toAnyMap(arg)
					return types.NewStringStringMap(types.DefaultTypeAdapter, map[string]string{
						"hasReviewers": strconv.FormatBool(anyMapHasNonEmptyArrayOrString(m, "reviewers")),
					})
				}),
			),
		),
	}
}

// anyMapHasNonEmptyArrayOrString reports whether key resolves to a
// non-empty []any or a non-empty string, case-insensitively.
func anyMapHasNonEmptyArrayOrString(m map[string]any, key string) bool {
	v, ok := caseInsensitiveGet(m, key)
	if !ok {
		return false
	}
	switch val := v.(type) {
	case []any:
		return len(val) > 0
	case string:
		return val != ""
	default:
		return false
	}
}
