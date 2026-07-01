# Vicegerent Agents

GitOps repository for the **vicegerent** infra agent platform — credential-isolated, egress-locked Hermes agent sandboxes on a local minikube cluster, managed by Flux.

![Architecture of Vicegerent Agents (Excalidraw)](./architecture.png)

## MCP authorization model

Three components enforce MCP authorization, each with one job. Keeping them separate is the design — overlapping allowlists drift out of sync, and that drift is a security bug.

- **Agentgateway**: owns the MCP tool allowlist (with CEL).
- **Cerbos**: argument/resource blocking for allowlisted MCP tools (with CEL).
- **mcp-cerbos-shim**: standardizes fields so Cerbos is accurate (with drop-in helper functions).

Permit decisions belong to the gateway allowlist; every deny is Cerbos's; the shim makes no policy decisions. See [`images/mcp-cerbos-shim/README.md`](images/mcp-cerbos-shim/README.md) for the full division of responsibility.

## Create the local minikube cluster

Prerequisites:

- macOS with minikube + vfkit installed
- `kubectl`
- `flux`
- `helm`
- `op`
- `jq`
- SSH access to `gitlab.hahomelabs.com:jchristensen/vicegerent-agents`

Create the cluster:

```bash
./vicegerent cluster setup
```

Verify the cluster and addons:

```bash
kubectl --context vicegerent get nodes -o wide
kubectl --context vicegerent get pods -n kube-system
kubectl --context vicegerent get runtimeclass
kubectl --context vicegerent top nodes
```

If metrics are not ready immediately, wait a minute and rerun `kubectl --context vicegerent top nodes`.

## Secrets setup

Secrets are provisioned in two passes: **platform-wide** material (shared by the
whole cluster) and **per-agent** material (one set per named agent). Both are
idempotent — anything already in 1Password is reused, never regenerated, and each
only prompts before a step that actually changes something.

### Platform-wide

Creates the `Vicegerent` vault, the 1Password Connect server and token, the
ghostunnel CA, the egress-proxy MITM CA, the server/client certificates, and the
shared model/search API keys — storing everything in 1Password and leaving nothing
on disk.

```bash
export ANTHROPIC_API_KEY=sk-ant-...   # set for any key to be stored non-interactively
./vicegerent secrets setup platform
```

```text
-y, --yes     auto-approve every change (non-interactive)
--force       rebuild the CA and all certificates from scratch
```

This provisions, in vault `Vicegerent`:

```text
Connect Credentials          1password-credentials.json   (Connect bootstrap)
Connect Token                token                         (operator token)
Agentgateway - Anthropic      Authorization                 (Anthropic API key → agentgateway-system)
Agentgateway - OpenAI         Authorization                 (optional OpenAI key → agentgateway-system)
Agentgateway - Host MCP       tls.crt, tls.key             (mTLS client cert → agentgateway-system)
Agentgateway - Host MCP CA    ca.cert                       (public CA → agentgateway-system)
Host - MCP Tunnel             server.crt, server.key, ca.cert, ca.key   (host-only, never synced)
Egress Proxy CA               ca.crt, ca.key               (MITM CA private material → egress-proxy)
Egress Proxy CA Cert          ca.crt                        (public CA → agent-sandbox + searxng trust)
SearXNG                       secret_key                    (session/limiter signing key)
MCP - Tavily                  TAVILY_API_KEY                (optional → kmcp)
MCP - Firecrawl               FIRECRAWL_API_KEY             (optional → kmcp)
```

The CA private key lives only in `Host - MCP Tunnel` so a re-run can re-issue a missing leaf certificate without rebuilding the chain. The server private key never enters Kubernetes. 1Password is the single source of truth for this material, for both the laptop and the cluster.

### Per-agent

Run once per named agent. Each agent gets its own independently generated dashboard
credentials, SSH key, and agentgateway bearer token — no material is shared between
agents.

```bash
./vicegerent secrets setup agent hermes   # accepts -y/--yes
```

This provisions, in vault `Vicegerent` (for agent name `<name>`):

```text
Agent - <name>                    password, signing-secret  (dashboard auth)
                                  SLACK_BOT_TOKEN, SLACK_APP_TOKEN,
                                  SLACK_ALLOWED_USERS, SLACK_HOME_CHANNEL (optional),
                                  public-key                (→ agent-sandbox)
Agent - <name> SSH Key            ed25519 keypair (1Password Document)
Agent - <name> agentgateway API key   api-key (random hex bearer token)
```

## Bootstrap Flux

Bootstrap the local minikube cluster against this repo. The script is idempotent — it seeds 1Password Connect (recovering a stuck/failed Helm release if an earlier run was interrupted), then runs `flux bootstrap git`. Once the Connect release is deployed and adopted by Flux, re-runs skip the Helm seed so they do not fight Flux's ownership. It confirms before each change; pass `-y`/`--yes` for a non-interactive run.

```bash
./vicegerent bootstrap
```

The script defaults to:

```text
KUBE_CONTEXT=vicegerent
REPO_URL=ssh://git@gitlab.hahomelabs.com/jchristensen/vicegerent-agents.git
BRANCH=main
CLUSTER_PATH=./clusters/vicegerent
PRIVATE_KEY_FILE=$HOME/.ssh/id_rsa
OP_CONNECT_CREDENTIALS_REF=op://Vicegerent/Connect Credentials/1password-credentials.json
OP_CONNECT_TOKEN_ITEM=Connect Token
OP_CONNECT_TOKEN_VAULT=Vicegerent
```

Override those with environment variables if needed:

```bash
BRANCH=my-test-branch PRIVATE_KEY_FILE=$HOME/.ssh/id_ed25519 ./vicegerent bootstrap
```

Check reconciliation:

```bash
flux --context vicegerent get all -A
kubectl --context vicegerent get pods -A
```

The committed `gotk-sync.yaml` expects the bootstrap-created `flux-system` Git credential Secret.

## Host-side MCP control plane

OAuth-backed or laptop-context MCPs run in the macOS GUI session and are exposed to the cluster through ghostunnel. The control plane lives in [`host/mcp`](host/mcp): it renders `mcp-proxy-server` config (vendored at `host/mcp-proxy-server`) and supervises `mcp-proxy-server` + Caddy + ghostunnel, plus a `caffeinate` process that keeps macOS awake while the stack runs. It filters the host endpoint to the MCP transport methods (`POST`/`GET`/`DELETE` on `/mcp`) and includes helper commands for `mcp-remote` OAuth cache status/reset.

Start and stop the whole local platform — the minikube cluster and the host MCP stack together — with the top-level commands:

```bash
./vicegerent start   # resume minikube, then bring up the host MCP stack
./vicegerent stop    # stop the host MCP stack, then pause minikube
```

For finer control of just the host stack, drive it with `./vicegerent-mcp` (`start`, `stop`, `status`, `enable`/`disable`, `reload`, `logs`, `doctor`, `tui`); see [`host/mcp/README.md`](host/mcp/README.md) for the full reference.

```bash
./vicegerent-mcp start
./vicegerent-mcp status
```

The host-MCP tunnel defaults to `192.168.64.1:8453`. Agents reach these tools through per-server gateway MCP routes: `/mcp/host` (host-tunneled stdio MCPs — Kubernetes, Linear, Notion) and one route per in-cluster `charts/vicegerent-mcp` server — currently `/mcp/tavily` and `/mcp/firecrawl` (gitlab is defined under `apps/vicegerent/mcps/gitlab` but not yet applied by Flux).

## Development

Install and run the repo hooks before committing:

```bash
pre-commit install
pre-commit run --all-files
```

The local Flux validation hook expects `yq` v4, `kustomize`, `kubeconform`, and `curl` on `PATH`.
