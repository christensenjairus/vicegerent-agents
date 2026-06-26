package eval

import (
	"strings"

	"github.com/google/cel-go/cel"
	"github.com/google/cel-go/common/types"
	"github.com/google/cel-go/common/types/ref"
)

// helperRegistry: backend-scoped CEL helpers registered by helpers_<backend>.go init(); core never references backend semantics.
var helperRegistry = map[string]func() []cel.EnvOption{}

// registerHelper registers a helper; duplicate name panics at startup (fail fast).
func registerHelper(name string, ctor func() []cel.EnvOption) {
	if _, dup := helperRegistry[name]; dup {
		panic("eval: duplicate helper registration: " + name)
	}
	helperRegistry[name] = ctor
}

// helperOptions returns CEL options for a named helper; unknown name is fatal in newEnv.
func helperOptions(name string) (opts []cel.EnvOption, ok bool) {
	ctor, ok := helperRegistry[name]
	if !ok {
		return nil, false
	}
	return ctor(), true
}

// getFunc: case-insensitive key lookup; handles kind vs Kind casing differences across tools.
func getFunc() cel.EnvOption {
	return cel.Function("get",
		cel.Overload("get_map_string_string",
			[]*cel.Type{cel.MapType(cel.StringType, cel.DynType), cel.StringType, cel.StringType},
			cel.StringType,
			cel.FunctionBinding(func(args ...ref.Val) ref.Val {
				m, ok := args[0].Value().(map[ref.Val]ref.Val)
				if !ok {
					return types.String(lookupCI(toAnyMap(args[0]), str(args[1]), str(args[2])))
				}
				return types.String(lookupCIRef(m, str(args[1]), str(args[2])))
			}),
		),
	)
}

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
