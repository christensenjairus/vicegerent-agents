# agentgateway (vicegerent patched build)

A source-patched build of the upstream
[`agentgateway/agentgateway`](https://github.com/agentgateway/agentgateway) data
plane, published to `harbor.hahomelabs.com/vicegerent/agentgateway`. Unlike
`images/hermes` (a thin `FROM` derivation of a published image), agentgateway is
a Rust binary with no runtime patch seam, so the fix has to be applied to source
and compiled. This directory clones upstream at a pinned tag, applies the patches
in `patches/`, and builds — reusing upstream's glibc target + arm64 jemalloc
page-size handling, dropping the UI feature (the proxy does not serve the
dashboard here).

## Why a patched image

A **request-phase** `mcpGuardrails` rejection (how the `mcp-cerbos-shim` denies a
Secret read, and how it will deny future destructive actions) is returned by
upstream v1.3.1 as **HTTP 400** with the JSON-RPC error in the body
(`crates/agentgateway/src/proxy/mod.rs`, the `McpGuardrails` arm of the
`ProxyError` status mapping). The
Python `mcp` SDK that Hermes uses calls `raise_for_status()` on any non-2xx,
which tears down the whole MCP session — so a policy deny surfaces to the agent as
"the MCP disconnected" instead of "blocked by policy".

The fix is one line: return that rejection as **HTTP 200** (the JSON-RPC error
body is already built correctly downstream — only the status was wrong). 200 is
the correct transport for an application-level per-call refusal and matches the
gateway's own **response-phase** guardrail path. See
`patches/0001-guardrail-reject-200.patch`.

This must be fixed in the **request** phase, not worked around by moving the block
to the response phase: response-phase blocking can hide a result but cannot stop a
side effect (a destructive command already executed upstream by the time the
response exists). Request-phase blocking is the only phase that generalizes to
both secret-confidentiality and destructive-action prevention.

Carry this image only until the fix lands upstream; an upstream PR returning
request-phase guardrail rejections as 200 is filed in parallel. When a fixed
upstream release is deployed, repoint the chart back at the stock image and delete
this directory.

## Patches

- `0001-guardrail-reject-200.patch` — `crates/agentgateway/src/proxy/mod.rs`: map
  the `McpGuardrails` arm to `StatusCode::OK` instead of `BAD_REQUEST` (and updates
  the corresponding `controller/test/e2e/extmcp_test.go` expectation to 200).

`git apply --verbose` in the build **hard-fails** if a patch stops applying
against a new `AGW_VERSION`, which is the intended signal to re-verify (or drop)
the patch rather than silently miscompile.

## Build & push

Built on a machine with internet (your laptop), then pushed to Harbor. The
egress-locked cluster only ever pulls.

```sh
docker login harbor.hahomelabs.com
make image PLATFORM=linux/arm64      # Kind on Apple Silicon
make push
# or: make release PLATFORM=linux/arm64 AGW_VERSION=v1.3.1
```

This is a full Rust release compile — the first build is slow; rebuilds reuse
the cargo layer cache. `make help` lists targets.

## Version pin & Renovate

`AGW_VERSION` (the upstream tag cloned and built) is tracked by Renovate via the
`# renovate: datasource=github-releases depName=agentgateway/agentgateway`
comment on the `ARG` in the `Dockerfile`. An upstream release opens an MR bumping
it; the build then either still applies the patch cleanly or fails loudly so the
patch can be re-checked.

The image `TAG` is the upstream version verbatim (`v1.3.1`), **not** a patch-suffixed
tag. This keeps the gateway image ref a clean semver string so the `docker`
datasource on `apps/base/gateway/gateway.yaml` tracks it and Renovate
auto-detects the next build. The patched-vs-stock distinction is carried by the
**registry path** (`harbor.hahomelabs.com/vicegerent/agentgateway` vs upstream
`ghcr.io/agentgateway/agentgateway`), not the tag.

Keep `AGW_VERSION` in lockstep with the agentgateway chart/data-plane version
(`apps/base/gateway/gateway.yaml` `AgentgatewayParameters.spec.image.tag`
and the chart in `infrastructure/controllers/agentgateway/`) when rebuilding.
