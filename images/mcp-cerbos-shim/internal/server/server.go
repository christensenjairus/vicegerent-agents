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

// The Notion existing-page-write ancestry gate keys off the mapped resource,
// not the tool-name string, so renaming a tool in mapping.yaml keeps the gate
// intact as long as it keeps one of these resourceType/action pairs (matches
// the rules in defs/resource_notion.yaml). notion-create-pages is NOT one of
// these -- it's still pinned to Scratchpad-only via its own Cerbos deny rule
// (deny-create-outside-scratchpad), a narrower and separate policy from this
// multi-parent allowlist for calls that target an EXISTING page.
const (
	notionPageResource  = "notion_page"
	notionUpdateAction  = "update"
	notionCommentAction = "comment"
)

// notionAncestryGatedActions is the set of notion_page actions the ancestry
// gate applies to -- every write against an EXISTING page by id. create-pages
// deliberately isn't here (see comment above).
var notionAncestryGatedActions = map[string]bool{
	notionUpdateAction:  true,
	notionCommentAction: true,
}

// linearSaveCommentTool and linearTeamResource identify the one call the
// HAH-69 team-resolution gate applies to: linear_save_comment mapped to
// linear_team/access (mapping.yaml). Keying off the tool name (not just the
// resource/action pair, unlike the Notion gate above) because save_issue
// shares the exact same resourceType/action and must NOT be re-resolved --
// its own teamId is already populated directly from the call's own `team`
// arg by linearIssueAttr, a genuinely different, already-verifiable signal.
const (
	linearSaveCommentTool = "linear_save_comment"
	linearTeamResource    = "linear_team"
)

// upstreamLookupTimeout bounds a single live shim->vMCP lookup call (Notion
// ancestry, Linear issue-team resolution) so one gated tools/call can't hang
// the whole CheckRequest (the gateway is FailClosed, so a hang would deny
// anyway — but only after its own longer timeout, holding the connection
// open meanwhile).
const upstreamLookupTimeout = 5 * time.Second

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

	// notionAncestry, when set, gates every existing-page Notion write
	// (update-page, create-comment) to pages under one of
	// notionAllowedParentIDs via a live notion-fetch lookup — a network round
	// trip the CEL/Cerbos path can't make (it's pure/synchronous, no I/O). It
	// lives on Server rather than in a CEL helper for that reason.
	// notionAllowedParentIDs is a caller-scoped allowlist of parent folders
	// (e.g. Scratchpad plus a set of team folders — HAH's multi-parent
	// scoping); a page passes the gate if it descends from ANY of them.
	// notion-create-pages is NOT covered by this list — it stays pinned to
	// Scratchpad-only via its own, narrower Cerbos deny rule.
	notionAncestry         upstream.ToolCaller
	notionAllowedParentIDs []string

	// linearIssueTeam, when set, resolves a Linear save_comment call's
	// issueId to its team via a live linear_get_issue lookup -- a network
	// round trip the CEL/Cerbos path can't make, same rationale as
	// notionAncestry above. Unlike the Notion gate this doesn't deny
	// directly: it injects the resolved team into the resource's teamId attr
	// so Cerbos's existing deny-non-devops-team rule (resource_linear.yaml)
	// evaluates it exactly like a save_issue call, with zero duplication of
	// the allowlist.
	linearIssueTeam upstream.ToolCaller
}

// Option configures a Server at construction. Variadic so existing four-arg
// New callers (tests, and any backend that doesn't need the ancestry gate)
// keep compiling unchanged.
type Option func(*Server)

// WithNotionAncestry enables the Notion existing-page-write ancestry gate.
// client resolves a page's ancestors (production: an upstream.Client to vMCP;
// tests: a stub); allowedParentIDs is the set of parent folders a page must
// descend from ANY one of to pass (Scratchpad plus any additional team
// folders the caller configures — the caller is responsible for including
// Scratchpad in this list if it should remain allowed).
func WithNotionAncestry(client upstream.ToolCaller, allowedParentIDs []string) Option {
	return func(s *Server) {
		s.notionAncestry = client
		s.notionAllowedParentIDs = allowedParentIDs
	}
}

// WithLinearIssueTeam enables the Linear save_comment issueId->team
// resolution gate (HAH-69). client resolves an issue id to its team
// (production: an upstream.Client to vMCP; tests: a stub).
func WithLinearIssueTeam(client upstream.ToolCaller) Option {
	return func(s *Server) {
		s.linearIssueTeam = client
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

	// Notion existing-page-write gate: this runs BEFORE Cerbos (a live
	// ancestry lookup Cerbos itself can't do) and denies any write to an
	// existing page (update-page, create-comment) outside the allowed parent
	// folders. The Cerbos policy still independently blocks destructive
	// update-page commands (replace_content / allow_deleting_content) on the
	// pages that DO pass this gate; the two checks are complementary, not
	// redundant.
	if res.ResourceType == notionPageResource && notionAncestryGatedActions[res.Action] {
		if derr := s.checkNotionAncestry(ctx, res.ID); derr != nil {
			log.Printf("deny: notion %s ancestry (page=%q backend=%s): %v", res.Action, res.ID, backend, derr)
			return deny(derr.Error()), nil
		}
	}

	// Linear save_comment team-resolution gate (HAH-69): this runs BEFORE
	// Cerbos and, unlike the Notion gate above, doesn't deny directly -- it
	// resolves issueId to its team via a live lookup and injects that team
	// into res.Attr's teamId key, so Cerbos's existing deny-non-devops-team
	// rule (resource_linear.yaml) evaluates this exactly like a save_issue
	// call. save_issue itself is untouched (its own teamId is already
	// populated by linearIssueAttr in mapping.yaml); this only fires for
	// linear_save_comment, and only when the call has an issueId to resolve
	// (a comment on a project/initiative/document/milestone, or a reply via
	// parentId with no entity ref, has nothing to resolve and passes
	// unchecked -- same fail-open-when-unverifiable posture as save_project's
	// linearProjectAttr helper). Gated on the issueId ATTR, not res.ID --
	// res.ID falls back to "*" when issueId is absent (mapping.yaml), same
	// non-empty-id convention save_project's id: get(args,'id','*') uses,
	// since Cerbos itself rejects an empty resource.id before policy ever
	// runs.
	if cp.Name == linearSaveCommentTool && res.ResourceType == linearTeamResource {
		issueID, _ := res.Attr["issueId"].(string)
		if issueID != "" {
			team, derr := s.checkLinearIssueTeam(ctx, issueID)
			if derr != nil {
				log.Printf("deny: linear save_comment team lookup (issue=%q backend=%s): %v", issueID, backend, derr)
				return deny(derr.Error()), nil
			}
			res.Attr["teamId"] = team
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

// checkNotionAncestry returns nil to allow the existing-page-write call
// through to Cerbos, or an error (used verbatim as the deny reason) to block
// it. Every failure path is fail-closed: an unconfigured gate, a missing
// page_id, a lookup error, and a confirmed not-under-any-allowed-parent all
// deny.
func (s *Server) checkNotionAncestry(ctx context.Context, pageID string) error {
	if s.notionAncestry == nil || len(s.notionAllowedParentIDs) == 0 {
		// The gate is mandatory for these tools: production always wires it
		// (main.go). Reaching here unconfigured means a broken deploy, not a
		// license to allow an unscoped page edit.
		return fmt.Errorf("notion ancestry gate not configured; denying write to page %q", pageID)
	}
	if pageID == "" {
		return fmt.Errorf("notion call has no page_id; cannot verify allowed-parent ancestry")
	}
	ctx, cancel := context.WithTimeout(ctx, upstreamLookupTimeout)
	defer cancel()
	under, err := upstream.PageIsUnderAnyAncestor(ctx, s.notionAncestry, pageID, s.notionAllowedParentIDs)
	if err != nil {
		return fmt.Errorf("could not verify this Notion page is under an allowed parent folder (failing closed): %v", err)
	}
	if !under {
		return fmt.Errorf("this agent may only write to Notion pages under its allowed parent folders; page %q is not, so the write is denied", pageID)
	}
	return nil
}

// checkLinearIssueTeam resolves issueID to its team via a live lookup, or
// returns an error (used verbatim as the deny reason) on any failure --
// fail-closed contract mirrors checkNotionAncestry above: an unconfigured
// gate or a lookup error both deny rather than silently allow-through with
// no teamId attr (which would let the call skip Cerbos's team check
// entirely, the exact hole HAH-69 closes).
func (s *Server) checkLinearIssueTeam(ctx context.Context, issueID string) (string, error) {
	if s.linearIssueTeam == nil {
		return "", fmt.Errorf("linear issue-team gate not configured; denying save_comment for issue %q", issueID)
	}
	ctx, cancel := context.WithTimeout(ctx, upstreamLookupTimeout)
	defer cancel()
	team, err := upstream.IssueTeam(ctx, s.linearIssueTeam, issueID)
	if err != nil {
		return "", fmt.Errorf("could not verify this Linear issue's team (failing closed): %v", err)
	}
	return team, nil
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
