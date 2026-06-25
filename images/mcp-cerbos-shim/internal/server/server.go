// Package server implements the agentgateway ExtMcp gRPC service, translating
// each MCP tool call into a Cerbos authorization decision.
//
// Security contract (fail closed everywhere):
//   - Only "tools/call" is evaluated at the request phase. Any other invoked
//     method on a deny-default backend is denied (closes hook-broadening drift).
//   - Unparseable params, unmapped backend/tool, CEL eval errors, unmet required
//     attrs, unknown kind, and Cerbos errors ALL deny.
//   - Responses set ONLY pass or error: mutated, header_mutation, and metadata
//     are always left absent (asserted by tests) so no side-effect channel leaks.
package server

import (
	"context"
	"encoding/json"
	"fmt"
	"log"

	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	config "github.com/jchristensen/vicegerent-agents/images/mcp-cerbos-shim/internal"
	"github.com/jchristensen/vicegerent-agents/images/mcp-cerbos-shim/internal/authz"
	"github.com/jchristensen/vicegerent-agents/images/mcp-cerbos-shim/internal/eval"
	pb "github.com/jchristensen/vicegerent-agents/images/mcp-cerbos-shim/proto/gen"
)

const toolsCall = "tools/call"

// denyMessage is the generic, backend-agnostic reason returned to the MCP
// client on a policy deny. It deliberately omits the resource/action so it is
// safe for any MCP backend and does not leak what was probed. It signals that
// this is a deliberate policy refusal (not a tool malfunction) so the client
// does not abandon the MCP. The structured detail (action + resource type)
// goes to the shim's own log for operators.
const denyMessage = "Access denied by security policy. This is an intentional restriction, not a tool error — try a different resource or action."

// Principal is the fixed identity stamped on every Cerbos request. The policy
// allows all roles and denies only by resource (Secrets) — identity is NOT a
// control here (one agent per sandbox; the namespace/network lock is the
// identity boundary). Cerbos still requires a principal + role in the request,
// so this is a constant for audit/log context, not authorization.
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
}

// New constructs a Server. The engine must already be compiled from mapping.
func New(m *config.Mapping, e *eval.Engine, d authz.Decider, p Principal) *Server {
	return &Server{mapping: m, engine: e, decider: d, principal: p}
}

// callParams is the tools/call params shape (rmcp CallToolRequestParam).
type callParams struct {
	Name      string         `json:"name"`
	Arguments map[string]any `json:"arguments"`
}

// CheckRequest is the pre-forward gate. It returns Pass{} to allow, or an
// AuthorizationError to deny. It never sets mutated/header_mutation/metadata.
func (s *Server) CheckRequest(ctx context.Context, req *pb.McpRequest) (*pb.McpRequestResult, error) {
	backend, derr := s.resolveBackend(req.GetServiceNames())
	if derr != nil {
		return deny(derr.Error()), nil
	}

	b := s.mapping.Backends[backend]

	// Method gate (deny-default aware): only tools/call is handled at the
	// request phase. Any other invoked method denies on a deny-default backend.
	if req.GetMethod() != toolsCall {
		if b.DefaultAction == config.ActionDeny {
			return deny(fmt.Sprintf("method %q not handled on deny-default backend %q", req.GetMethod(), backend)), nil
		}
		return pass(), nil
	}

	// Parse params. Unparseable/missing → deny (do not trust gateway FailClosed
	// for our own parse failures).
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

	// Unmapped tool → defaultAction.
	if _, ok := b.Tools[cp.Name]; !ok {
		if b.DefaultAction == config.ActionDeny {
			return deny(fmt.Sprintf("tool %q not mapped on deny-default backend %q", cp.Name, backend)), nil
		}
		return pass(), nil
	}

	// Evaluate the mapping → Cerbos resource. Any eval error / unmet required
	// attr returns *eval.ErrDeny.
	res, err := s.engine.Eval(eval.CallInput{
		Tool: cp.Name, Backend: backend, Method: req.GetMethod(), Args: cp.Arguments,
	})
	if err != nil {
		return deny(fmt.Sprintf("policy input eval: %v", err)), nil
	}

	allowed, err := s.decider.IsAllowed(ctx,
		s.principal.ID, s.principal.Roles,
		res.ResourceType, res.ID, res.Attr, res.Action)
	if err != nil {
		// Cerbos unreachable / error → deny (gateway FailClosed is a backstop).
		return deny(fmt.Sprintf("authorization check failed: %v", err)), nil
	}
	if !allowed {
		// Operator detail to our own log; generic message to the client.
		log.Printf("deny: %s on %s (tool=%s backend=%s)", res.Action, res.ResourceType, cp.Name, backend)
		return deny(denyMessage), nil
	}
	return pass(), nil
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

// CheckResponse is stubbed for v1: always Pass with no mutation. Response-phase
// filtering/redaction is a documented follow-up.
func (s *Server) CheckResponse(ctx context.Context, _ *pb.McpResponse) (*pb.McpResponseResult, error) {
	return &pb.McpResponseResult{Result: &pb.McpResponseResult_Pass{Pass: &pb.Pass{}}}, nil
}

// guard against accidentally returning a gRPC-level error (which the gateway
// treats as a transport failure, not a deny). All decisions are in-band.
var _ = status.Errorf
var _ = codes.OK
