package eval

import (
	"reflect"
	"sort"
	"testing"

	config "github.com/jchristensen/vicegerent-agents/images/mcp-cerbos-shim/internal"
)

func TestElasticTargetsHelperSelfRegisters(t *testing.T) {
	if _, ok := helperOptions("elasticTargetsAttr"); !ok {
		t.Fatal("elasticTargetsAttr not registered; helpers_elastic.go init() did not run")
	}
}

// TestElasticTargetsAttrEval exercises elasticTargetsAttr end-to-end through
// the Engine (compile + eval + toAnyResultMap), covering every index-bearing
// arg shape the Kibana Agent Builder data-access tools use, plus the
// lowercasing and the no-target fall-through (no `targets` key -> the
// has()-guarded deny rule can't fire).
func TestElasticTargetsAttrEval(t *testing.T) {
	m := &config.Mapping{Backends: map[string]config.Backend{
		"vmcp": {
			Helpers: []string{"elasticTargetsAttr"},
			Tools: map[string]config.Tool{
				"elastic_data": {
					ResourceType: "elastic",
					ID:           "get(args,'index','*')",
					AttrFrom:     "elasticTargetsAttr(args)",
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
		want []string // nil means no "targets" key at all
	}{
		{
			name: "index arg (search / get_document_by_id / security_alerts)",
			args: map[string]any{"index": "logs-snowflake-alpha-default"},
			want: []string{"logs-snowflake-alpha-default"},
		},
		{
			name: "indices array (get_index_mapping)",
			args: map[string]any{"indices": []any{"logs-app-default", "logs-snowflake-beta-default"}},
			want: []string{"logs-app-default", "logs-snowflake-beta-default"},
		},
		{
			name: "pattern arg (list_indices)",
			args: map[string]any{"pattern": "*Snowflake*"},
			want: []string{"*snowflake*"}, // lowercased
		},
		{
			name: "indexPattern arg (index_explorer)",
			args: map[string]any{"indexPattern": "logs-*"},
			want: []string{"logs-*"},
		},
		{
			name: "name arg (platform_streams_*)",
			args: map[string]any{"name": "logs-snowflake-gamma-default"},
			want: []string{"logs-snowflake-gamma-default"},
		},
		{
			name: "query arg carries the ES|QL FROM target (execute_esql)",
			args: map[string]any{"query": "FROM logs-snowflake-beta-default | LIMIT 10"},
			want: []string{"from logs-snowflake-beta-default | limit 10"},
		},
		{
			name: "multiple arg shapes on one call all collected",
			args: map[string]any{"index": "logs-a", "name": "logs-b"},
			want: []string{"logs-a", "logs-b"},
		},
		{
			name: "no target arg falls through (no targets key)",
			args: map[string]any{"id": "doc-123"},
			want: nil,
		},
		{
			name: "empty-string target arg contributes nothing",
			args: map[string]any{"index": "  "},
			want: nil,
		},
	}

	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			res, err := eng.Eval(CallInput{Tool: "elastic_data", Backend: "vmcp", Args: c.args})
			if err != nil {
				t.Fatalf("Eval: %v", err)
			}
			v, ok := res.Attr["targets"]
			if c.want == nil {
				if ok {
					t.Fatalf("expected no targets key, got %v", v)
				}
				return
			}
			if !ok {
				t.Fatalf("expected targets key, got none; attr=%v", res.Attr)
			}
			got, ok := v.([]string)
			if !ok {
				t.Fatalf("expected []string, got %T (%v)", v, v)
			}
			sort.Strings(got)
			want := append([]string(nil), c.want...)
			sort.Strings(want)
			if !reflect.DeepEqual(got, want) {
				t.Fatalf("targets = %v, want %v", got, want)
			}
		})
	}
}
