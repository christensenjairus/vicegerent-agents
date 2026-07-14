package server

import (
	"context"
	"testing"

	"github.com/jchristensen/vicegerent-agents/images/mcp-cerbos-shim/internal/eval"
)

// These tests run the SHIPPED mapping (not a fixture) through the request path,
// using the backend name ("vmcp") and prefixed tool names ("elastic_*") exactly
// as ToolHive's vMCP presents them. They prove the wiring that turns an Elastic
// data-access tool call into the `elastic` resource carrying a `targets` list
// Cerbos denies for a blocked index token; the deny *decision* itself is proven
// by defs/elastic_test.yaml.

func TestDeployedElasticMapping_DeniedTargetReachesCerbos(t *testing.T) {
	m := deployedMapping(t)
	e, err := eval.Compile(m)
	if err != nil {
		t.Fatalf("compile: %v", err)
	}

	cases := []struct {
		tool string
		args map[string]any
		want string // a target the helper should surface (lowercased)
	}{
		{"elastic_platform_core_search", map[string]any{"index": "logs-snowflake-alpha-default", "query": "logins"}, "logs-snowflake-alpha-default"},
		{"elastic_platform_core_get_document_by_id", map[string]any{"index": "logs-snowflake-gamma-default", "id": "abc"}, "logs-snowflake-gamma-default"},
		{"elastic_platform_core_execute_esql", map[string]any{"query": "FROM logs-snowflake-beta-default | LIMIT 10"}, "from logs-snowflake-beta-default | limit 10"},
		{"elastic_platform_streams_query_documents", map[string]any{"name": "logs-snowflake-delta-default", "query": "x"}, "logs-snowflake-delta-default"},
		{"elastic_platform_core_get_index_mapping", map[string]any{"indices": []any{"logs-snowflake-alpha-default"}}, "logs-snowflake-alpha-default"},
	}

	for _, tc := range cases {
		t.Run(tc.tool, func(t *testing.T) {
			// allow=false: the shim must forward a well-formed resource to Cerbos
			// and honor its deny (turning it into a PERMISSION_DENIED error).
			d := &stubDecider{allow: false}
			s := New(m, e, d, Principal{ID: "hermes", Roles: []string{"agent"}})
			res, err := s.CheckRequest(context.Background(),
				mcpReq("vmcp", "tools/call", toolCall(tc.tool, tc.args)))
			if err != nil {
				t.Fatalf("CheckRequest: %v", err)
			}
			if !isDeny(res) {
				t.Fatalf("expected deny when Cerbos denies, got pass")
			}
			assertNoSideEffects(t, res)
			if d.calls != 1 {
				t.Fatalf("expected exactly one Cerbos check, got %d", d.calls)
			}
			if d.gotType != "elastic" {
				t.Errorf("resourceType = %q, want elastic", d.gotType)
			}
			targets, ok := d.gotAttr["targets"].([]string)
			if !ok {
				t.Fatalf("attr.targets not a []string: %T (%v)", d.gotAttr["targets"], d.gotAttr["targets"])
			}
			found := false
			for _, tgt := range targets {
				if tgt == tc.want {
					found = true
				}
			}
			if !found {
				t.Errorf("attr.targets = %v, want to contain %q", targets, tc.want)
			}
		})
	}
}

func TestDeployedElasticMapping_NonDeniedTargetPasses(t *testing.T) {
	m := deployedMapping(t)
	e, err := eval.Compile(m)
	if err != nil {
		t.Fatalf("compile: %v", err)
	}
	// A non-blocked index: mapped, reaches Cerbos, allowed.
	d := &stubDecider{allow: true}
	s := New(m, e, d, Principal{ID: "hermes", Roles: []string{"agent"}})
	res, err := s.CheckRequest(context.Background(),
		mcpReq("vmcp", "tools/call", toolCall("elastic_platform_core_search",
			map[string]any{"index": "logs-nginx", "query": "errors"})))
	if err != nil {
		t.Fatalf("CheckRequest: %v", err)
	}
	if !isPass(res) {
		t.Fatalf("expected pass for a non-blocked index")
	}
	if d.gotType != "elastic" {
		t.Errorf("resourceType = %q, want elastic", d.gotType)
	}
	targets, _ := d.gotAttr["targets"].([]string)
	found := false
	for _, tgt := range targets {
		if tgt == "logs-nginx" {
			found = true
		}
	}
	if !found {
		t.Errorf("attr.targets = %v, want to contain logs-nginx", targets)
	}
}

func TestDeployedElasticMapping_UnmappedToolPasses(t *testing.T) {
	m := deployedMapping(t)
	e, err := eval.Compile(m)
	if err != nil {
		t.Fatalf("compile: %v", err)
	}
	// A non-data tool (product docs) names no index and is deliberately unmapped
	// -> passes without a Cerbos call. Confirms the guardrail doesn't over-block
	// the read tools that legitimately mention a data source by name.
	d := &stubDecider{allow: false}
	s := New(m, e, d, Principal{ID: "hermes", Roles: []string{"agent"}})
	res, err := s.CheckRequest(context.Background(),
		mcpReq("vmcp", "tools/call", toolCall("elastic_platform_core_product_documentation",
			map[string]any{"query": "how does the snowflake integration work"})))
	if err != nil {
		t.Fatalf("CheckRequest: %v", err)
	}
	if !isPass(res) {
		t.Fatalf("expected pass for an unmapped elastic tool")
	}
	if d.calls != 0 {
		t.Errorf("unmapped tool must not call Cerbos, got %d calls", d.calls)
	}
}
