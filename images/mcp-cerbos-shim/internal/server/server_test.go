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

// fakeSlackBotToken/fakeBearerHeader build secret-shaped-but-fake test
// fixtures via concatenation so no literal credential-pattern string sits
// verbatim in this source file (keeps local secret scanners from flagging
// the test file itself).
func fakeSlackBotToken() string {
	return "xox" + "b-" + strings.Repeat("1", 10) + "-" + strings.Repeat("2", 10) + "-" + strings.Repeat("a", 24) // pragma: allowlist secret
}

func fakeBearerHeader() string {
	return "Bear" + "er " + strings.Repeat("z", 20) + "." + strings.Repeat("y", 20) + "." + strings.Repeat("x", 10) // pragma: allowlist secret
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

func TestCheckResponse_NonToolsCallAlwaysPassNoMutation(t *testing.T) {
	s := newTestServer(t, &stubDecider{})
	r, err := s.CheckResponse(context.Background(), &pb.McpResponse{ServiceNames: []string{"kubernetes"}, Method: "tools/list"})
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

func TestCheckResponse_ResourcesReadAndPromptsGetAlsoRedact(t *testing.T) {
	// HAH-101: resources/read and prompts/get response bodies must be
	// scrubbed for secret-shaped values same as tools/call, even though
	// neither carries Cerbos authz (no mapping exists to build a resource
	// from a resources/read URI or a prompts/get name).
	for _, method := range []string{"resources/read", "prompts/get"} {
		t.Run(method, func(t *testing.T) {
			s := newTestServer(t, &stubDecider{})
			secret := fakeSlackBotToken()
			payload := map[string]any{
				"contents": []any{map[string]any{"type": "text", "text": "token: " + secret}},
			}
			body, err := json.Marshal(payload)
			if err != nil {
				t.Fatalf("marshal fixture: %v", err)
			}
			r, err := s.CheckResponse(context.Background(), &pb.McpResponse{ServiceNames: []string{"kubernetes"}, Method: method, McpResponse: body})
			if err != nil {
				t.Fatalf("err: %v", err)
			}
			mutated := r.GetMutated()
			if mutated == nil {
				t.Fatalf("expected a mutated (redacted) result for method %q, got pass=%v", method, r.GetPass() != nil)
			}
			if strings.Contains(string(mutated), secret) {
				t.Errorf("secret leaked through unredacted for method %q: %s", method, mutated)
			}
			if !strings.Contains(string(mutated), redactedPlaceholder) {
				t.Errorf("expected redaction placeholder in mutated result for method %q: %s", method, mutated)
			}
		})
	}
}

func TestCheckResponse_ResourcesReadAndPromptsGetCleanBodyPassesUnmutated(t *testing.T) {
	// Same two methods, but confirming the "nothing to redact" path still
	// passes unmutated rather than always mutating once a method is in
	// redactableResponseMethods.
	for _, method := range []string{"resources/read", "prompts/get"} {
		t.Run(method, func(t *testing.T) {
			s := newTestServer(t, &stubDecider{})
			body := []byte(`{"contents":[{"type":"text","text":"nothing secret here"}]}`)
			r, err := s.CheckResponse(context.Background(), &pb.McpResponse{ServiceNames: []string{"kubernetes"}, Method: method, McpResponse: body})
			if err != nil {
				t.Fatalf("err: %v", err)
			}
			if r.GetPass() == nil {
				t.Fatalf("expected pass for a clean %q result, got mutated=%v", method, r.GetMutated() != nil)
			}
		})
	}
}

func TestCheckResponse_StillIgnoresUnrelatedMethods(t *testing.T) {
	// Widening to resources/read + prompts/get must not become a blanket
	// widening -- an unrelated method (e.g. tools/list, resources/list)
	// still passes through untouched, matching the pre-HAH-101 behavior
	// TestCheckResponse_NonToolsCallAlwaysPassNoMutation already covers for
	// tools/list.
	for _, method := range []string{"resources/list", "prompts/list", "initialize"} {
		t.Run(method, func(t *testing.T) {
			s := newTestServer(t, &stubDecider{})
			secret := fakeSlackBotToken()
			body := []byte(`{"text":"token: ` + secret + `"}`)
			r, err := s.CheckResponse(context.Background(), &pb.McpResponse{ServiceNames: []string{"kubernetes"}, Method: method, McpResponse: body})
			if err != nil {
				t.Fatalf("err: %v", err)
			}
			if r.GetPass() == nil {
				t.Fatalf("expected pass for unrelated method %q, got mutated=%v", method, r.GetMutated() != nil)
			}
			if r.GetMutated() != nil {
				t.Errorf("unrelated method %q should never be mutated", method)
			}
		})
	}
}

func TestCheckRequest_ResourcesReadAndPromptsGetSkipCerbosAuthz(t *testing.T) {
	// resources/read and prompts/get have no Cerbos mapping (no resource
	// type can be built from a resource URI or prompt name), so
	// CheckRequest must still pass them through on an allow-default backend
	// exactly like any other unmapped method -- only CheckResponse widened,
	// not CheckRequest's authz gate.
	for _, method := range []string{"resources/read", "prompts/get"} {
		t.Run(method, func(t *testing.T) {
			s := newTestServer(t, &stubDecider{})
			r, err := s.CheckRequest(context.Background(), &pb.McpRequest{ServiceNames: []string{"kubernetes"}, Method: method})
			if err != nil {
				t.Fatalf("err: %v", err)
			}
			if r.GetPass() == nil {
				t.Fatalf("expected pass for %q on allow-default backend, got %+v", method, r)
			}
		})
	}
}

func TestCheckResponse_CleanToolsCallResultPassesUnmutated(t *testing.T) {
	s := newTestServer(t, &stubDecider{})
	body := []byte(`{"content":[{"type":"text","text":"all good, no secrets here"}],"isError":false}`)
	r, err := s.CheckResponse(context.Background(), &pb.McpResponse{ServiceNames: []string{"kubernetes"}, Method: "tools/call", McpResponse: body})
	if err != nil {
		t.Fatalf("err: %v", err)
	}
	if r.GetPass() == nil {
		t.Fatalf("expected pass for a clean result, got mutated=%v", r.GetMutated() != nil)
	}
}

func TestCheckResponse_EmptyOrUnparseableBodyPassesUnmutated(t *testing.T) {
	s := newTestServer(t, &stubDecider{})

	t.Run("empty body", func(t *testing.T) {
		r, err := s.CheckResponse(context.Background(), &pb.McpResponse{Method: "tools/call", McpResponse: nil})
		if err != nil {
			t.Fatalf("err: %v", err)
		}
		if r.GetPass() == nil {
			t.Fatalf("expected pass for an empty body")
		}
	})

	t.Run("unparseable body", func(t *testing.T) {
		r, err := s.CheckResponse(context.Background(), &pb.McpResponse{Method: "tools/call", McpResponse: []byte("not json")})
		if err != nil {
			t.Fatalf("err: %v", err)
		}
		if r.GetPass() == nil {
			t.Fatalf("expected pass for an unparseable body (fail-open on redaction, not fail-closed)")
		}
	})
}

func TestCheckResponse_RedactsSecretInToolResult(t *testing.T) {
	s := newTestServer(t, &stubDecider{})
	secret := fakeSlackBotToken()
	payload := map[string]any{
		"content": []any{map[string]any{"type": "text", "text": "here is the token: " + secret}},
		"isError": false,
	}
	body, err := json.Marshal(payload)
	if err != nil {
		t.Fatalf("marshal fixture: %v", err)
	}
	r, err := s.CheckResponse(context.Background(), &pb.McpResponse{ServiceNames: []string{"kubernetes"}, Method: "tools/call", McpResponse: body})
	if err != nil {
		t.Fatalf("err: %v", err)
	}
	mutated := r.GetMutated()
	if mutated == nil {
		t.Fatalf("expected a mutated (redacted) result, got pass=%v", r.GetPass() != nil)
	}
	if strings.Contains(string(mutated), secret) {
		t.Errorf("secret leaked through unredacted: %s", mutated)
	}
	if !strings.Contains(string(mutated), redactedPlaceholder) {
		t.Errorf("expected redaction placeholder in mutated result: %s", mutated)
	}
	var decoded map[string]any
	if err := json.Unmarshal(mutated, &decoded); err != nil {
		t.Fatalf("mutated result is not valid JSON: %v", err)
	}
}

func TestCheckRequest_RedactsSecretInArguments(t *testing.T) {
	secret := fakeSlackBotToken()
	d := &stubDecider{allow: true}
	s := newTestServer(t, d)
	r, err := s.CheckRequest(context.Background(), mcpReq("github", "tools/call",
		toolCall("create_issue", map[string]any{"repo": "r", "body": "leaked key: " + secret})))
	if err != nil {
		t.Fatalf("err: %v", err)
	}
	if !isMutated(r) {
		t.Fatalf("expected a mutated result for a call carrying a secret-shaped argument, got pass=%v deny=%v", isPass(r), isDeny(r))
	}
	_, args := decodeMutated(t, r)
	body, _ := args["body"].(string)
	if strings.Contains(body, secret) {
		t.Errorf("secret leaked through unredacted: %q", body)
	}
	if !strings.Contains(body, redactedPlaceholder) {
		t.Errorf("expected redaction placeholder in mutated arg: %q", body)
	}
	if args["repo"] != "r" {
		t.Errorf("non-secret args should be preserved unchanged: %v", args)
	}
}

func TestCheckRequest_NoSecretMeansPlainPassWhenNoForce(t *testing.T) {
	d := &stubDecider{allow: true}
	s := newTestServer(t, d)
	r, err := s.CheckRequest(context.Background(), mcpReq("github", "tools/call",
		toolCall("create_issue", map[string]any{"repo": "r", "body": "just an ordinary comment"})))
	if err != nil {
		t.Fatalf("err: %v", err)
	}
	if !isPass(r) {
		t.Fatalf("expected a plain pass when nothing needs redacting and the tool has no force block")
	}
}

func TestCheckRequest_DeniedCallNeverMutatesEvenWithSecret(t *testing.T) {
	secret := fakeSlackBotToken()
	d := &stubDecider{allow: false}
	s := newTestServer(t, d)
	r, err := s.CheckRequest(context.Background(), mcpReq("github", "tools/call",
		toolCall("create_issue", map[string]any{"repo": "r", "body": secret})))
	if err != nil {
		t.Fatalf("err: %v", err)
	}
	if !isDeny(r) {
		t.Fatalf("expected deny, got pass=%v mutated=%v", isPass(r), isMutated(r))
	}
	if isMutated(r) {
		t.Fatalf("a denied call must never carry a mutation, secret-redaction or otherwise")
	}
}

func TestRedactString(t *testing.T) {
	// Fixtures are built via string concatenation (strings.Repeat / manual
	// joins) rather than literal secret-shaped constants, so no
	// credential-pattern-matching string sits in this source file itself.
	// Every entry in secretPatternRegistry gets one fixture here -- when
	// adding a new pattern to the registry, add its fixture in the same
	// shape (fragment any structural marker -- prefix, header/footer -- via
	// concatenation, not just the random-looking body) and a case below.
	sshKey := "-----BEGIN " + "RSA " + "PRIVATE" + " KEY-----\n" + // pragma: allowlist secret
		strings.Repeat("QUJDRUZHSElKS0xNTk9QUVJTVFVWV1hZWg==\n", 3) +
		"-----END " + "RSA " + "PRIVATE" + " KEY-----"
	slackBot := "xox" + "b-" + strings.Repeat("1", 10) + "-" + strings.Repeat("2", 10) + "-" + strings.Repeat("a", 24)    // pragma: allowlist secret
	slackApp := "xapp-" + "1-" + strings.Repeat("A", 10) + "-" + strings.Repeat("9", 20)                                  // pragma: allowlist secret
	bearerVal := "Bear" + "er " + strings.Repeat("z", 20) + "." + strings.Repeat("y", 20) + "." + strings.Repeat("x", 10) // pragma: allowlist secret
	basicVal := "Bas" + "ic " + strings.Repeat("b", 24) + "=="                                                            // pragma: allowlist secret
	awsKey := "AKIA" + strings.Repeat("Q", 16)                                                                            // pragma: allowlist secret
	githubToken := "gh" + "p_" + strings.Repeat("g", 36)                                                                  // pragma: allowlist secret
	gitlabToken := "glp" + "at-" + strings.Repeat("l", 20)                                                                // pragma: allowlist secret
	googleKey := "AIza" + strings.Repeat("G", 35)                                                                         // pragma: allowlist secret
	openaiKey := "sk-" + strings.Repeat("o", 20)                                                                          // pragma: allowlist secret
	openaiProjKey := "sk-" + "proj-" + strings.Repeat("p", 20)                                                            // pragma: allowlist secret
	anthropicKey := "sk-" + "ant-" + strings.Repeat("n", 20)                                                              // pragma: allowlist secret
	stripeKey := "sk" + "_live_" + strings.Repeat("s", 16)                                                                // pragma: allowlist secret
	notionTok := "ntn" + "_" + strings.Repeat("t", 20)                                                                    // pragma: allowlist secret
	twilioKey := "SK" + strings.Repeat("f", 32)                                                                           // pragma: allowlist secret
	npmTok := "npm" + "_" + strings.Repeat("m", 36)                                                                       // pragma: allowlist secret
	jwtVal := "eyJ" + strings.Repeat("h", 10) + "." + "eyJ" + strings.Repeat("p", 10) + "." + strings.Repeat("s", 10)     // pragma: allowlist secret
	// PII (fake) — SSN, two card issuer shapes, and a US phone number. Built by
	// concatenation like the secret fixtures above.
	ssnVal := "123" + "-" + "45" + "-" + "6789"
	visaCard := "4" + strings.Repeat("1", 15)        // 16-digit Visa (starts 4)
	mcCard := "5" + "1" + strings.Repeat("0", 14)    // 16-digit Mastercard (starts 51)
	amexCard := "3" + "4" + strings.Repeat("0", 13)  // 15-digit Amex (starts 34)
	discoverCard := "6011" + strings.Repeat("0", 12) // 16-digit Discover (starts 6011)
	phoneVal := "(" + "555" + ") " + "123" + "-" + "4567"

	cases := []struct {
		name       string
		in         string
		wantRedact bool
		wantAbsent string // substring that must NOT survive redaction
	}{
		{
			name:       "ssh private key",
			in:         "leading noise " + sshKey + " trailing noise",
			wantRedact: true,
			wantAbsent: sshKey,
		},
		{
			name:       "slack bot token",
			in:         "token=" + slackBot,
			wantRedact: true,
			wantAbsent: slackBot,
		},
		{
			name:       "slack app-config token",
			in:         slackApp + " is the app token",
			wantRedact: true,
			wantAbsent: slackApp,
		},
		{
			name:       "bearer token",
			in:         "Authorization: " + bearerVal,
			wantRedact: true,
			wantAbsent: bearerVal,
		},
		{
			name:       "basic auth",
			in:         "Authorization: " + basicVal,
			wantRedact: true,
			wantAbsent: basicVal,
		},
		{
			name:       "aws access key id",
			in:         "export AWS_ACCESS_KEY_ID=" + awsKey,
			wantRedact: true,
			wantAbsent: awsKey,
		},
		{
			name:       "github token",
			in:         "GITHUB_TOKEN=" + githubToken,
			wantRedact: true,
			wantAbsent: githubToken,
		},
		{
			name:       "gitlab token",
			in:         "private_token: " + gitlabToken,
			wantRedact: true,
			wantAbsent: gitlabToken,
		},
		{
			name:       "google api key",
			in:         "key=" + googleKey,
			wantRedact: true,
			wantAbsent: googleKey,
		},
		{
			name:       "openai api key",
			in:         "OPENAI_API_KEY=" + openaiKey,
			wantRedact: true,
			wantAbsent: openaiKey,
		},
		{
			name:       "openai project-scoped api key",
			in:         "OPENAI_API_KEY=" + openaiProjKey,
			wantRedact: true,
			wantAbsent: openaiProjKey,
		},
		{
			name:       "anthropic api key",
			in:         "ANTHROPIC_API_KEY=" + anthropicKey,
			wantRedact: true,
			wantAbsent: anthropicKey,
		},
		{
			name:       "stripe api key",
			in:         "stripe key: " + stripeKey,
			wantRedact: true,
			wantAbsent: stripeKey,
		},
		{
			name:       "notion integration token",
			in:         "NOTION_TOKEN=" + notionTok,
			wantRedact: true,
			wantAbsent: notionTok,
		},
		{
			name:       "twilio api key sid",
			in:         "twilio key: " + twilioKey,
			wantRedact: true,
			wantAbsent: twilioKey,
		},
		{
			name:       "npm token",
			in:         "//registry.npmjs.org/:_authToken=" + npmTok,
			wantRedact: true,
			wantAbsent: npmTok,
		},
		{
			name:       "generic jwt",
			in:         "Authorization header value (no Bearer prefix): " + jwtVal,
			wantRedact: true,
			wantAbsent: jwtVal,
		},
		{
			name:       "us ssn",
			in:         "ssn: " + ssnVal,
			wantRedact: true,
			wantAbsent: ssnVal,
		},
		{
			name:       "visa card number",
			in:         "card " + visaCard + " on file",
			wantRedact: true,
			wantAbsent: visaCard,
		},
		{
			name:       "mastercard card number",
			in:         "card=" + mcCard,
			wantRedact: true,
			wantAbsent: mcCard,
		},
		{
			name:       "amex card number",
			in:         "amex " + amexCard,
			wantRedact: true,
			wantAbsent: amexCard,
		},
		{
			name:       "discover card number",
			in:         "discover " + discoverCard,
			wantRedact: true,
			wantAbsent: discoverCard,
		},
		{
			name:       "us phone number",
			in:         "call me at " + phoneVal,
			wantRedact: true,
			wantAbsent: phoneVal,
		},
		{
			name:       "ordinary text is untouched",
			in:         "This PR closes the auth bug, no credentials involved.",
			wantRedact: false,
			wantAbsent: "",
		},
		// PII prefix/range scoping must NARROW matches: these must NOT redact,
		// proving the card patterns are issuer-prefix-scoped (not a naive
		// 13-19 digit catch-all) and the SSN pattern excludes invalid ranges.
		{
			name:       "16-digit number with no known issuer prefix is not a card",
			in:         "order number " + strings.Repeat("9", 16),
			wantRedact: false,
			wantAbsent: "",
		},
		{
			name:       "16-digit number starting 1 (unassigned IIN) is not a card",
			in:         "ref 1234567890123456 processed",
			wantRedact: false,
			wantAbsent: "",
		},
		{
			name:       "ssn with invalid area 000 is not matched",
			in:         "value " + "000" + "-" + "12" + "-" + "3456",
			wantRedact: false,
			wantAbsent: "",
		},
		{
			name:       "bare 10-digit run without separators is not a phone",
			in:         "id 5551234567 here",
			wantRedact: false,
			wantAbsent: "",
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			out, n := redactString(tc.in)
			if tc.wantRedact && n == 0 {
				t.Fatalf("expected at least one redaction, got none for %q", tc.in)
			}
			if !tc.wantRedact && n != 0 {
				t.Fatalf("expected zero redactions, got %d for %q -> %q", n, tc.in, out)
			}
			if tc.wantAbsent != "" && strings.Contains(out, tc.wantAbsent) {
				t.Errorf("secret substring %q survived redaction: %q", tc.wantAbsent, out)
			}
		})
	}
}

func TestRedactValue_NestedStructures(t *testing.T) {
	nestedSecret := fakeSlackBotToken()                  // pragma: allowlist secret
	listSecret := "Authorization: " + fakeBearerHeader() // pragma: allowlist secret
	in := map[string]any{
		"safe": "nothing to see here",
		"nested": map[string]any{
			"token": nestedSecret,
		},
		"list": []any{
			"clean entry",
			listSecret,
		},
	}
	out, n := redactValue(in)
	if n < 2 {
		t.Fatalf("expected at least 2 redactions across nested structures, got %d", n)
	}
	outMap, ok := out.(map[string]any)
	if !ok {
		t.Fatalf("redactValue did not return a map: %T", out)
	}
	if outMap["safe"] != "nothing to see here" {
		t.Errorf("safe value was altered: %v", outMap["safe"])
	}
	nested, ok := outMap["nested"].(map[string]any)
	if !ok {
		t.Fatalf("nested map lost its shape: %v", outMap["nested"])
	}
	if strings.Contains(nested["token"].(string), nestedSecret) {
		t.Errorf("nested secret leaked through: %v", nested["token"])
	}
	list, ok := outMap["list"].([]any)
	if !ok || len(list) != 2 {
		t.Fatalf("list lost its shape: %v", outMap["list"])
	}
	if strings.Contains(list[1].(string), listSecret) {
		t.Errorf("secret in list element leaked through: %v", list[1])
	}
	if list[0] != "clean entry" {
		t.Errorf("clean list entry was altered: %v", list[0])
	}
}

func TestRedactValue_SmuggledJSONStringIsRecursivelyScrubbed(t *testing.T) {
	// Mirrors Jira's additional_fields/fields raw-JSON-string args -- a
	// secret can be smuggled one level of JSON-string-encoding deep, same
	// shape jiraFieldsAttr already has to unwrap for epicKey/parent.
	secret := fakeSlackBotToken()
	inner := map[string]any{"epicKey": "OTHER-123", "note": secret}
	innerJSON, err := json.Marshal(inner)
	if err != nil {
		t.Fatalf("marshal inner: %v", err)
	}
	args := map[string]any{"additional_fields": string(innerJSON)}

	out, n := redactValue(args)
	if n == 0 {
		t.Fatalf("expected the smuggled secret to be found and redacted")
	}
	outMap, ok := out.(map[string]any)
	if !ok {
		t.Fatalf("redactValue did not return a map: %T", out)
	}
	rewritten, ok := outMap["additional_fields"].(string)
	if !ok {
		t.Fatalf("additional_fields is no longer a string: %v", outMap["additional_fields"])
	}
	if strings.Contains(rewritten, secret) {
		t.Errorf("secret smuggled inside a JSON string survived redaction: %q", rewritten)
	}
	// The epicKey signal jiraFieldsAttr relies on must survive -- redaction
	// must not corrupt the surrounding structure, only the secret value.
	var reparsed map[string]any
	if err := json.Unmarshal([]byte(rewritten), &reparsed); err != nil {
		t.Fatalf("rewritten additional_fields is no longer valid JSON: %v", err)
	}
	if reparsed["epicKey"] != "OTHER-123" {
		t.Errorf("epicKey signal was corrupted by redaction: %v", reparsed["epicKey"])
	}
}

func TestRedactRawJSON_InvalidJSONPassesThroughUnchanged(t *testing.T) {
	raw := []byte("not json at all")
	out, n := redactRawJSON(raw)
	if n != 0 {
		t.Errorf("expected 0 redactions for unparseable input, got %d", n)
	}
	if string(out) != string(raw) {
		t.Errorf("unparseable input should pass through byte-for-byte unchanged, got %q", out)
	}
}

// TestRedactString_GitleaksCatchesBeyondRegistry proves the gitleaks pass adds
// coverage the hand-rolled secretPatternRegistry does NOT have. SendGrid API
// tokens (gitleaks rule "sendgrid-api-token", shape "SG." + 66 chars) are the
// exemplar: there is no SendGrid entry in secretPatternRegistry, so if this
// redacts, it can only be gitleaks doing it. Fixtures are fragmented via
// concatenation for the same reason the registry table is -- keep any
// structural marker out of a single literal so the repo's own detect-secrets
// hook stays quiet.
func TestRedactString_GitleaksCatchesBeyondRegistry(t *testing.T) {
	// SG. + 66 chars from [a-z0-9=_\-.], split across concatenated fragments.
	sendgridKey := "S" + "G." + strings.Repeat("a", 22) + "." + strings.Repeat("b", 43) // pragma: allowlist secret
	if got := len("SG.") + 66; got != len(sendgridKey) {
		t.Fatalf("sendgrid fixture is malformed: want %d chars, got %d", got, len(sendgridKey))
	}

	// Confirm the registry alone would miss it -- this is the whole point of
	// the gitleaks layer, so assert the negative explicitly rather than trust
	// it. redactPattern over every registry entry must find nothing.
	registryHits := 0
	for _, p := range secretPatternRegistry {
		if _, n := redactPattern(p.re, sendgridKey); n > 0 {
			registryHits += n
		}
	}
	if registryHits != 0 {
		t.Fatalf("sendgrid fixture unexpectedly matched the local registry (%d hits) -- "+
			"pick a shape genuinely outside secretPatternRegistry", registryHits)
	}

	out, n := redactString("sendgrid credential: " + sendgridKey + " (do not leak)")
	if n == 0 {
		t.Fatalf("expected gitleaks to redact the SendGrid token, got zero redactions")
	}
	if strings.Contains(out, sendgridKey) {
		t.Errorf("SendGrid token survived redaction: %q", out)
	}
	if !strings.Contains(out, redactedPlaceholder) {
		t.Errorf("expected redaction placeholder in output: %q", out)
	}
}

// TestRedactString_CustomRegistryStillCoversGaps proves the custom registry is
// still load-bearing after the gitleaks integration: it catches shapes that
// gitleaks' default ruleset does NOT flag on their own. Notion (ntn_) and
// Twilio (SK...) tokens are the exemplars here -- empirically gitleaks'
// default 180 rules do not match these fixture shapes, so redaction of them is
// attributable to the local registry. This is exactly the "keep the custom
// escape hatch" guarantee: even where gitleaks is silent, the registry fires.
func TestRedactString_CustomRegistryStillCoversGaps(t *testing.T) {
	notionTok := "ntn" + "_" + strings.Repeat("t", 20) // pragma: allowlist secret
	twilioKey := "SK" + strings.Repeat("f", 32)        // pragma: allowlist secret

	for _, tc := range []struct {
		name, secret string
	}{
		{"notion", notionTok},
		{"twilio", twilioKey},
	} {
		t.Run(tc.name, func(t *testing.T) {
			// gitleaks alone is expected to be silent on these shapes; if a
			// future gitleaks bump starts covering one, this log documents the
			// overlap without failing (the user explicitly wants the registry
			// kept regardless of upstream coverage).
			if _, gn := redactStringGitleaks("value=" + tc.secret); gn > 0 {
				t.Logf("note: gitleaks now also covers the %s shape (overlap, not a problem)", tc.name)
			}
			out, n := redactString("credential=" + tc.secret)
			if n == 0 {
				t.Fatalf("expected the %s token to be redacted by the custom registry", tc.name)
			}
			if strings.Contains(out, tc.secret) {
				t.Errorf("%s token survived redaction: %q", tc.name, out)
			}
		})
	}
}

// TestRedactString_Idempotent proves running redaction repeatedly is safe: a
// second pass over already-redacted text finds nothing new, the placeholder
// itself is never mistaken for a secret by either layer, and no garbage is
// produced. This guards the double-redaction edge the two-layer design could
// otherwise introduce (gitleaks + registry both sweeping the same string, and
// the shim redacting both a tool-call argument and, later, a response that
// echoes it).
func TestRedactString_Idempotent(t *testing.T) {
	// A payload mixing a gitleaks-caught shape (AWS key) and a registry-caught
	// shape (Notion) so both layers fire on the first pass.
	awsKey := "AKIA" + strings.Repeat("Q", 16)         // pragma: allowlist secret
	notionTok := "ntn" + "_" + strings.Repeat("t", 20) // pragma: allowlist secret
	in := "aws=" + awsKey + " notion=" + notionTok

	first, n1 := redactString(in)
	if n1 < 2 {
		t.Fatalf("expected at least 2 redactions on first pass (aws + notion), got %d", n1)
	}
	if strings.Contains(first, awsKey) || strings.Contains(first, notionTok) {
		t.Fatalf("a secret survived the first pass: %q", first)
	}

	second, n2 := redactString(first)
	if n2 != 0 {
		t.Errorf("second pass over already-redacted text should redact nothing, got %d", n2)
	}
	if second != first {
		t.Errorf("second pass mutated already-redacted text:\n first=%q\nsecond=%q", first, second)
	}

	// The placeholder must not itself read as a secret to either layer, or the
	// scrub would never converge.
	if _, np := redactString(strings.Repeat(redactedPlaceholder+" ", 8)); np != 0 {
		t.Errorf("the redaction placeholder was matched as a secret (%d hits) -- scrub cannot converge", np)
	}
}
