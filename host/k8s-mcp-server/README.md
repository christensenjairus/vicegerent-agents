# k8s-mcp-server

Fork of [reza-gholizade/k8s-mcp-server](https://github.com/reza-gholizade/k8s-mcp-server)
modified to add a required `context` parameter to every Kubernetes tool, so a single
process serves all kubeconfig contexts.

## Build

```bash
make -C host/k8s-mcp-server
```

Binary lands at `host/k8s-mcp-server/k8s-mcp-server`.

## Usage

Pass `"context": "uw1-prod1"` (or any valid kubeconfig context name) as a required
argument in every tool call. `KUBECONFIG` controls which file is used; defaults to
`~/.kube/config`.

## Changes from upstream

- `pkg/k8s/client_factory.go` — `ClientFactory` with a `sync.Map` cache keyed by
  context name; builds one `*k8s.Client` per context on first use
- `pkg/k8s/client.go` — `BuildKubernetesConfig` / `NewClient` accept a `contextName`
  arg and apply `ConfigOverrides{CurrentContext: contextName}`
- `tools/k8s.go` — all 13 k8s tools declare `context` as the first required parameter
- `handlers/k8s.go` — all 13 handlers accept `*k8s.ClientFactory`, extract `context`
  from args, and call `factory.GetOrCreate(contextName)` per request
- `main.go` — uses `k8s.NewClientFactory()` instead of a startup-time singleton
- Helm tools are unchanged
