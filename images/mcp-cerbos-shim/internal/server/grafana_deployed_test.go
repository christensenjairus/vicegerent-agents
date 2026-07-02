package server

import (
	"context"
	"os"
	"path/filepath"
	"testing"

	config "github.com/jchristensen/vicegerent-agents/images/mcp-cerbos-shim/internal"
	"github.com/jchristensen/vicegerent-agents/images/mcp-cerbos-shim/internal/eval"
)

// These tests run the SHIPPED mapping (not a fixture) through the request path,
// using the backend name ("vmcp") and prefixed tool names ("grafana_*") exactly
// as ToolHive's vMCP presents them. They prove the wiring that turns a Grafana
// tool call into the grafana_datasource resource Cerbos denies for OpenSearch;
// the deny *decision* itself is proven by defs/grafana_test.yaml.

func deployedMapping(t *testing.T) *config.Mapping {
	t.Helper()
	p := filepath.Join("..", "..", "..", "..", "infrastructure", "controllers", "mcp-cerbos-shim", "mapping.yaml")
	if _, err := os.Stat(p); err != nil {
		t.Skipf("deployed mapping not reachable from test dir: %v", err)
	}
	m, err := config.Load(p)
	if err != nil {
		t.Fatalf("load deployed mapping: %v", err)
	}
	return m
}

func TestDeployedGrafanaMapping_OpenSearchReachesCerbos(t *testing.T) {
	m := deployedMapping(t)
	e, err := eval.Compile(m)
	if err != nil {
		t.Fatalf("compile: %v", err)
	}

	const osUID = "fess5o6x6evb4b"
	const osName = "dev-opensearch-datasource"

	cases := []struct {
		tool     string
		args     map[string]any
		wantUID  string
		wantName string
	}{
		{"grafana_query_prometheus", map[string]any{"datasourceUid": osUID, "expr": "up"}, osUID, ""},
		{"grafana_query_prometheus_histogram", map[string]any{"datasourceUid": osUID}, osUID, ""},
		{"grafana_list_prometheus_label_names", map[string]any{"datasourceUid": osUID}, osUID, ""},
		{"grafana_list_prometheus_metric_names", map[string]any{"datasourceUid": osUID}, osUID, ""},
		{"grafana_get_datasource", map[string]any{"uid": osUID}, osUID, ""},
		{"grafana_get_datasource_by_uid", map[string]any{"uid": osUID}, osUID, ""},
		{"grafana_get_datasource_by_name", map[string]any{"name": osName}, "", osName},
		{"grafana_check_datasources_health", map[string]any{"uid": osUID}, osUID, ""},
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
			if d.gotType != "grafana_datasource" {
				t.Errorf("resourceType = %q, want grafana_datasource", d.gotType)
			}
			if d.gotAct != "read" {
				t.Errorf("action = %q, want read", d.gotAct)
			}
			if d.gotAttr["uid"] != tc.wantUID {
				t.Errorf("attr.uid = %q, want %q", d.gotAttr["uid"], tc.wantUID)
			}
			if d.gotAttr["name"] != tc.wantName {
				t.Errorf("attr.name = %q, want %q", d.gotAttr["name"], tc.wantName)
			}
		})
	}
}

func TestDeployedGrafanaMapping_NonOpenSearchPasses(t *testing.T) {
	m := deployedMapping(t)
	e, err := eval.Compile(m)
	if err != nil {
		t.Fatalf("compile: %v", err)
	}
	// A prometheus datasource uid: mapped, reaches Cerbos, allowed.
	d := &stubDecider{allow: true}
	s := New(m, e, d, Principal{ID: "hermes", Roles: []string{"agent"}})
	res, err := s.CheckRequest(context.Background(),
		mcpReq("vmcp", "tools/call", toolCall("grafana_query_prometheus",
			map[string]any{"datasourceUid": "prom-abc123", "expr": "up"})))
	if err != nil {
		t.Fatalf("CheckRequest: %v", err)
	}
	if !isPass(res) {
		t.Fatalf("expected pass for a non-opensearch datasource")
	}
	if d.gotAttr["uid"] != "prom-abc123" {
		t.Errorf("attr.uid = %q, want prom-abc123", d.gotAttr["uid"])
	}
}

func TestDeployedGrafanaMapping_UnmappedGrafanaToolPasses(t *testing.T) {
	m := deployedMapping(t)
	e, err := eval.Compile(m)
	if err != nil {
		t.Fatalf("compile: %v", err)
	}
	// A scoped read tool that names no datasource is unmapped -> passes without
	// a Cerbos call. Confirms the guardrail doesn't over-block the allowed tools.
	d := &stubDecider{allow: false}
	s := New(m, e, d, Principal{ID: "hermes", Roles: []string{"agent"}})
	res, err := s.CheckRequest(context.Background(),
		mcpReq("vmcp", "tools/call", toolCall("grafana_search_dashboards",
			map[string]any{"query": "prod"})))
	if err != nil {
		t.Fatalf("CheckRequest: %v", err)
	}
	if !isPass(res) {
		t.Fatalf("expected pass for an unmapped grafana tool")
	}
	if d.calls != 0 {
		t.Errorf("unmapped tool must not call Cerbos, got %d calls", d.calls)
	}
}
