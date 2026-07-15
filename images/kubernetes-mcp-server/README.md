# kubernetes-mcp-server (+ AWS CLI)

A wrapper image around the upstream `kubernetes-mcp-server` npm package that
adds the AWS CLI, so a kubeconfig using AWS's `exec:`-based IAM authenticator
(the kind `aws eks update-kubeconfig` generates) can actually resolve inside
the container.

## Why this exists

`host/mcp/toolhive-servers.json`'s `kubernetes` backend used to run via
ToolHive's generic `npx://kubernetes-mcp-server` protocol handler, which has
no way to layer extra binaries into the container it builds. That's fine for
the default target — the local Kind cluster's kubeconfig carries a static
client cert, no exec plugin — but a kubeconfig for a real AWS EKS cluster
normally shells out to `aws eks get-token` at request time to mint a
short-lived bearer token from the operator's ambient AWS credentials. That
exec call happens *inside* whatever process is holding the kubeconfig open —
here, the containerized `kubernetes-mcp-server` — so the container needs the
`aws` binary on `PATH` and, at runtime, the operator's `~/.aws` mounted in
(see `apply: "aws_config"` in `host/mcp/vicegerent_mcp.py`, the same mount
type the `aws` backend uses — `~/.aws` is always mounted, blank
`aws_config_dir` just defaults to it).

No other behavior changes: server args, tool allowlist, and the Kind-cluster
default kubeconfig path are all untouched.

## Versioning

Both pins are tracked by the repo's generic `images/*/Dockerfile` Renovate
customManager (`# renovate: datasource=... depName=...` immediately above
each `ARG *_VERSION=`):

- `KUBERNETES_MCP_SERVER_VERSION` — the npm package, same version previously
  pinned directly in `toolhive-servers.json`'s `package` field.
- `AWSCLI_VERSION` — AWS CLI v2's GitHub tag (`aws/aws-cli`); the install
  bundle URL is versioned per-tag and verified to resolve for both
  `linux-x86_64` and `linux-aarch64` before bumping.

## Build

```sh
make image                      # native build
make release PLATFORM=linux/arm64 TAG=v0.1.0   # build + push to Harbor
```
