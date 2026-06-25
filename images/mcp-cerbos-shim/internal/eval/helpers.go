package eval

import (
	"strings"

	"github.com/google/cel-go/cel"
	"github.com/google/cel-go/common/types"
	"github.com/google/cel-go/common/types/ref"
)

// helperRegistry maps a config helper name to a constructor of the CEL options
// that install it. It is populated by backend-specific helper files (e.g.
// helpers_k8s.go) via registerHelper in their init(), so the generic core never
// references any backend's semantics. To add a helper for a new MCP server, drop
// a helpers_<backend>.go that self-registers — no edits to this file.
//
// Helpers are still backend-scoped at use time: only those named in a backend's
// `helpers` list are added to that backend's CEL env (see newEnv), so a k8s
// helper can't leak into a GitHub or AWS backend mapping.
var helperRegistry = map[string]func() []cel.EnvOption{}

// registerHelper installs a backend-scoped helper under name. Called from a
// helper file's init(). A duplicate name is a programming error and panics at
// startup (fail fast, never silently shadow).
func registerHelper(name string, ctor func() []cel.EnvOption) {
	if _, dup := helperRegistry[name]; dup {
		panic("eval: duplicate helper registration: " + name)
	}
	helperRegistry[name] = ctor
}

// helperOptions returns the CEL options for a named helper. ok is false when no
// helper is registered under name, which newEnv treats as fatal (fail closed).
func helperOptions(name string) (opts []cel.EnvOption, ok bool) {
	ctor, ok := helperRegistry[name]
	if !ok {
		return nil, false
	}
	return ctor(), true
}

// getFunc registers get(map, key, default): case-insensitive key lookup over a
// string-keyed map, returning default (a string) when absent. This is how a
// mapping reads args whose casing differs across tools (kind vs Kind, Name vs name)
// without per-tool code. It is part of the generic core and always in scope.
func getFunc() cel.EnvOption {
	return cel.Function("get",
		cel.Overload("get_map_string_string",
			[]*cel.Type{cel.MapType(cel.StringType, cel.DynType), cel.StringType, cel.StringType},
			cel.StringType,
			cel.FunctionBinding(func(args ...ref.Val) ref.Val {
				m, ok := args[0].Value().(map[ref.Val]ref.Val)
				if !ok {
					// Fall back to a generic conversion for other map encodings.
					return types.String(lookupCI(toAnyMap(args[0]), str(args[1]), str(args[2])))
				}
				return types.String(lookupCIRef(m, str(args[1]), str(args[2])))
			}),
		),
	)
}

// --- shared ref.Val map helpers (generic; used by core and backend helpers) ---

func str(v ref.Val) string {
	s, _ := v.Value().(string)
	return s
}

func toAnyMap(v ref.Val) map[string]any {
	out := map[string]any{}
	native, err := v.ConvertToNative(mapStringAnyType)
	if err == nil {
		if m, ok := native.(map[string]any); ok {
			return m
		}
	}
	// Best-effort: iterate as a CEL map.
	if m, ok := v.Value().(map[ref.Val]ref.Val); ok {
		for k, val := range m {
			out[str(k)] = val.Value()
		}
	}
	return out
}

func lookupCI(m map[string]any, key, def string) string {
	for k, v := range m {
		if strings.EqualFold(k, key) {
			if s, ok := v.(string); ok {
				return s
			}
		}
	}
	return def
}

func lookupCIRef(m map[ref.Val]ref.Val, key, def string) string {
	for k, v := range m {
		if strings.EqualFold(str(k), key) {
			if s, ok := v.Value().(string); ok {
				return s
			}
		}
	}
	return def
}

func firstNonEmpty(vals ...string) string {
	for _, v := range vals {
		if strings.TrimSpace(v) != "" {
			return v
		}
	}
	return ""
}

var mapStringAnyType = mapStringAnyReflect()
