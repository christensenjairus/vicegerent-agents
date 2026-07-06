// Package server implements the agentgateway ExtMcp gRPC service.
// Fail-closed contract: only tools/call is evaluated; bad params/mapping/eval/Cerbos errors
// deny. Responses are pass or error, except a tool with a mapping `force` set, which allows
// via a mutated (rewritten-args) result instead of a bare pass — never on a denied call.
package server

import (
	"context"
	"encoding/json"
	"fmt"
	"log"
	"time"

	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	config "github.com/jchristensen/vicegerent-agents/images/mcp-cerbos-shim/internal"
	"github.com/jchristensen/vicegerent-agents/images/mcp-cerbos-shim/internal/authz"
	"github.com/jchristensen/vicegerent-agents/images/mcp-cerbos-shim/internal/eval"
	"github.com/jchristensen/vicegerent-agents/images/mcp-cerbos-shim/internal/upstream"
	pb "github.com/jchristensen/vicegerent-agents/images/mcp-cerbos-shim/proto/gen"
)

const toolsCall = "tools/call"

// The Notion update-page ancestry gate keys off the mapped resource, not the
// tool-name string, so renaming the tool in mapping.yaml keeps the gate intact
// as long as it keeps this resourceType/action pair (matches the update rule in
// defs/resource_notion.yaml).
const (
	notionPageResource = "notion_page"
	notionUpdateAction = "update"
)

// notionAncestryTimeout bounds the single live notion-fetch the ancestry gate
// makes, so one update-page call can't hang the whole CheckRequest (the gateway
// is FailClosed, so a hang would deny anyway — but only after its own longer
// timeout, holding the connection open meanwhile).
const notionAncestryTimeout = 5 * time.Second

// callToolMeta is the vMCP optimizer's (thv vmcp serve --optimizer/--optimizer-embedding)
// meta-tool name. With the optimizer on, vMCP exposes only find_tool/call_tool instead
// of the real backend tools, so every actual invocation arrives wrapped as
// call_tool{tool_name, parameters} rather than under its own name. Left unhandled, the
// mapping lookup below would only ever see "call_tool" — never a mapped tool — and
// silently pass every call through on this backend's defaultAction: allow. Field names
// match github.com/stacklok/toolhive/pkg/vmcp/optimizer.CallToolInput.
const callToolMeta = "call_tool"

// denyMessage is the fallback used when Cerbos denies a call but the matched
// deny rule carries no policy `output` (see policies/defs/*.yaml `output:`
// blocks). It intentionally omits resource/action to avoid leaking probed
// state; detail goes to the shim log. Prefer adding an `output` to the rule
// over relying on this generic string — see HAH-65/72: without a specific
// reason, a calling agent has no way to distinguish "try a different
// approach" (self-approve blocked, use REQUEST_CHANGES instead) from
// "this whole avenue is closed" (protected branch, wrong project), and burns
// retries rediscovering the boundary by trial and error.
const denyMessage = "Access denied by security policy. This is an intentional restriction, not a tool error; try a different resource or action."

// Principal is a fixed audit constant (not an authz control; policy denies only by resource).
type Principal struct {
	ID    string
	Roles []string
}

// Server implements pb.ExtMcpServer.
type Server struct {
	pb.UnimplementedExtMcpServer
	mapping   *config.Mapping
	engine    *eval.Engine
	decider   authz.Decider
	principal Principal

	// notionAncestry, when set, gates notion_notion-update-page to pages under
	// the Scratchpad tree via a live notion-fetch lookup — a network round trip
	// the CEL/Cerbos path can't make (it's pure/synchronous, no I/O). It lives
	// on Server rather than in a CEL helper for that reason. notionScratchpadID
	// is the ancestor every updatable page must descend from; it's sourced from
	// the same mapping force-rewrite value notion-create-pages uses (see
	// main.go), so create and update stay pinned to one Scratchpad id.
	notionAncestry     upstream.ToolCaller
	notionScratchpadID string
}

// Option configures a Server at construction. Variadic so existing four-arg
// New callers (tests, and any backend that doesn't need the ancestry gate)
// keep compiling unchanged.
type Option func(*Server)

// WithNotionAncestry enables the Notion update-page Scratchpad-ancestry gate.
// client resolves a page's ancestors (production: an upstream.Client to vMCP;
// tests: a stub); scratchpadPageID is the required ancestor.
func WithNotionAncestry(client upstream.ToolCaller, scratchpadPageID string) Option {
	return func(s *Server) {
		s.notionAncestry = client
		s.notionScratchpadID = scratchpadPageID
	}
}

// New constructs a Server. The engine must already be compiled from mapping.
func New(m *config.Mapping, e *eval.Engine, d authz.Decider, p Principal, opts ...Option) *Server {
	s := &Server{mapping: m, engine: e, decider: d, principal: p}
	for _, o := range opts {
		o(s)
	}
	return s
}

// callParams is the tools/call params shape (rmcp CallToolRequestParam).
type callParams struct {
	Name      string         `json:"name"`
	Arguments map[string]any `json:"arguments"`
}

// CheckRequest is the pre-forward gate. It returns Pass{} to allow, Mutated{}
// to allow-with-rewritten-args (only for a tool carrying a mapping `force`
// set), or an AuthorizationError to deny. It never sets header_mutation/metadata.
func (s *Server) CheckRequest(ctx context.Context, req *pb.McpRequest) (*pb.McpRequestResult, error) {
	backend, derr := s.resolveBackend(req.GetServiceNames())
	if derr != nil {
		return deny(derr.Error()), nil
	}

	b := s.mapping.Backends[backend]

	// Only tools/call is handled; other methods deny on a deny-default backend.
	if req.GetMethod() != toolsCall {
		if b.DefaultAction == config.ActionDeny {
			return deny(fmt.Sprintf("method %q not handled on deny-default backend %q", req.GetMethod(), backend)), nil
		}
		return pass(), nil
	}

	// Unparseable/missing params deny; don't rely on gateway FailClosed for our own failures.
	raw := req.GetMcpRequest()
	if len(raw) == 0 {
		return deny("tools/call has no params"), nil
	}
	var cp callParams
	if err := json.Unmarshal(raw, &cp); err != nil {
		return deny(fmt.Sprintf("unparseable tools/call params: %v", err)), nil
	}
	if cp.Name == "" {
		return deny("tools/call params missing tool name"), nil
	}
	if cp.Arguments == nil {
		cp.Arguments = map[string]any{} // valid: some tools take no args
	}

	// wrapped remembers whether this call arrived through the optimizer's
	// call_tool meta-tool, so an eventual mutation can be re-wrapped into the
	// same shape before forwarding (the gateway replaces the whole params
	// object verbatim; it does not know about call_tool itself).
	wrapped := cp.Name == callToolMeta
	if wrapped {
		toolName, ok := cp.Arguments["tool_name"].(string)
		if !ok || toolName == "" {
			return deny("call_tool missing string tool_name"), nil
		}
		params, _ := cp.Arguments["parameters"].(map[string]any) // absent/wrong-type -> no args
		cp.Name = toolName
		cp.Arguments = params
		if cp.Arguments == nil {
			cp.Arguments = map[string]any{}
		}
	}

	tool, ok := b.Tools[cp.Name]
	if !ok {
		if b.DefaultAction == config.ActionDeny {
			return deny(fmt.Sprintf("tool %q not mapped on deny-default backend %q", cp.Name, backend)), nil
		}
		return pass(), nil
	}

	res, err := s.engine.Eval(eval.CallInput{
		Tool: cp.Name, Backend: backend, Method: req.GetMethod(), Args: cp.Arguments,
	})
	if err != nil {
		return deny(fmt.Sprintf("policy input eval: %v", err)), nil
	}

	// Notion update-page gate: this runs BEFORE Cerbos (a live ancestry lookup
	// Cerbos itself can't do) and denies any update to a page outside the
	// Scratchpad tree. The Cerbos policy still independently blocks destructive
	// commands (replace_content / allow_deleting_content) on the pages that DO
	// pass this gate; the two checks are complementary, not redundant.
	if res.ResourceType == notionPageResource && res.Action == notionUpdateAction {
		if derr := s.checkNotionAncestry(ctx, res.ID); derr != nil {
			log.Printf("deny: notion update-page ancestry (page=%q backend=%s): %v", res.ID, backend, derr)
			return deny(derr.Error()), nil
		}
	}

	allowed, reason, err := s.decider.IsAllowed(ctx,
		s.principal.ID, s.principal.Roles,
		res.ResourceType, res.ID, res.Attr, res.Action)
	if err != nil {
		return deny(fmt.Sprintf("authorization check failed: %v", err)), nil
	}
	if !allowed {
		log.Printf("deny: %s on %s (tool=%s backend=%s reason=%q)", res.Action, res.ResourceType, cp.Name, backend, reason)
		// Surface the policy-authored reason (Cerbos rule `output`) when present
		// so the calling agent understands *why* and what to do instead (e.g.
		// "use REQUEST_CHANGES instead of APPROVE") rather than retrying blindly
		// or silently downgrading its own intent. Falls back to the generic
		// denyMessage when the matched rule has no output configured.
		msg := denyMessage
		if reason != "" {
			msg = reason
		}
		return deny(msg), nil
	}

	if len(tool.Force) == 0 {
		return pass(), nil
	}
	mutated, err := buildMutatedParams(cp, wrapped, tool.Force)
	if err != nil {
		// A shim-side malfunction (e.g. the tool's own args aren't marshalable) —
		// fail closed rather than forward an un-mutated, non-compliant call.
		return deny(fmt.Sprintf("force-override eval: %v", err)), nil
	}
	return mutate(mutated), nil
}

// buildMutatedParams applies literal force-overrides to cp.Arguments and
// re-serializes the tools/call params in the same shape the request arrived
// in (re-wrapped into call_tool{tool_name,parameters} if it came in that way).
func buildMutatedParams(cp callParams, wrapped bool, force map[string]any) ([]byte, error) {
	for k, v := range force {
		cp.Arguments[k] = v
	}
	if wrapped {
		return json.Marshal(map[string]any{
			"name":      callToolMeta,
			"arguments": map[string]any{"tool_name": cp.Name, "parameters": cp.Arguments},
		})
	}
	return json.Marshal(map[string]any{"name": cp.Name, "arguments": cp.Arguments})
}

// checkNotionAncestry returns nil to allow the update-page call through to
// Cerbos, or an error (used verbatim as the deny reason) to block it. Every
// failure path is fail-closed: an unconfigured gate, a missing page_id, a
// lookup error, and a confirmed not-under-Scratchpad all deny.
func (s *Server) checkNotionAncestry(ctx context.Context, pageID string) error {
	if s.notionAncestry == nil || s.notionScratchpadID == "" {
		// The gate is mandatory for this tool: production always wires it
		// (main.go). Reaching here unconfigured means a broken deploy, not a
		// license to allow an unscoped page edit.
		return fmt.Errorf("notion ancestry gate not configured; denying update to page %q", pageID)
	}
	if pageID == "" {
		return fmt.Errorf("notion update-page has no page_id; cannot verify Scratchpad ancestry")
	}
	ctx, cancel := context.WithTimeout(ctx, notionAncestryTimeout)
	defer cancel()
	under, err := upstream.PageIsUnderAncestor(ctx, s.notionAncestry, pageID, s.notionScratchpadID)
	if err != nil {
		return fmt.Errorf("could not verify this Notion page is under the agent's Scratchpad tree (failing closed): %v", err)
	}
	if !under {
		return fmt.Errorf("this agent may only update Notion pages under its Scratchpad page tree; page %q is not, so the update is denied", pageID)
	}
	return nil
}

// resolveBackend enforces exactly-one mapped backend in service_names.
func (s *Server) resolveBackend(names []string) (string, error) {
	if len(names) != 1 {
		return "", fmt.Errorf("expected exactly one service name, got %d", len(names))
	}
	name := names[0]
	if _, ok := s.mapping.Backends[name]; !ok {
		return "", fmt.Errorf("backend %q not mapped", name)
	}
	return name, nil
}

// pass returns a clean allow with NO side-effect channels set.
func pass() *pb.McpRequestResult {
	return &pb.McpRequestResult{Result: &pb.McpRequestResult_Pass{Pass: &pb.Pass{}}}
}

// deny returns a PERMISSION_DENIED AuthorizationError with NO side-effect channels.
func deny(reason string) *pb.McpRequestResult {
	return &pb.McpRequestResult{
		Result: &pb.McpRequestResult_Error{
			Error: &pb.AuthorizationError{
				Code:   pb.AuthorizationError_PERMISSION_DENIED,
				Reason: reason,
			},
		},
	}
}

// mutate replaces the JSON-RPC params before the gateway forwards the call
// upstream. Only reached after Cerbos has already allowed the (unmutated)
// call, so the resource checked and the resource forwarded always agree on
// owner/repo/branch — only literal force-override keys (e.g. draft) change.
func mutate(params []byte) *pb.McpRequestResult {
	return &pb.McpRequestResult{Result: &pb.McpRequestResult_Mutated{Mutated: params}}
}

// CheckResponse is stubbed for v1: always Pass with no mutation.
func (s *Server) CheckResponse(ctx context.Context, _ *pb.McpResponse) (*pb.McpResponseResult, error) {
	return &pb.McpResponseResult{Result: &pb.McpResponseResult_Pass{Pass: &pb.Pass{}}}, nil
}

// Compile-time guard: gRPC-level errors are gateway transport failures, not denies.
var _ = status.Errorf
var _ = codes.OK
