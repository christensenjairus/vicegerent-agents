package eval

import "reflect"

// mapStringAnyReflect returns the reflect.Type for map[string]any, used to coax
// CEL map values into a native Go map for case-insensitive key handling.
func mapStringAnyReflect() reflect.Type {
	return reflect.TypeOf(map[string]any{})
}
