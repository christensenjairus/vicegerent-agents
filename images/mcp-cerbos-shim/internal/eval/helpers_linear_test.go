package eval

import (
	"reflect"
	"sort"
	"testing"

	config "github.com/jchristensen/vicegerent-agents/images/mcp-cerbos-shim/internal"
)

func TestLinearProjectHelperSelfRegisters(t *testing.T) {
	if _, ok := helperOptions("linearProjectAttr"); !ok {
		t.Fatal("linearProjectAttr not registered; helpers_linear.go init() did not run")
	}
}

// TestLinearProjectAttrEval exercises linearProjectAttr end-to-end through
// the Engine (compile + eval + toAnyResultMap) since this helper is the
// first to produce a list-valued attr; the map[string]string-only helpers
// don't exercise that code path.
func TestLinearProjectAttrEval(t *testing.T) {
	m := &config.Mapping{Backends: map[string]config.Backend{
		"linear": {
			Helpers: []string{"linearProjectAttr"},
			Tools: map[string]config.Tool{
				"save_project": {
					ResourceType: "linear_team",
					ID:           "get(args,'id','*')",
					AttrFrom:     "linearProjectAttr(args)",
				},
			},
		},
	}}
	eng, err := Compile(m)
	if err != nil {
		t.Fatalf("Compile: %v", err)
	}

	cases := []struct {
		name string
		args map[string]any
		want []string // nil means no "teams" key at all
	}{
		{
			name: "addTeams only",
			args: map[string]any{"addTeams": []any{"DEVOPS"}},
			want: []string{"DEVOPS"},
		},
		{
			name: "setTeams only",
			args: map[string]any{"setTeams": []any{"some-other-team"}},
			want: []string{"some-other-team"},
		},
		{
			name: "addTeams and setTeams union",
			args: map[string]any{
				"addTeams": []any{"DEVOPS"},
				"setTeams": []any{"some-other-team"},
			},
			want: []string{"DEVOPS", "some-other-team"},
		},
		{
			name: "neither present falls through (no teams key)",
			args: map[string]any{"name": "Q3 roadmap"},
			want: nil,
		},
	}

	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			res, err := eng.Eval(CallInput{Tool: "save_project", Backend: "linear", Args: c.args})
			if err != nil {
				t.Fatalf("Eval: %v", err)
			}
			v, ok := res.Attr["teams"]
			if c.want == nil {
				if ok {
					t.Fatalf("expected no teams key, got %v", v)
				}
				return
			}
			if !ok {
				t.Fatalf("expected teams key, got none; attr=%v", res.Attr)
			}
			got, ok := v.([]string)
			if !ok {
				t.Fatalf("expected []string, got %T (%v)", v, v)
			}
			sort.Strings(got)
			want := append([]string(nil), c.want...)
			sort.Strings(want)
			if !reflect.DeepEqual(got, want) {
				t.Fatalf("teams = %v, want %v", got, want)
			}
		})
	}
}
