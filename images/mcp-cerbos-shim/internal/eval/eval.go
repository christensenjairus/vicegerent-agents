// Package eval compiles the mapping's CEL expressions and evaluates them per
// call. It standardizes a tool call into a Cerbos resource (kind/apiResource/
// namespace/action); it does not make policy decisions. Error paths here deny
// only on the shim's own malfunction (CEL eval failure, malformed result); a
// half-built resource must never reach Cerbos. The policy denies (Secrets, and
// the empty-kind ambiguity) are made by Cerbos, not here.
package eval

import (
	"fmt"
	"reflect"

	"github.com/google/cel-go/cel"
	"github.com/google/cel-go/common/types"
	"github.com/google/cel-go/common/types/ref"

	config "github.com/jchristensen/vicegerent-agents/images/mcp-cerbos-shim/internal"
)

// CallInput is the data in scope for a tool call's CEL expressions.
type CallInput struct {
	Tool    string
	Backend string
	Method  string
	Args    map[string]any
}

// Resource is the evaluated Cerbos request shape for one call.
type Resource struct {
	ResourceType string
	Action       string
	ID           string
	Attr         map[string]any
}

// compiledTool holds the type-checked programs for one tool.
type compiledTool struct {
	resourceType string
	action       string
	idProg       cel.Program
	attrProgs    map[string]cel.Program
	attrFromProg cel.Program // optional; yields map<string,string>
}

// Engine holds compiled programs for every (backend, tool) in the mapping.
type Engine struct {
	tools map[string]map[string]*compiledTool // backend -> tool -> compiled
}

// Compile builds and type-checks every expression in the mapping. Any failure
// returns an error so the process refuses to start (fail closed).
func Compile(m *config.Mapping) (*Engine, error) {
	e := &Engine{tools: map[string]map[string]*compiledTool{}}
	for bname, b := range m.Backends {
		env, err := newEnv(b.Helpers)
		if err != nil {
			return nil, fmt.Errorf("backend %q: %w", bname, err)
		}
		e.tools[bname] = map[string]*compiledTool{}
		for tname, t := range b.Tools {
			ct, err := compileTool(env, tname, t)
			if err != nil {
				return nil, fmt.Errorf("backend %q tool %q: %w", bname, tname, err)
			}
			e.tools[bname][tname] = ct
		}
	}
	return e, nil
}

// newEnv builds the CEL environment; unknown helper name is fatal (fail closed).
func newEnv(helpers []string) (*cel.Env, error) {
	opts := []cel.EnvOption{
		cel.Variable("tool", cel.StringType),
		cel.Variable("backend", cel.StringType),
		cel.Variable("method", cel.StringType),
		cel.Variable("args", cel.MapType(cel.StringType, cel.DynType)),
		getFunc(),
	}
	for _, h := range helpers {
		opt, ok := helperOptions(h)
		if !ok {
			return nil, fmt.Errorf("unknown helper %q", h)
		}
		opts = append(opts, opt...)
	}
	return cel.NewEnv(opts...)
}

func compileTool(env *cel.Env, name string, t config.Tool) (*compiledTool, error) {
	action := t.Action
	if action == "" {
		action = name
	}
	ct := &compiledTool{
		resourceType: t.ResourceType,
		action:       action,
		attrProgs:    map[string]cel.Program{},
	}
	var err error
	if ct.idProg, err = compileString(env, t.ID); err != nil {
		return nil, fmt.Errorf("id: %w", err)
	}
	for k, expr := range t.Attr {
		p, err := compileString(env, expr)
		if err != nil {
			return nil, fmt.Errorf("attr %q: %w", k, err)
		}
		ct.attrProgs[k] = p
	}
	if t.AttrFrom != "" {
		if ct.attrFromProg, err = compileMap(env, t.AttrFrom); err != nil {
			return nil, fmt.Errorf("attrFrom: %w", err)
		}
	}
	return ct, nil
}

// compileString compiles an expression and checks it yields a string.
func compileString(env *cel.Env, expr string) (cel.Program, error) {
	ast, iss := env.Compile(expr)
	if iss != nil && iss.Err() != nil {
		return nil, iss.Err()
	}
	if !ast.OutputType().IsAssignableType(cel.StringType) && ast.OutputType().String() != "string" {
		return nil, fmt.Errorf("expression must yield string, got %s", ast.OutputType())
	}
	return env.Program(ast)
}

// compileMap compiles an expression and checks it yields a map keyed by string.
func compileMap(env *cel.Env, expr string) (cel.Program, error) {
	ast, iss := env.Compile(expr)
	if iss != nil && iss.Err() != nil {
		return nil, iss.Err()
	}
	if k := ast.OutputType().Kind(); k != types.MapKind {
		return nil, fmt.Errorf("expression must yield a map, got %s", ast.OutputType())
	}
	return env.Program(ast)
}

// ErrDeny signals the call must be denied on the shim's own malfunction (CEL
// eval failure or malformed result); policy denies are Cerbos's, not this.
type ErrDeny struct{ Reason string }

func (e *ErrDeny) Error() string { return e.Reason }

// Eval runs a tool's compiled programs and returns the Cerbos resource, or an
// *ErrDeny if anything is wrong. Never returns a partial Resource on error.
func (e *Engine) Eval(in CallInput) (*Resource, error) {
	bt, ok := e.tools[in.Backend]
	if !ok {
		return nil, &ErrDeny{Reason: fmt.Sprintf("backend %q not mapped", in.Backend)}
	}
	ct, ok := bt[in.Tool]
	if !ok {
		return nil, &ErrDeny{Reason: fmt.Sprintf("tool %q not mapped on backend %q", in.Tool, in.Backend)}
	}
	vars := map[string]any{
		"tool":    in.Tool,
		"backend": in.Backend,
		"method":  in.Method,
		"args":    in.Args,
	}
	res := &Resource{ResourceType: ct.resourceType, Action: ct.action, Attr: map[string]any{}}

	// attrFrom first (canonicalizers); Attr overrides individual keys after.
	if ct.attrFromProg != nil {
		out, _, err := ct.attrFromProg.Eval(vars)
		if err != nil {
			return nil, &ErrDeny{Reason: fmt.Sprintf("attrFrom eval: %v", err)}
		}
		m, err := toAnyResultMap(out)
		if err != nil {
			return nil, &ErrDeny{Reason: fmt.Sprintf("attrFrom result: %v", err)}
		}
		for k, v := range m {
			res.Attr[k] = v
		}
	}
	for k, p := range ct.attrProgs {
		v, err := evalString(p, vars)
		if err != nil {
			return nil, &ErrDeny{Reason: fmt.Sprintf("attr %q eval: %v", k, err)}
		}
		res.Attr[k] = v
	}
	id, err := evalString(ct.idProg, vars)
	if err != nil {
		return nil, &ErrDeny{Reason: fmt.Sprintf("id eval: %v", err)}
	}
	// empty id causes Cerbos InvalidArgument and shim fail-closed; use "*" so policy can decide.
	if id == "" {
		id = "*"
	}
	res.ID = id

	return res, nil
}

func evalString(p cel.Program, vars map[string]any) (string, error) {
	out, _, err := p.Eval(vars)
	if err != nil {
		return "", err
	}
	s, ok := out.Value().(string)
	if !ok {
		return "", fmt.Errorf("expected string, got %T", out.Value())
	}
	return s, nil
}

// toAnyResultMap converts an attrFrom CEL result into map[string]any. Most
// helpers still yield map<string,string> (unchanged), but linearProjectAttr
// yields a map with a list<string> value (the `teams` key) for Cerbos's
// "any element not in allowlist" check — so this widens from the old
// map[string]string-only conversion to accept any attr value shape.
func toAnyResultMap(v ref.Val) (map[string]any, error) {
	native, err := v.ConvertToNative(reflect.TypeOf(map[string]any{}))
	if err != nil {
		return nil, err
	}
	m, ok := native.(map[string]any)
	if !ok {
		return nil, fmt.Errorf("expected map[string]any, got %T", native)
	}
	return m, nil
}
