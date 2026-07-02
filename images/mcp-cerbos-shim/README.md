# mcp-cerbos-shim

An [agentgateway](https://agentgateway.dev) **ExtMcp** policy server that authorizes
MCP tool calls against a [Cerbos](https://cerbos.dev) PDP. It is the connector that lets
agentgateway enforce **argument-level** authorization on MCP traffic; e.g. block an agent
from reading Kubernetes Secrets through a generic Kubernetes MCP server, while allowing
reads of non-secret resources.

## Authorization Layers

There is **no per-tool allowlist**: every tool the host vMCP exposes passes through
agentgateway to the agent. This shim + Cerbos are the only instance-level control, and
they block exactly one thing — reading Kubernetes Secrets. They are a confidentiality
guardrail, not a tool gate.

| Layer | Job | What it is NOT |
| --- | --- | --- |
| **agentgateway** | Single MCP ingress gate: routing, bearer auth, mTLS to the host vMCP. | NOT a per-tool allowlist — it does not decide *which* tools an agent may call. |
| **mcp-cerbos-shim** (this) | Extract the resource a *kind-bearing* tool targets and ask Cerbos about it. | NOT a list of allowed tools or kinds. `defaultAction: allow`; only the tools that can name a Secret are mapped. |
| **Cerbos policy** | Make every deny decision: block instances that touch Secrets, and reject a kind-bearing call whose kind can't be resolved. | NOT a list of allowed tools/kinds, and NOT a principal gate. Rule is allow-all for all roles + DENY overrides for Secrets and empty-kind. |

Consequences:
- A new read-only tool exposed by the vMCP needs **no** shim/Cerbos change; it
  passes, because it isn't a Secret.
- An unknown/arbitrary Kubernetes kind (e.g. a CRD) is **allowed**, not denied;
  the shim blocks Secrets, it does not enumerate readable kinds.
- The only tools mapped in the shim are the ones that take a `kind`/resource
  selector (`kubernetes_resources_get`, `kubernetes_resources_list`), because
  those are the only ones that can name a Secret. Everything else passes untouched.

Do not add a tool or kind name to the shim mapping or Cerbos `allow` rule to permit
something — permitting a tool is not this layer's job; it exists only to deny Secret reads.

### Guardrail Attachment

The Secret block depends on `AgentgatewayPolicy` attaching the `tools/call ->
mcp-cerbos-shim` guardrail with `failureMode: FailClosed`. `FailClosed` only
covers invoked processor failures; a missing guardrail silently fails open.

- **Authoring (covered):** `scripts/validate.sh` renders the overlay and fails CI
  if the rendered `AgentgatewayPolicy` does not carry exactly one `tools/call ->
  mcp-cerbos-shim` guardrail with `FailClosed`. A bad edit can't merge.
- **Runtime (NOT covered):** if Flux never reconciles the commit, or the
  controller silently rejects the CRD, the live gateway can lack the guardrail
  even though the repo is correct. There is no backend-level default-deny in the
  agentgateway CRD to backstop this, so it is an accepted gap on this dev
  platform. Treat a reconcile failure on this policy as a security incident.

## Why it exists

agentgateway's HTTP `extAuthz` (which Cerbos speaks natively) **cannot see MCP tool
arguments** at decision time (upstream issue #720; the `mcp` CEL context is empty during
extauthz). Only the **ExtMcp guardrails** protocol (`ext_mcp.proto`) carries the tool name +
params, and Cerbos doesn't implement it. This connector bridges the two: it implements
`ExtMcp.CheckRequest` on one side and calls Cerbos `CheckResources` on the other.

## How it works

For each `tools/call` the gateway forwards (`McpRequest`), the connector:

1. Resolves the backend from `service_names` (exactly one mapped backend, else deny).
2. Parses the JSON-RPC params (`{name, arguments}`); unparseable/missing denies.
3. Looks up `(backend, tool)` in the mapping. The `vmcp` backend is `defaultAction:
   allow`, so an unmapped tool **passes** (it can't name a Secret); only the
   kind-bearing tools are mapped.
4. Evaluates the mapped tool's CEL expressions against `{tool, args, backend,
   method}` to build a Cerbos resource (standardizing kind/apiResource via the
   `canonicalK8s` helper). A CEL eval failure denies (the shim's own
   malfunction; never send a half-built resource).
5. Calls Cerbos `IsAllowed`. Allowed returns `Pass{}`; denied or Cerbos error returns `AuthorizationError`.

It **never** returns `mutated`, `header_mutation`, or `metadata`; only `pass` or `error`
(the gateway applies `metadata` even on `Pass`, so leaving it empty is part of the contract).

The shim makes **no policy decisions**; it standardizes fields and delegates
the verdict. Every *deny* is Cerbos's (Secrets, and a kind-bearing call whose
kind can't be resolved as `kind==""`/`deny-no-kind`); everything else is permitted
by default — there is no per-tool allowlist. The shim only fails closed on its own malfunction
(unparseable params, unknown/multiple backend, CEL eval error, Cerbos
unreachable). See `internal/server/server_test.go` for the full matrix.

## Config

A YAML mapping (see `mapping.example.yaml`) mounted at `MAPPING_PATH`. Every `id`/`attr`/
`attrFrom` value is a CEL expression compiled and type-checked at startup; an invalid mapping
aborts startup (k8s restarts the pod; the gateway's `FailClosed` denies meanwhile).

| Env var | Default | Meaning |
| --- | --- | --- |
| `LISTEN_ADDR` | `:4445` | gRPC listen address |
| `MAPPING_PATH` | `/etc/mcp-cerbos-shim/mapping.yaml` | mapping file |
| `CERBOS_ADDR` | `cerbos.cerbos.svc.cluster.local:3593` | Cerbos PDP gRPC |
| `CERBOS_PLAINTEXT` | `true` | use plaintext to the PDP (mTLS later) |

The Cerbos principal is a fixed constant (`hermes`/`agent`) stamped on every
request for audit context. It is **not** an authorization control: the policy
allows all roles and denies only by resource, so there is nothing to configure.

## Helpers (per-backend, pluggable)

The CEL evaluator core is generic; it knows the MCP wire shape and Cerbos, nothing
about any specific server. Server-specific normalization lives in **helpers**: CEL
functions a mapping opts into via a backend's `helpers:` list, scoped to that backend
only (a k8s helper can't leak into a GitHub or AWS mapping).

Built-in:

| Helper | Backend | Purpose |
| --- | --- | --- |
| `get(map, key, default)` | core (always in scope) | case-insensitive arg lookup |
| `canonicalK8s(args)` | `helpers_k8s.go` | reads `kind`/`Kind` case-insensitively and normalizes a **Secret** reference (plural, `v1/secrets`, etc.) to `{kind:Secret, apiResource:secrets}` so Cerbos's deny-secrets rule catches every spelling; any other kind is passed through unchanged (no per-kind allowlist) |

To add a helper for a new MCP server, drop an `internal/eval/helpers_<backend>.go`
whose `init()` calls `registerHelper("<name>", <ctor>)` and defines the CEL function;
**no edits to the generic core**. Reference it from that backend's mapping under
`helpers:`. A name listed in a mapping but not registered aborts startup (fail closed);
duplicate registration panics at startup.

## Build

A `Makefile` wraps the common tasks (`make help` lists them). The image name is
fixed to `harbor.hahomelabs.com/vicegerent/mcp-cerbos-shim`; override the tag
with `TAG=`.

```bash
make check                 # gofmt-check + go vet + go test ./... (CI parity)
make image TAG=v0.1.0      # docker build
make push  TAG=v0.1.0      # docker push to Harbor
make release TAG=v0.1.0    # check + image + push
make proto                 # regenerate stubs (only when proto/ext_mcp.proto changes)
```

`make proto` requires the protoc plugins on PATH:

```bash
go install google.golang.org/protobuf/cmd/protoc-gen-go@latest
go install google.golang.org/grpc/cmd/protoc-gen-go-grpc@latest
export PATH="$PATH:$(go env GOPATH)/bin"
```

`proto/ext_mcp.proto` is vendored from agentgateway and pinned to the deployed gateway
version (v1.3.1). The proto is not API-stable across versions; bump it deliberately.
