# Vicegerent Agents

GitOps repository for the **vicegerent** infra agent platform — credential-isolated, egress-locked Hermes agent sandboxes on a local minikube cluster, managed by Flux.

![Architecture of Vicegerent Agents (Excalidraw)](./architecture.png)

## Why this exists

**What it provides.** Every agent runs as its own `agent-sandbox` pod: non-root, gVisor-isolated, with its own 1Password-issued credentials (SSH key, dashboard auth, agentgateway bearer token) that are never shared with another agent. The sandbox has no direct internet or cluster access — every model call goes through agentgateway, every tool call goes through an allowlisted MCP route, and every shell command passes through a layered approval pipeline (hardline block → operator silence list → tirith static scan → LLM-assessed smart approval) before it executes. MCP tool authorization is itself split into two independent layers (agentgateway's tool allowlist decides *what an agent may call at all*; Cerbos decides *which arguments/instances of an allowed tool are blocked*, e.g. reading Kubernetes Secrets) so a single misconfigured allowlist can't silently become a security hole. Because the containment is structural, agents can run genuinely autonomously — unattended, on schedules, reacting to events — without needing a human in the loop for every action; the boundary is what makes autonomy safe to grant. The whole platform — agents, models, MCP servers, secrets policy, approval rules — is Flux-reconciled from this git repo: standing it up, changing it, or reproducing it on another machine is `git clone` + `flux bootstrap`, not a pile of manual laptop setup steps.

**Why not just run Claude Code / Codex directly on a laptop?** This isn't a replacement for that — running an agent directly on your laptop with a human watching every action is a perfectly good mode, and for a lot of interactive work it's the right one. The problem shows up specifically when you want the agent to run unattended: a CLI agent running as your own user has your full filesystem, your full network, and every credential in your shell environment or keychain, gated only by per-action safety prompts. Those prompts work when a human is actually reading them, but in practice people learn the agent's patterns and start reflexively approving — at which point the prompt is theater, not a control, and a prompt-injected page or a wrong destructive command has the same reach you do. Claude Code on a laptop is great precisely because a human is there; this project exists for the case where one isn't — durable, unattended, full autonomy — and needs the containment to come from the platform instead of from someone paying attention to every prompt. Nothing about the laptop setup is declarative or reviewable either — config lives in whatever state your laptop happens to be in, drifts silently, and isn't reproducible for a second machine or a second person.

**Why not a plain container, or a runtime like NVIDIA OpenShell?** A raw container is an improvement over bare-metal but usually still runs as root or with a mounted Docker socket (host-root-equivalent), has unrestricted egress, and mounts one set of credentials that every process inside can read. [NVIDIA OpenShell](https://github.com/NVIDIA/OpenShell) is a closer analogue — per-sandbox isolation, declarative YAML network/filesystem/process policy, and credential injection via named providers — but it's explicitly alpha, single-tenant ("single-player mode": one developer, one environment, one gateway), and its policy engine draws one line per sandbox (filesystem/network/process/inference) rather than a two-layer split between *which MCP tools exist at all* and *which arguments to an allowed tool are blocked*. That coarser model also pushes a real tradeoff onto the operator at runtime: without the graded approval pipeline this repo has, a sandbox either needs a human reviewing/approving actions as it goes, or it needs a policy loose enough to let the agent actually act on its own — there's no middle layer that lets an agent run unattended while still catching a specific dangerous call. Neither a plain container nor OpenShell gives you Flux-reconciled, git-auditable state across many independently-credentialed agents sharing one cluster.

**How this is better.** Compromising one agent's shell doesn't leak another agent's credentials or reach the host — the credential and network boundary is enforced by the platform, not by the agent's good behavior. Every capability an agent has is explicit and versioned in git (which MCP tools, which model routes, which egress paths), so "what can this agent do" is answerable by reading the repo, not by auditing a running process. Command execution gets real guardrails (static scan + LLM triage) even though the agent is otherwise trusted, and MCP tool access gets fine-grained argument-level control instead of a binary allow/disable per tool.

**Drawbacks of this architecture.** The flexibility that makes this useful is also complexity to maintain:
- More moving parts than a laptop CLI — minikube, Flux, agentgateway, Cerbos, and the approval pipeline all need to be understood together when something doesn't behave as expected.
- Adding a new integration takes a few more steps than `pip install` — an MCP route, an agentgateway allowlist entry, and possibly a Cerbos rule.
- The host-side MCP bridge depends on the developer's machine being on; it's a deliberate tradeoff (see below) rather than a fully in-cluster design.
- Onboarding a new agent (secrets, sandbox config, gateway routes) takes longer than opening a terminal and running a CLI tool.
- It's infrastructure with normal upkeep — cert rotation, image bumps, keeping Flux reconciled — like any platform, not a install-and-forget script.
- The isolation and review overhead pays off most for agents that run unattended or touch things worth protecting; for quick one-off exploration it's more setup than you need.

## Why not run every tool in Kubernetes?

Some MCP servers stay on the developer's machine on purpose (see [`host/mcp`](host/mcp)) — OAuth-backed tools and anything tied to laptop-local session state (browser OAuth flows, a local kubeconfig, AWS SSO) don't have a clean cluster-side equivalent. Standing up a service identity for these in-cluster means either provisioning bot accounts/service tokens per integration or building an OAuth flow a headless pod can complete on its own, and most organizations haven't standardized bot identities for every tool an agent might want to use. Rather than block on that, this platform runs those tools under the developer's own already-authenticated session and tunnels them into the cluster (ghostunnel + Caddy + `mcp-proxy-server`), so the agent acts through the developer's existing identity instead of waiting for a separate one to be provisioned.

That's a deliberate design choice, not just a stopgap: it makes the agent a genuine extension of the developer — reviewing a Notion doc or hitting the Kubernetes API as *them*, with their actual permissions — rather than a separate identity whose access has to be independently reasoned about, requested, and kept in sync with the developer's own. The tradeoff is real and stated above: the host bridge only works while the developer's machine is up. Once an organization has bot tokens and service identities sorted out for a given tool, it both can and should move that tool fully into the cluster — at that point the developer laptop dependency for that integration disappears entirely, and the whole system stops needing any one person's machine at all. Taken to its conclusion, this platform can run entirely in the cloud, serving many different people's sandboxes identically, with no host-side bridge in the picture.

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
