package server

import (
	"context"
	"encoding/json"
	"fmt"
	"strings"
	"testing"

	config "github.com/jchristensen/vicegerent-agents/images/mcp-cerbos-shim/internal"
	"github.com/jchristensen/vicegerent-agents/images/mcp-cerbos-shim/internal/eval"
	pb "github.com/jchristensen/vicegerent-agents/images/mcp-cerbos-shim/proto/gen"
)

// stubDecider lets tests assert exactly what resource/action the connector
// would send to Cerbos, and force allow/deny/error verdicts.
type stubDecider struct {
	allow   bool
	reason  string // policy-authored deny output; "" exercises the denyMessage fallback
	err     error
	gotType string
	gotID   string
	gotAttr map[string]any
	gotAct  string
	calls   int
}

func (s *stubDecider) IsAllowed(_ context.Context, _ string, _ []string,
	resourceType, resourceID string, attr map[string]any, action string) (bool, string, error) {
	s.calls++
	s.gotType, s.gotID, s.gotAttr, s.gotAct = resourceType, resourceID, attr, action
	// Mirror the real Cerbos PDP, which rejects an empty resource.id with
	// InvalidArgument before evaluating policy. Without this, the mock hides
	// the empty-id bug that broke listResources in-cluster.
	if resourceID == "" {
		return false, "", fmt.Errorf("validation error: resources[0].resource.id: value is required")
	}
	return s.allow, s.reason, s.err
}

// testMapping mirrors the design's k8s backend (deny-default) plus a permissive
// second backend, exercising the canonicalK8s helper and case-insensitive args.
const testMappingYAML = `
backends:
  kubernetes:
    defaultAction: allow
    helpers: [canonicalK8s]
    tools:
      getResource:
        resourceType: k8s_resource
        action: getResource
        id: "get(args,'name','')"
        attrFrom: "canonicalK8s(args)"
      listResources:
        resourceType: k8s_resource
        action: listResources
        id: "''"
        attrFrom: "canonicalK8s(args)"
      describeResource:
        resourceType: k8s_resource
        action: describeResource
        id: "get(args,'name','')"
        attrFrom: "canonicalK8s(args)"
      getPodsLogs:
        resourceType: k8s_resource
        action: getPodsLogs
        id: "get(args,'Name','')"
        attr: { namespace: "get(args,'namespace','')" }
  github:
    defaultAction: allow
    tools:
      create_issue:
        resourceType: gh_action
        action: create_issue
        id: "get(args,'repo','')"
        attr: { repo: "get(args,'repo','')" }
      open_pr:
        resourceType: gh_action
        action: open_pr
        id: "get(args,'repo','')"
        attr: { repo: "get(args,'repo','')" }
        force: { draft: true }
      force_nested:
        resourceType: gh_action
        action: force_nested
        id: "get(args,'repo','')"
        attr: { repo: "get(args,'repo','')" }
        force:
          parent: { type: page_id, page_id: scratchpadid }
`

func newTestServer(t *testing.T, d *stubDecider) *Server {
	t.Helper()
	m, err := config.Parse([]byte(testMappingYAML))
	if err != nil {
		t.Fatalf("parse mapping: %v", err)
	}
	e, err := eval.Compile(m)
	if err != nil {
		t.Fatalf("compile: %v", err)
	}
	return New(m, e, d, Principal{ID: "hermes", Roles: []string{"agent"}})
}

func toolCall(name string, args map[string]any) []byte {
	b, _ := json.Marshal(map[string]any{"name": name, "arguments": args})
	return b
}

// optimizerCall builds the vMCP optimizer's call_tool wrapper around a real
// invocation: {"name":"call_tool","arguments":{"tool_name":...,"parameters":...}}.
func optimizerCall(toolName string, params map[string]any) []byte {
	b, _ := json.Marshal(map[string]any{
		"name":      callToolMeta,
		"arguments": map[string]any{"tool_name": toolName, "parameters": params},
	})
	return b
}

func mcpReq(backend, method string, body []byte) *pb.McpRequest {
	return &pb.McpRequest{ServiceNames: []string{backend}, Method: method, McpRequest: body}
}

func isPass(r *pb.McpRequestResult) bool    { return r.GetPass() != nil }
func isDeny(r *pb.McpRequestResult) bool    { return r.GetError() != nil }
func isMutated(r *pb.McpRequestResult) bool { return r.GetMutated() != nil }

// decodeMutated parses a Mutated result's replacement params back into a
// name/arguments pair for assertions.
func decodeMutated(t *testing.T, r *pb.McpRequestResult) (string, map[string]any) {
	t.Helper()
	var cp callParams
	if err := json.Unmarshal(r.GetMutated(), &cp); err != nil {
		t.Fatalf("mutated result is not valid callParams JSON: %v", err)
	}
	return cp.Name, cp.Arguments
}

// assertNoSideEffects enforces the v1 invariant: results carry no mutation or metadata channels.
func assertNoSideEffects(t *testing.T, r *pb.McpRequestResult) {
	t.Helper()
	if r.GetMutated() != nil {
		t.Errorf("result carried mutated bytes")
	}
	if r.GetHeaderMutation() != nil {
		t.Errorf("result carried header_mutation")
	}
	if r.GetMetadata() != nil {
		t.Errorf("result carried metadata struct")
	}
}

func TestCheckRequest_SecretPaths(t *testing.T) {
	// Every tool that can return Secret data must reach Cerbos with kind=Secret,
	// across the casing differences (getResource: kind, others: Kind).
	cases := []struct {
		name string
		tool string
		args map[string]any
	}{
		{"getResource lowercase kind", "getResource", map[string]any{"kind": "Secret", "name": "x", "namespace": "kube-system"}},
		{"listResources capital Kind", "listResources", map[string]any{"Kind": "Secret", "namespace": "default"}},
		{"describeResource capital Kind", "describeResource", map[string]any{"Kind": "Secret", "name": "x"}},
		{"getResource plural secrets", "getResource", map[string]any{"kind": "secrets", "name": "x"}},
		{"getResource lowercase secret", "getResource", map[string]any{"kind": "secret", "name": "x"}},
		{"getResource group-qualified", "getResource", map[string]any{"kind": "v1/secrets", "name": "x"}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			// Decider says DENY (mirrors the shipped deny-secrets policy).
			d := &stubDecider{allow: false}
			s := newTestServer(t, d)
			r, err := s.CheckRequest(context.Background(), mcpReq("kubernetes", "tools/call", toolCall(tc.tool, tc.args)))
			if err != nil {
				t.Fatalf("unexpected gRPC error: %v", err)
			}
			if !isDeny(r) {
				t.Fatalf("expected deny, got pass")
			}
			assertNoSideEffects(t, r)
			if d.calls != 1 {
				t.Fatalf("expected 1 cerbos call, got %d", d.calls)
			}
			if d.gotAttr["kind"] != "Secret" || d.gotAttr["apiResource"] != "secrets" {
				t.Errorf("canonicalization failed: got kind=%q apiResource=%q", d.gotAttr["kind"], d.gotAttr["apiResource"])
			}
		})
	}
}

func TestCheckRequest_AllowsNonSecretReads(t *testing.T) {
	d := &stubDecider{allow: true}
	s := newTestServer(t, d)
	r, err := s.CheckRequest(context.Background(), mcpReq("kubernetes", "tools/call",
		toolCall("getResource", map[string]any{"kind": "Pod", "name": "p", "namespace": "default"})))
	if err != nil {
		t.Fatalf("err: %v", err)
	}
	if !isPass(r) {
		t.Fatalf("expected pass for allowed Pod read")
	}
	assertNoSideEffects(t, r)
	if d.gotAttr["kind"] != "Pod" || d.gotAct != "getResource" {
		t.Errorf("wrong cerbos input: kind=%q action=%q", d.gotAttr["kind"], d.gotAct)
	}
}

func TestCheckRequest_FailClosedPaths(t *testing.T) {
	// Each of these must DENY without ever calling Cerbos with a half-built
	// resource (most must not call Cerbos at all).
	tests := []struct {
		name      string
		req       *pb.McpRequest
		deciderOK bool // verdict if Cerbos IS called
		wantCalls int
	}{
		{"unparseable params", mcpReq("kubernetes", "tools/call", []byte("{not json")), true, 0},
		{"empty params", mcpReq("kubernetes", "tools/call", nil), true, 0},
		{"missing tool name", mcpReq("kubernetes", "tools/call", []byte(`{"arguments":{"kind":"Secret"}}`)), true, 0},
		{"zero service_names", &pb.McpRequest{ServiceNames: nil, Method: "tools/call", McpRequest: toolCall("getResource", map[string]any{"kind": "Pod", "name": "p"})}, true, 0},
		{"multiple service_names", &pb.McpRequest{ServiceNames: []string{"kubernetes", "other"}, Method: "tools/call", McpRequest: toolCall("getResource", map[string]any{"kind": "Pod", "name": "p"})}, true, 0},
		{"unmapped backend", mcpReq("mystery", "tools/call", toolCall("getResource", map[string]any{"kind": "Pod", "name": "p"})), true, 0},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			d := &stubDecider{allow: tc.deciderOK}
			s := newTestServer(t, d)
			r, err := s.CheckRequest(context.Background(), tc.req)
			if err != nil {
				t.Fatalf("unexpected gRPC error: %v", err)
			}
			if !isDeny(r) {
				t.Fatalf("expected deny, got pass")
			}
			assertNoSideEffects(t, r)
			if d.calls != tc.wantCalls {
				t.Fatalf("expected %d cerbos calls, got %d", tc.wantCalls, d.calls)
			}
		})
	}
}

func TestCheckRequest_ListResourcesReachesPolicy(t *testing.T) {
	// A collection call has no single object name (mapping sets id ''). The
	// engine must substitute a non-empty id so the real Cerbos PDP evaluates
	// policy instead of rejecting on InvalidArgument.
	t.Run("Pod list allowed", func(t *testing.T) {
		d := &stubDecider{allow: true}
		s := newTestServer(t, d)
		r, err := s.CheckRequest(context.Background(), mcpReq("kubernetes", "tools/call",
			toolCall("listResources", map[string]any{"Kind": "Pod", "namespace": "default"})))
		if err != nil {
			t.Fatalf("err: %v", err)
		}
		if !isPass(r) {
			t.Fatalf("expected pass for allowed Pod list, got deny: %s", r.GetError().GetReason())
		}
		if d.calls != 1 {
			t.Fatalf("expected 1 cerbos call, got %d", d.calls)
		}
		if d.gotID == "" {
			t.Errorf("engine sent empty resource.id to Cerbos (would be rejected as InvalidArgument)")
		}
		if d.gotAttr["kind"] != "Pod" || d.gotAct != "listResources" {
			t.Errorf("wrong cerbos input: kind=%q action=%q", d.gotAttr["kind"], d.gotAct)
		}
	})
	t.Run("Secret list denied by policy", func(t *testing.T) {
		// allow:true so a deny here proves it's the policy verdict path, not an
		// empty-id rejection.
		d := &stubDecider{allow: false}
		s := newTestServer(t, d)
		r, err := s.CheckRequest(context.Background(), mcpReq("kubernetes", "tools/call",
			toolCall("listResources", map[string]any{"Kind": "secrets"})))
		if err != nil {
			t.Fatalf("err: %v", err)
		}
		if !isDeny(r) {
			t.Fatalf("expected deny for Secret list")
		}
		if d.calls != 1 {
			t.Fatalf("expected 1 cerbos call (policy evaluated), got %d", d.calls)
		}
		if d.gotID == "" {
			t.Errorf("engine sent empty resource.id to Cerbos")
		}
		if d.gotAttr["kind"] != "Secret" || d.gotAttr["apiResource"] != "secrets" {
			t.Errorf("canonicalization failed: kind=%q apiResource=%q", d.gotAttr["kind"], d.gotAttr["apiResource"])
		}
	})
}

// TestCheckRequest_PolicyDenyMessageIsGeneric pins the client-facing contract
// for a deny whose matched Cerbos rule carries no `output`: the shim falls
// back to the generic, backend-agnostic denyMessage and must NOT leak the
// probed resource type or action (those go only to the shim log).
func TestCheckRequest_PolicyDenyMessageIsGeneric(t *testing.T) {
	d := &stubDecider{allow: false} // reason: "" — no policy output configured
	s := newTestServer(t, d)
	r, err := s.CheckRequest(context.Background(), mcpReq("kubernetes", "tools/call",
		toolCall("getResource", map[string]any{"kind": "Secret", "name": "x"})))
	if err != nil {
		t.Fatalf("err: %v", err)
	}
	if !isDeny(r) {
		t.Fatalf("expected deny")
	}
	got := r.GetError().GetReason()
	if got != denyMessage {
		t.Errorf("client reason not the generic message:\n got=%q\nwant=%q", got, denyMessage)
	}
	if strings.Contains(got, "Secret") || strings.Contains(got, "k8s_resource") || strings.Contains(got, "getResource") {
		t.Errorf("deny message leaks probed resource/action to client: %q", got)
	}
}

// TestCheckRequest_PolicyDenyMessageSurfacesOutput covers the opposite case:
// when the matched Cerbos deny rule DOES carry an `output` (e.g.
// resource_github.yaml's deny-self-approve), the shim must
// surface that exact policy-authored reason to the caller verbatim instead of
// falling back to the generic denyMessage. This is what lets the calling
// agent understand *why* a call was blocked and self-correct (e.g. retry with
// REQUEST_CHANGES) instead of silently giving up or retrying blind.
func TestCheckRequest_PolicyDenyMessageSurfacesOutput(t *testing.T) {
	const reason = "This agent is not allowed to approve pull requests (event=APPROVE). Use event=COMMENT or REQUEST_CHANGES instead, or ask a human to approve."
	d := &stubDecider{allow: false, reason: reason}
	s := newTestServer(t, d)
	r, err := s.CheckRequest(context.Background(), mcpReq("kubernetes", "tools/call",
		toolCall("getResource", map[string]any{"kind": "Secret", "name": "x"})))
	if err != nil {
		t.Fatalf("err: %v", err)
	}
	if !isDeny(r) {
		t.Fatalf("expected deny")
	}
	got := r.GetError().GetReason()
	if got != reason {
		t.Errorf("client reason did not surface policy output:\n got=%q\nwant=%q", got, reason)
	}
}

// TestCheckRequest_PassDefaultContract documents the simplified model: the shim
// is a resource-blocker, not a tool/kind allowlist. Which tools exist is decided
// upstream by tool selection (ToolHive's vMCP here, or an agentgateway per-tool
// allowlist in a centralized setup); here, anything that isn't a protected
// resource passes.
func TestCheckRequest_PassDefaultContract(t *testing.T) {
	t.Run("arbitrary non-secret kind (CRD) reaches Cerbos and passes", func(t *testing.T) {
		// Unknown non-Secret kinds reach Cerbos; the shim is not a kind allowlist.
		d := &stubDecider{allow: true}
		s := newTestServer(t, d)
		r, err := s.CheckRequest(context.Background(), mcpReq("kubernetes", "tools/call",
			toolCall("getResource", map[string]any{"kind": "PrometheusRule", "name": "x"})))
		if err != nil {
			t.Fatalf("err: %v", err)
		}
		if !isPass(r) {
			t.Fatalf("expected pass for unknown non-secret kind, got deny: %s", r.GetError().GetReason())
		}
		if d.gotAttr["kind"] != "PrometheusRule" || d.gotAttr["apiResource"] != "prometheusrule" {
			t.Errorf("non-secret kind not passed through: kind=%q apiResource=%q", d.gotAttr["kind"], d.gotAttr["apiResource"])
		}
	})
	t.Run("unmapped tool passes without a Cerbos call", func(t *testing.T) {
		// getEvents/metrics carry no kind and need no mapping entry; they pass.
		d := &stubDecider{allow: false}
		s := newTestServer(t, d)
		r, err := s.CheckRequest(context.Background(), mcpReq("kubernetes", "tools/call",
			toolCall("getEvents", map[string]any{"namespace": "default"})))
		if err != nil {
			t.Fatalf("err: %v", err)
		}
		if !isPass(r) {
			t.Fatalf("expected pass for unmapped non-secret tool")
		}
		if d.calls != 0 {
			t.Fatalf("expected no cerbos call for unmapped tool, got %d", d.calls)
		}
	})
	t.Run("Secret still denied under pass-default", func(t *testing.T) {
		// The whole point: relaxing to pass-default must NOT open the Secret path.
		d := &stubDecider{allow: false}
		s := newTestServer(t, d)
		r, err := s.CheckRequest(context.Background(), mcpReq("kubernetes", "tools/call",
			toolCall("getResource", map[string]any{"kind": "Secret", "name": "x"})))
		if err != nil {
			t.Fatalf("err: %v", err)
		}
		if !isDeny(r) {
			t.Fatalf("expected Secret read to deny even under pass-default")
		}
		if d.gotAttr["kind"] != "Secret" || d.gotAttr["apiResource"] != "secrets" {
			t.Errorf("secret not canonicalized: kind=%q apiResource=%q", d.gotAttr["kind"], d.gotAttr["apiResource"])
		}
	})
	t.Run("kind-less call reaches Cerbos for the empty-kind verdict", func(t *testing.T) {
		// A kind-bearing tool called with no resolvable kind is no longer denied
		// by the shim (no requiredAttrs). It reaches Cerbos as kind=="" so the
		// policy's deny-no-kind rule owns the verdict. The shim only standardizes.
		d := &stubDecider{allow: false}
		s := newTestServer(t, d)
		r, err := s.CheckRequest(context.Background(), mcpReq("kubernetes", "tools/call",
			toolCall("getResource", map[string]any{"name": "x"})))
		if err != nil {
			t.Fatalf("err: %v", err)
		}
		if !isDeny(r) {
			t.Fatalf("expected deny (Cerbos verdict), got pass")
		}
		if d.calls != 1 {
			t.Fatalf("expected the call to reach Cerbos for the verdict, got %d calls", d.calls)
		}
		if d.gotAttr["kind"] != "" || d.gotAttr["apiResource"] != "" {
			t.Errorf("expected empty kind forwarded to Cerbos, got kind=%q apiResource=%q", d.gotAttr["kind"], d.gotAttr["apiResource"])
		}
	})
}

func TestCheckRequest_CerbosErrorDenies(t *testing.T) {
	d := &stubDecider{allow: true, err: context.DeadlineExceeded}
	s := newTestServer(t, d)
	r, err := s.CheckRequest(context.Background(), mcpReq("kubernetes", "tools/call",
		toolCall("getResource", map[string]any{"kind": "Pod", "name": "p"})))
	if err != nil {
		t.Fatalf("err: %v", err)
	}
	if !isDeny(r) {
		t.Fatalf("expected deny when Cerbos errors (fail closed)")
	}
	assertNoSideEffects(t, r)
}

func TestCheckRequest_AllowDefaultBackendPassesUnmappedTool(t *testing.T) {
	// github backend is allow-default: an unmapped tool passes WITHOUT a Cerbos call.
	d := &stubDecider{allow: false}
	s := newTestServer(t, d)
	r, err := s.CheckRequest(context.Background(), mcpReq("github", "tools/call",
		toolCall("list_repos", map[string]any{})))
	if err != nil {
		t.Fatalf("err: %v", err)
	}
	if !isPass(r) {
		t.Fatalf("expected pass for unmapped tool on allow-default backend")
	}
	if d.calls != 0 {
		t.Fatalf("expected no cerbos call, got %d", d.calls)
	}
}

// TestCheckRequest_OptimizerCallToolUnwrap covers the vMCP optimizer path
// (thv vmcp serve --optimizer): every real invocation arrives wrapped as
// call_tool{tool_name, parameters}. If the shim didn't unwrap it, the mapping
// lookup would only ever see "call_tool" and silently pass every call on this
// allow-default backend — including Secret reads.
func TestCheckRequest_OptimizerCallToolUnwrap(t *testing.T) {
	t.Run("call_tool wrapping a Secret read still denies", func(t *testing.T) {
		d := &stubDecider{allow: false}
		s := newTestServer(t, d)
		r, err := s.CheckRequest(context.Background(), mcpReq("kubernetes", "tools/call",
			optimizerCall("getResource", map[string]any{"kind": "Secret", "name": "x"})))
		if err != nil {
			t.Fatalf("err: %v", err)
		}
		if !isDeny(r) {
			t.Fatalf("expected deny for Secret read via call_tool wrapper")
		}
		if d.calls != 1 {
			t.Fatalf("expected 1 cerbos call, got %d", d.calls)
		}
		if d.gotAttr["kind"] != "Secret" {
			t.Errorf("unwrap lost the real args: kind=%q", d.gotAttr["kind"])
		}
	})

	t.Run("call_tool wrapping an allowed read passes using the real tool's mapping", func(t *testing.T) {
		d := &stubDecider{allow: true}
		s := newTestServer(t, d)
		r, err := s.CheckRequest(context.Background(), mcpReq("kubernetes", "tools/call",
			optimizerCall("getResource", map[string]any{"kind": "Pod", "name": "p", "namespace": "default"})))
		if err != nil {
			t.Fatalf("err: %v", err)
		}
		if !isPass(r) {
			t.Fatalf("expected pass, got deny: %s", r.GetError().GetReason())
		}
		if d.gotAct != "getResource" || d.gotAttr["kind"] != "Pod" {
			t.Errorf("unwrap didn't reach the real tool's mapping: action=%q kind=%q", d.gotAct, d.gotAttr["kind"])
		}
	})

	t.Run("missing tool_name denies without a Cerbos call", func(t *testing.T) {
		d := &stubDecider{allow: true}
		s := newTestServer(t, d)
		body, _ := json.Marshal(map[string]any{
			"name":      callToolMeta,
			"arguments": map[string]any{"parameters": map[string]any{"kind": "Pod"}},
		})
		r, err := s.CheckRequest(context.Background(), mcpReq("kubernetes", "tools/call", body))
		if err != nil {
			t.Fatalf("err: %v", err)
		}
		if !isDeny(r) {
			t.Fatalf("expected deny for call_tool missing tool_name")
		}
		if d.calls != 0 {
			t.Fatalf("expected no cerbos call, got %d", d.calls)
		}
	})

	t.Run("non-string tool_name denies", func(t *testing.T) {
		d := &stubDecider{allow: true}
		s := newTestServer(t, d)
		body, _ := json.Marshal(map[string]any{
			"name":      callToolMeta,
			"arguments": map[string]any{"tool_name": 5, "parameters": map[string]any{}},
		})
		r, err := s.CheckRequest(context.Background(), mcpReq("kubernetes", "tools/call", body))
		if err != nil {
			t.Fatalf("err: %v", err)
		}
		if !isDeny(r) {
			t.Fatalf("expected deny for non-string tool_name")
		}
		if d.calls != 0 {
			t.Fatalf("expected no cerbos call, got %d", d.calls)
		}
	})

	t.Run("omitted parameters unwraps to empty args instead of crashing", func(t *testing.T) {
		d := &stubDecider{allow: false}
		s := newTestServer(t, d)
		body, _ := json.Marshal(map[string]any{
			"name":      callToolMeta,
			"arguments": map[string]any{"tool_name": "listResources"},
		})
		r, err := s.CheckRequest(context.Background(), mcpReq("kubernetes", "tools/call", body))
		if err != nil {
			t.Fatalf("err: %v", err)
		}
		if !isDeny(r) {
			t.Fatalf("expected deny (stub decider denies), got pass")
		}
		if d.calls != 1 {
			t.Fatalf("expected the call to reach Cerbos, got %d calls", d.calls)
		}
	})

	t.Run("find_tool invokes no resource and passes on an allow-default backend", func(t *testing.T) {
		d := &stubDecider{allow: false}
		s := newTestServer(t, d)
		body, _ := json.Marshal(map[string]any{
			"name":      "find_tool",
			"arguments": map[string]any{"tool_description": "read a pod"},
		})
		r, err := s.CheckRequest(context.Background(), mcpReq("kubernetes", "tools/call", body))
		if err != nil {
			t.Fatalf("err: %v", err)
		}
		if !isPass(r) {
			t.Fatalf("expected pass for find_tool (no resource invoked)")
		}
		if d.calls != 0 {
			t.Fatalf("expected no cerbos call for find_tool, got %d", d.calls)
		}
	})
}

// TestCheckRequest_ForceOverride covers a mapping `force` block (used to make
// GitHub PR create/update always draft: true): on allow, the call must be
// forwarded with the forced key rewritten regardless of what was sent — as a
// Mutated result, not a bare Pass. On deny, force must never apply.
func TestCheckRequest_ForceOverride(t *testing.T) {
	t.Run("allow: forces the key and forwards other args unchanged", func(t *testing.T) {
		d := &stubDecider{allow: true}
		s := newTestServer(t, d)
		r, err := s.CheckRequest(context.Background(), mcpReq("github", "tools/call",
			toolCall("open_pr", map[string]any{"repo": "r", "draft": false, "title": "t"})))
		if err != nil {
			t.Fatalf("err: %v", err)
		}
		if !isMutated(r) {
			t.Fatalf("expected a mutated result for a force-mapped tool, got pass=%v deny=%v", isPass(r), isDeny(r))
		}
		name, args := decodeMutated(t, r)
		if name != "open_pr" {
			t.Errorf("mutated name = %q, want open_pr", name)
		}
		if args["draft"] != true {
			t.Errorf("forced arg draft = %v, want true", args["draft"])
		}
		if args["repo"] != "r" || args["title"] != "t" {
			t.Errorf("non-forced args not preserved: %v", args)
		}
	})

	t.Run("deny: force never applies, no mutation leaks through", func(t *testing.T) {
		d := &stubDecider{allow: false}
		s := newTestServer(t, d)
		r, err := s.CheckRequest(context.Background(), mcpReq("github", "tools/call",
			toolCall("open_pr", map[string]any{"repo": "r", "draft": false})))
		if err != nil {
			t.Fatalf("err: %v", err)
		}
		if !isDeny(r) {
			t.Fatalf("expected deny, got pass=%v mutated=%v", isPass(r), isMutated(r))
		}
		if isMutated(r) {
			t.Fatalf("a denied call must never carry a mutation")
		}
	})

	t.Run("a tool with no force block still returns a plain pass", func(t *testing.T) {
		d := &stubDecider{allow: true}
		s := newTestServer(t, d)
		r, err := s.CheckRequest(context.Background(), mcpReq("github", "tools/call",
			toolCall("create_issue", map[string]any{"repo": "r"})))
		if err != nil {
			t.Fatalf("err: %v", err)
		}
		if !isPass(r) {
			t.Fatalf("expected plain pass for a tool with no force block")
		}
	})

	t.Run("optimizer-wrapped call_tool is re-wrapped after mutation", func(t *testing.T) {
		d := &stubDecider{allow: true}
		s := newTestServer(t, d)
		r, err := s.CheckRequest(context.Background(), mcpReq("github", "tools/call",
			optimizerCall("open_pr", map[string]any{"repo": "r", "draft": false})))
		if err != nil {
			t.Fatalf("err: %v", err)
		}
		if !isMutated(r) {
			t.Fatalf("expected a mutated result, got pass=%v deny=%v", isPass(r), isDeny(r))
		}
		name, outerArgs := decodeMutated(t, r)
		if name != callToolMeta {
			t.Fatalf("expected the mutation to stay wrapped in call_tool, got name=%q", name)
		}
		if outerArgs["tool_name"] != "open_pr" {
			t.Errorf("tool_name = %v, want open_pr", outerArgs["tool_name"])
		}
		params, ok := outerArgs["parameters"].(map[string]any)
		if !ok {
			t.Fatalf("parameters is not a map: %v", outerArgs["parameters"])
		}
		if params["draft"] != true {
			t.Errorf("forced arg draft = %v, want true", params["draft"])
		}
		if params["repo"] != "r" {
			t.Errorf("non-forced arg not preserved: %v", params)
		}
	})

	// A force value can be a nested object (Notion create-pages forces
	// parent -> {type: page_id, page_id: <folder>}); it must replace whatever
	// parent the caller sent, wholesale.
	t.Run("nested-object force replaces the caller's value", func(t *testing.T) {
		d := &stubDecider{allow: true}
		s := newTestServer(t, d)
		r, err := s.CheckRequest(context.Background(), mcpReq("github", "tools/call",
			toolCall("force_nested", map[string]any{"repo": "r", "parent": map[string]any{"page_id": "someotherpage"}})))
		if err != nil {
			t.Fatalf("err: %v", err)
		}
		if !isMutated(r) {
			t.Fatalf("expected a mutated result, got pass=%v deny=%v", isPass(r), isDeny(r))
		}
		_, args := decodeMutated(t, r)
		parent, ok := args["parent"].(map[string]any)
		if !ok {
			t.Fatalf("forced parent is not an object: %v", args["parent"])
		}
		if parent["page_id"] != "scratchpadid" || parent["type"] != "page_id" {
			t.Errorf("forced parent = %v, want {type: page_id, page_id: scratchpadid}", parent)
		}
	})
}

func TestCheckResponse_AlwaysPassNoMutation(t *testing.T) {
	s := newTestServer(t, &stubDecider{})
	r, err := s.CheckResponse(context.Background(), &pb.McpResponse{ServiceNames: []string{"kubernetes"}, Method: "tools/call"})
	if err != nil {
		t.Fatalf("err: %v", err)
	}
	if r.GetPass() == nil {
		t.Fatalf("expected pass")
	}
	if r.GetMutated() != nil {
		t.Errorf("response carried mutation")
	}
}
