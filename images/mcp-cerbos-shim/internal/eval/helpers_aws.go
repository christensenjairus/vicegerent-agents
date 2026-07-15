package eval

// AWS-specific helper; self-registers via init().

import (
	"strings"

	"github.com/google/cel-go/cel"
	"github.com/google/cel-go/common/types"
	"github.com/google/cel-go/common/types/ref"
)

func init() {
	registerHelper("awsSecretReadAttr", awsSecretReadAttrOption)
}

// awsValueGlobalOpts are AWS CLI global options whose VALUE is the following
// token, so that token must be skipped when scanning for the service/operation
// positionals. (--opt=value is a single token, skipped by the leading-dash
// rule; boolean globals like --debug/--no-verify-ssl take no value.)
var awsValueGlobalOpts = map[string]bool{
	"--region": true, "--profile": true, "--output": true, "--endpoint-url": true,
	"--ca-bundle": true, "--cli-read-timeout": true, "--cli-connect-timeout": true,
	"--color": true, "--query": true, "--page-size": true, "--max-items": true,
	"--starting-token": true, "--cli-binary-format": true, "--cli-auto-prompt": true,
}

// awsSecretReadAttrOption surfaces, for the aws-api-mcp-server `call_aws` tool,
// each parsed command's "<service>/<operation>" as a lowercased `awsOps` list,
// so resource_aws.yaml can deny Secrets Manager value-reads (get-secret-value /
// batch-get-secret-value). READ_OPERATIONS_ONLY does NOT cover these — a secret
// read is classified read-only — which is exactly why this resource guardrail
// exists on top.
//
// call_aws's `cli_command` is a str OR a list[str] (batch, up to 20). Both are
// parsed, one awsOps token per command, so the batch path can't smuggle a
// secret read past the rule, and a per-command token keeps service+operation
// atomic (no false cross-pairing across commands).
//
// AWS CLI grammar is `aws [global-options] <service> <operation> [params]`:
// service and operation are the first two barewords after an optional leading
// `aws`, once interleaved global options (and their values) are skipped. A
// command we can't confidently parse yields no awsOps entry, so the
// has()-guarded deny falls through to allow — fail-open-when-unverifiable, the
// same posture as every other helper; per-profile IAM is the airtight backstop.
func awsSecretReadAttrOption() []cel.EnvOption {
	return []cel.EnvOption{
		cel.Function("awsSecretReadAttr",
			cel.Overload("awsSecretReadAttr_map",
				[]*cel.Type{cel.MapType(cel.StringType, cel.DynType)},
				cel.MapType(cel.StringType, cel.DynType),
				cel.UnaryBinding(func(arg ref.Val) ref.Val {
					m := toAnyMap(arg)
					// cli_command is either a string or a []string (batch);
					// gather both shapes (only one is ever populated).
					var cmds []string
					if s := lookupCI(m, "cli_command", ""); s != "" {
						cmds = append(cmds, s)
					}
					cmds = append(cmds, lookupCIStringSlice(m, "cli_command")...)

					var ops []string
					for _, c := range cmds {
						if op := awsServiceOp(c); op != "" {
							ops = append(ops, op)
						}
					}
					if len(ops) == 0 {
						return types.NewDynamicMap(types.DefaultTypeAdapter, map[string]any{})
					}
					return types.NewDynamicMap(types.DefaultTypeAdapter, map[string]any{"awsOps": ops})
				}),
			),
		),
	}
}

// awsServiceOp extracts "<service>/<operation>" (lowercased) from one AWS CLI
// command string, or "" if it can't identify both positionals.
func awsServiceOp(command string) string {
	toks := tokenizeShellLike(command)
	i := 0
	if i < len(toks) && strings.EqualFold(toks[i], "aws") {
		i++
	}
	var pos []string
	for i < len(toks) && len(pos) < 2 {
		t := toks[i]
		if t == "--" { // explicit end-of-options separator
			i++
			continue
		}
		if strings.HasPrefix(t, "-") {
			if !strings.Contains(t, "=") && awsValueGlobalOpts[strings.ToLower(t)] {
				i += 2 // skip the option AND its value token
				continue
			}
			i++
			continue
		}
		pos = append(pos, t)
		i++
	}
	if len(pos) < 2 {
		return ""
	}
	service := strings.ToLower(strings.TrimSpace(pos[0]))
	operation := strings.ToLower(strings.TrimSpace(pos[1]))
	if service == "" || operation == "" {
		return ""
	}
	return service + "/" + operation
}

// tokenizeShellLike splits on unquoted whitespace, honoring '...' and "..." so
// a quoted option value (e.g. --profile "my profile") isn't mis-split into two
// tokens. It is NOT a full shell parser — the aws-api-mcp-server itself forbids
// pipes, redirection, and command substitution — just enough to isolate the
// service/operation positionals robustly.
func tokenizeShellLike(s string) []string {
	var toks []string
	var cur strings.Builder
	inSingle, inDouble, started := false, false, false
	flush := func() {
		if started {
			toks = append(toks, cur.String())
			cur.Reset()
			started = false
		}
	}
	for _, r := range s {
		switch {
		case inSingle:
			if r == '\'' {
				inSingle = false
			} else {
				cur.WriteRune(r)
			}
		case inDouble:
			if r == '"' {
				inDouble = false
			} else {
				cur.WriteRune(r)
			}
		case r == '\'':
			inSingle, started = true, true
		case r == '"':
			inDouble, started = true, true
		case r == ' ', r == '\t', r == '\n', r == '\r':
			flush()
		default:
			cur.WriteRune(r)
			started = true
		}
	}
	flush()
	return toks
}
