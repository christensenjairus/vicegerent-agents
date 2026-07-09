// Package server implements the agentgateway ExtMcp gRPC service.
// Fail-closed contract: only tools/call is evaluated for Cerbos authz; bad
// params/mapping/eval/Cerbos errors deny. Responses are pass or error, except
// a tool with a mapping `force` set, which allows via a mutated
// (rewritten-args) result instead of a bare pass — never on a denied call.
// resources/read and prompts/get responses also pass through secret
// redaction (redactableResponseMethods) even though they carry no Cerbos
// authz of their own (HAH-101) — see the resourcesRead/promptsGet doc
// comment below for why authz and redaction diverge for these two methods.
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

// resources/read and prompts/get carry response bodies that can contain
// secret-shaped strings just like a tools/call result, so both are routed
// through CheckResponse's redaction path (HAH-101). Neither carries an
// authorizable resource/action pair the way tools/call does -- no mapping
// entry exists to build a Cerbos resource from a resource URI or prompt
// name -- so CheckRequest still only evaluates Cerbos authz for tools/call;
// these two just get the secret-redaction pass on their way out.
const resourcesRead = "resources/read"
const promptsGet = "prompts/get"

// redactableResponseMethods are the JSON-RPC methods whose response bodies
// CheckResponse scrubs for secret-shaped values. tools/call is the original
// (and only fully-authorized) member; resources/read and prompts/get were
// added by HAH-101 to close the redaction gap those methods previously had
// -- CheckResponse used to no-op unconditionally for anything but
// tools/call, so a resource/prompt response never got scrubbed at all.
var redactableResponseMethods = map[string]bool{
	toolsCall:     true,
	resourcesRead: true,
	promptsGet:    true,
}

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

// linearSaveCommentTool, linearSaveIssueTool, linearSaveProjectTool, and
// linearTeamResource identify the Linear write calls the team-resolution
// gates apply to, all mapped to linear_team/access (mapping.yaml). Keying
// off the tool name (not just the resource/action pair, unlike the Notion
// gate above) because all three tools share that exact same
// resourceType/action, and each needs different resolution logic:
//   - linear_save_comment: never carries a team of its own --
//     always resolved via issueId lookup when issueId is set.
//   - linear_save_issue: an EXPLICIT `team` arg (create, or a
//     deliberate update reassignment) is already a directly-verifiable
//     signal populated by linearIssueAttr and must NOT be re-resolved or
//     overridden. Only an UPDATE call that omits `team` entirely gets a
//     lookup here, resolving the issue's CURRENT team by its `id` -- this
//     closes the gap where an ordinary field edit on an out-of-allowlist-team
//     issue previously fell through to allow-all with no teamId attr at all.
//   - linear_save_project: same shape as save_issue -- an explicit
//     addTeams/setTeams arg is already verifiable via linearProjectAttr and
//     is never re-resolved; only an update that sets NEITHER gets a lookup
//     here, resolving the project's CURRENT teams by its `id`.
const (
	linearSaveCommentTool = "linear_save_comment"
	linearSaveIssueTool   = "linear_save_issue"
	linearSaveProjectTool = "linear_save_project"
	linearTeamResource    = "linear_team"
)

// pagerdutyManageIncidentsTools/pagerdutyAddNoteTools/pagerdutyIncidentResource
// identify the PagerDuty write calls the service-resolution gate applies to,
// one entry per backend this shim fronts (toolhive-servers.json: pagerduty,
// pagerduty_gov). Unlike the Linear gates above, neither tool's own args
// carry ANYTHING that identifies the incident's owning service directly --
// only an opaque incident_id/incident_ids. The gate resolves each targeted
// incident to its service via a live get_incident lookup and hands the
// resolved id(s) to Cerbos's existing service allowlist rule
// (resource_pagerduty.yaml), the same handoff pattern as the Linear
// issue/project team gates.
//
// Each map's value is that SAME backend's own get_incident tool name -- the
// live lookup must query the backend the incident actually lives in, not
// always the first-registered one, or every gov call fails closed with a
// "not found" from looking the incident up in the wrong PagerDuty account
// (get_incident itself stays unmapped in Cerbos for both backends, for the
// same recursion-safety reason notion_notion-fetch/linear_get_issue are
// documented elsewhere in this shim: a deny rule on it would make every
// manage_incidents/add_note_to_incident lookup fail closed unconditionally
// instead of the intended per-call, service-scoping-tied check).
var (
	pagerdutyManageIncidentsTools = map[string]string{
		"pagerduty_manage_incidents":     "pagerduty_get_incident",
		"pagerduty_gov_manage_incidents": "pagerduty_gov_get_incident",
	}
	pagerdutyAddNoteTools = map[string]string{
		"pagerduty_add_note_to_incident":     "pagerduty_get_incident",
		"pagerduty_gov_add_note_to_incident": "pagerduty_gov_get_incident",
	}
)

const pagerdutyIncidentResource = "pagerduty_incident"

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
// over relying on this generic string: without a specific
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

	// linearIssueTeam, when set, resolves a Linear issueId/id to its current
	// team via a live linear_get_issue lookup -- a network round trip the
	// CEL/Cerbos path can't make, same rationale as notionAncestry above.
	// Used by: save_comment (always), and save_issue UPDATE calls
	// that omit an explicit `team` arg (an explicit team is
	// already resolved directly by linearIssueAttr and never re-looked-up
	// here). Unlike the Notion gate this doesn't deny directly: it injects
	// the resolved team into the resource's teamId attr so Cerbos's existing
	// deny-non-devops-team rule (resource_linear.yaml) evaluates it exactly
	// like an explicit-team call, with zero duplication of the allowlist.
	linearIssueTeam upstream.ToolCaller

	// linearProjectTeam, when set, resolves a Linear project id to its
	// CURRENT team(s) via a live linear_get_project lookup, same rationale
	// as linearIssueTeam above. Used only by save_project UPDATE calls that
	// set neither addTeams nor setTeams -- a call that sets either
	// is already resolved directly by linearProjectAttr and never
	// re-looked-up here. Injects the resolved teams into the resource's
	// teams attr so Cerbos's existing deny-non-devops-project-teams rule
	// evaluates it exactly like an explicit addTeams/setTeams call.
	linearProjectTeam upstream.ToolCaller

	// pagerdutyIncidentService, when set, resolves EVERY incident id a
	// manage_incidents/add_note_to_incident call targets to its owning
	// service via a live pagerduty_get_incident lookup -- neither
	// tool's own args carry a service/team identifier at all, only an
	// opaque incident_id/incident_ids, so there is nothing for a CEL helper
	// to check without this network round trip, same rationale as
	// notionAncestry/linearIssueTeam above. Injects the resolved service
	// id(s) into the resource's serviceIds attr so Cerbos's
	// deny-write-outside-allowed-services rule (resource_pagerduty.yaml)
	// evaluates it exactly like an explicit-service call.
	pagerdutyIncidentService upstream.ToolCaller
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

// WithLinearIssueTeam enables the Linear issue team-resolution gate: always
// for save_comment, and for save_issue UPDATE calls that omit an
// explicit `team` arg. client resolves an issue id to its current
// team (production: an upstream.Client to vMCP; tests: a stub).
func WithLinearIssueTeam(client upstream.ToolCaller) Option {
	return func(s *Server) {
		s.linearIssueTeam = client
	}
}

// WithLinearProjectTeam enables the Linear save_project UPDATE team-
// resolution gate: fires only when the call sets neither addTeams
// nor setTeams. client resolves a project id to its current team(s)
// (production: an upstream.Client to vMCP; tests: a stub).
func WithLinearProjectTeam(client upstream.ToolCaller) Option {
	return func(s *Server) {
		s.linearProjectTeam = client
	}
}

// WithPagerdutyIncidentService enables the PagerDuty incident service-
// resolution gate: every manage_incidents/add_note_to_incident
// call has each targeted incident id resolved to its owning service via a
// live lookup. client resolves an incident id to its service id
// (production: an upstream.Client to vMCP; tests: a stub).
func WithPagerdutyIncidentService(client upstream.ToolCaller) Option {
	return func(s *Server) {
		s.pagerdutyIncidentService = client
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

	// Cerbos authz is only evaluated for tools/call -- no mapping entry
	// exists to build a Cerbos resource from a resources/read URI or a
	// prompts/get name (see resourcesRead/promptsGet comment above), so
	// there's nothing to check for either. Both still get their RESPONSE
	// bodies scrubbed by CheckResponse (redactableResponseMethods); this
	// gate only concerns request-side authz + argument redaction.
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

	// Linear save_comment team-resolution gate: this runs BEFORE
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

	// Linear save_issue UPDATE team-resolution gate: closes the gap
	// where a plain field edit on an existing issue (no `team` arg at all)
	// fell through to allow-all regardless of the issue's REAL team, since
	// linearIssueAttr only surfaces teamId when the call itself sets `team`.
	// Fires only when: (a) this is save_issue, (b) the call is an update
	// (has an `id` arg -- res.ID is that same id per mapping.yaml's
	// `id: get(args,'id', get(args,'team',''))`), and (c) attr already has
	// NO teamId key, meaning the call didn't set `team` itself (an explicit
	// team, create or update, is linearIssueAttr's own directly-verifiable
	// signal and must never be overridden by a lookup here). A create call
	// always sets `team`+has no `id`, so it never reaches this branch.
	if cp.Name == linearSaveIssueTool && res.ResourceType == linearTeamResource {
		if _, hasTeam := res.Attr["teamId"]; !hasTeam {
			if issueID, _ := cp.Arguments["id"].(string); issueID != "" {
				team, derr := s.checkLinearIssueTeam(ctx, issueID)
				if derr != nil {
					log.Printf("deny: linear save_issue update team lookup (issue=%q backend=%s): %v", issueID, backend, derr)
					return deny(derr.Error()), nil
				}
				res.Attr["teamId"] = team
			}
		}
	}

	// Linear save_project UPDATE team-resolution gate: same shape
	// as the save_issue gate above -- closes the gap where a plain project
	// field edit (no addTeams/setTeams) fell through to allow-all regardless
	// of the project's REAL team(s), since linearProjectAttr only surfaces
	// a `teams` attr when the call itself sets one of those args. Fires only
	// when: (a) this is save_project, (b) the call is an update (has an `id`
	// arg), and (c) attr has NO teams key, meaning neither addTeams nor
	// setTeams was set (an explicit reassignment is linearProjectAttr's own
	// directly-verifiable signal and must never be overridden here). A
	// create call always sets one of addTeams/setTeams (Linear requires at
	// least one team on project creation) and has no `id`, so it never
	// reaches this branch.
	if cp.Name == linearSaveProjectTool && res.ResourceType == linearTeamResource {
		if _, hasTeams := res.Attr["teams"]; !hasTeams {
			if projectID, _ := cp.Arguments["id"].(string); projectID != "" {
				teams, derr := s.checkLinearProjectTeam(ctx, projectID)
				if derr != nil {
					log.Printf("deny: linear save_project update team lookup (project=%q backend=%s): %v", projectID, backend, derr)
					return deny(derr.Error()), nil
				}
				res.Attr["teams"] = teams
			}
		}
	}

	// PagerDuty incident service-resolution gate: this runs BEFORE
	// Cerbos and, like the Linear team gates above, doesn't deny directly --
	// it resolves every incident id the call targets to its owning service
	// via a live lookup and injects the resolved service id(s) into the
	// resource's serviceIds attr, so Cerbos's deny-write-outside-allowed-
	// services rule (resource_pagerduty.yaml) evaluates it exactly like an
	// explicit-service call. manage_incidents carries incident_ids (an
	// array, since it's a bulk-update tool); add_note_to_incident carries a
	// single incident_id. Both are handled the same way: resolve every
	// non-empty id, fail closed on ANY lookup error (a partially-resolved
	// batch is not a safe signal to check against an allowlist).
	getIncidentTool, pagerdutyGated := pagerdutyManageIncidentsTools[cp.Name]
	if !pagerdutyGated {
		getIncidentTool, pagerdutyGated = pagerdutyAddNoteTools[cp.Name]
	}
	if res.ResourceType == pagerdutyIncidentResource && pagerdutyGated {
		incidentIDs := pagerdutyIncidentIDsFromArgs(cp.Name, cp.Arguments)
		if len(incidentIDs) > 0 {
			serviceIDs, derr := s.checkPagerdutyIncidentServices(ctx, getIncidentTool, incidentIDs)
			if derr != nil {
				log.Printf("deny: pagerduty %s service lookup (incidents=%v backend=%s): %v", cp.Name, incidentIDs, backend, derr)
				return deny(derr.Error()), nil
			}
			res.Attr["serviceIds"] = serviceIDs
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

	// Secret redaction: scrub credential-shaped strings out of the call's
	// arguments before it ever reaches vMCP -- see secrets_redact.go for why
	// this has to happen here and not in the egress-proxy. Runs on every
	// allowed call regardless of Force, since a tool with no force-override
	// can still carry a secret in one of its own arguments. Redaction never
	// denies (a pattern match on an otherwise-legitimate call shouldn't
	// break it) -- only rewrites via the same mutate() path Force already
	// uses.
	redactedArgs, redactedCount := redactArguments(cp.Arguments)
	if redactedCount > 0 {
		log.Printf("redact: %d secret-shaped value(s) scrubbed from %s args (backend=%s)", redactedCount, cp.Name, backend)
		cp.Arguments = redactedArgs
	}

	if len(tool.Force) == 0 && redactedCount == 0 {
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
		return marshalNoHTMLEscape(map[string]any{
			"name":      callToolMeta,
			"arguments": map[string]any{"tool_name": cp.Name, "parameters": cp.Arguments},
		})
	}
	return marshalNoHTMLEscape(map[string]any{"name": cp.Name, "arguments": cp.Arguments})
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
// entirely, the exact hole this gate closes).
func (s *Server) checkLinearIssueTeam(ctx context.Context, issueID string) (string, error) {
	if s.linearIssueTeam == nil {
		return "", fmt.Errorf("linear issue-team gate not configured; denying write for issue %q", issueID)
	}
	ctx, cancel := context.WithTimeout(ctx, upstreamLookupTimeout)
	defer cancel()
	team, err := upstream.IssueTeam(ctx, s.linearIssueTeam, issueID)
	if err != nil {
		return "", fmt.Errorf("could not verify this Linear issue's team (failing closed): %v", err)
	}
	return team, nil
}

// checkLinearProjectTeam resolves projectID to its current team(s) via a
// live lookup, or returns an error (used verbatim as the deny reason) on any
// failure -- fail-closed contract mirrors checkLinearIssueTeam/
// checkNotionAncestry above: an unconfigured gate or a lookup error both
// deny rather than silently allow-through with no teams attr (which would
// let the call skip Cerbos's team check entirely, the exact hole this gate
// closes for save_project updates).
func (s *Server) checkLinearProjectTeam(ctx context.Context, projectID string) ([]string, error) {
	if s.linearProjectTeam == nil {
		return nil, fmt.Errorf("linear project-team gate not configured; denying update for project %q", projectID)
	}
	ctx, cancel := context.WithTimeout(ctx, upstreamLookupTimeout)
	defer cancel()
	teams, err := upstream.ProjectTeams(ctx, s.linearProjectTeam, projectID)
	if err != nil {
		return nil, fmt.Errorf("could not verify this Linear project's team(s) (failing closed): %v", err)
	}
	return teams, nil
}

// pagerdutyIncidentIDsFromArgs extracts every incident id a
// manage_incidents/add_note_to_incident call targets directly from the raw
// tool arguments (not res.Attr/res.ID, since manage_incidents' id/attr shape
// is a fixed literal "'manage_incidents'" per mapping.yaml, not the actual
// incident_ids -- see resource_pagerduty.yaml's own comment on why no
// bulk-size cap exists there). manage_incidents carries a nested
// manage_request.incident_ids array; add_note_to_incident carries a single
// top-level incident_id string. Non-string array elements are skipped
// (better to check what's checkable than fail the whole call over one
// malformed entry, same posture as lookupCIStringSlice elsewhere in this
// shim).
func pagerdutyIncidentIDsFromArgs(toolName string, args map[string]any) []string {
	if _, ok := pagerdutyManageIncidentsTools[toolName]; ok {
		req, _ := args["manage_request"].(map[string]any)
		ids, _ := req["incident_ids"].([]any)
		out := make([]string, 0, len(ids))
		for _, id := range ids {
			if s, ok := id.(string); ok && s != "" {
				out = append(out, s)
			}
		}
		return out
	}
	if _, ok := pagerdutyAddNoteTools[toolName]; ok {
		if id, ok := args["incident_id"].(string); ok && id != "" {
			return []string{id}
		}
	}
	return nil
}

// checkPagerdutyIncidentServices resolves every incidentID to its owning
// service id via a live lookup per id, or returns an error (used verbatim
// as the deny reason) on ANY single failure -- fail-closed contract mirrors
// checkLinearIssueTeam/checkLinearProjectTeam above: an unconfigured gate,
// or any one incident's lookup failing, denies the WHOLE call rather than
// silently checking only the incidents that happened to resolve (a
// partially-resolved batch is not a safe signal to check against an
// allowlist -- see resource_pagerduty.yaml's own no-bulk-cap rationale for
// why a batch call must be treated as a single unit here, not per-incident).
func (s *Server) checkPagerdutyIncidentServices(ctx context.Context, getIncidentTool string, incidentIDs []string) ([]string, error) {
	if s.pagerdutyIncidentService == nil {
		return nil, fmt.Errorf("pagerduty incident-service gate not configured; denying write for incidents %v", incidentIDs)
	}
	serviceIDs := make([]string, 0, len(incidentIDs))
	for _, id := range incidentIDs {
		lookupCtx, cancel := context.WithTimeout(ctx, upstreamLookupTimeout)
		serviceID, err := upstream.IncidentServiceID(lookupCtx, getIncidentTool, s.pagerdutyIncidentService, id)
		cancel()
		if err != nil {
			return nil, fmt.Errorf("could not verify PagerDuty incident %q's owning service (failing closed): %v", id, err)
		}
		serviceIDs = append(serviceIDs, serviceID)
	}
	return serviceIDs, nil
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

// responsePass returns a clean allow with no mutation, for CheckResponse.
func responsePass() *pb.McpResponseResult {
	return &pb.McpResponseResult{Result: &pb.McpResponseResult_Pass{Pass: &pb.Pass{}}}
}

// responseMutate replaces the JSON-RPC result before it reaches the model,
// mirroring mutate()'s request-side contract: must parse as a valid result
// for the method, or the gateway treats it as a protocol violation.
func responseMutate(result []byte) *pb.McpResponseResult {
	return &pb.McpResponseResult{Result: &pb.McpResponseResult_Mutated{Mutated: result}}
}

// CheckResponse scrubs credential-shaped strings out of a tool's RESULT
// before it reaches the model -- the response-side half of the redaction
// gap secrets_redact.go documents. Only tools/call responses carry
// meaningful content to scrub (other methods, and the empty/unparseable
// case, pass through unmutated). Redaction failures never deny -- a
// response that can't be parsed/re-encoded passes through as-is rather
// than breaking an otherwise-successful tool call; this is a
// best-effort, defense-in-depth layer, not a hard boundary (see
// secrets_redact.go's doc comment on why deny is never the right response
// here).
func (s *Server) CheckResponse(ctx context.Context, resp *pb.McpResponse) (*pb.McpResponseResult, error) {
	if !redactableResponseMethods[resp.GetMethod()] {
		return responsePass(), nil
	}
	raw := resp.GetMcpResponse()
	if len(raw) == 0 {
		return responsePass(), nil
	}
	redacted, n := redactRawJSON(raw)
	if n == 0 {
		return responsePass(), nil
	}
	log.Printf("redact: %d secret-shaped value(s) scrubbed from a tool result (backend=%v)", n, resp.GetServiceNames())
	return responseMutate(redacted), nil
}

// Compile-time guard: gRPC-level errors are gateway transport failures, not denies.
var _ = status.Errorf
var _ = codes.OK
