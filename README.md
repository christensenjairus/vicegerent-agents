# Vicegerent Agents

GitOps repository for the **vicegerent** infra agent platform — credential-isolated, egress-locked Hermes agent sandboxes on a local Kind cluster (Cilium CNI), managed by Flux.

![Architecture of Vicegerent Agents (Excalidraw)](./architecture.png)

## Why this exists

#### What it provides

Every agent runs as its own `agent-sandbox` pod: non-root and pod-hardened, with its own generated credentials (SSH key, dashboard auth, agentgateway bearer token) that are never shared with another agent. The sandbox has no direct internet or cluster access — Cilium egress-locks it to a scrubbing egress proxy, git-over-SSH, Slack, and DNS; every model call and every MCP tool call goes through agentgateway; and every shell command passes through a layered approval pipeline (hardline block → operator silence list → tirith static scan → LLM-assessed smart approval) before it executes. MCP tool calls also pass a Cerbos guardrail that blocks reading Kubernetes Secrets regardless of which tool asks, so a confidentiality boundary survives even if a tool is otherwise permitted. Because the containment is structural, agents can run genuinely autonomously — unattended, on schedules, reacting to events — without needing a human in the loop for every action; the boundary is what makes autonomy safe to grant. The whole platform — agents, models, gateway routes, secrets policy, approval rules — is Flux-reconciled from this git repo: standing it up, changing it, or reproducing it on another machine is `git clone` + `flux bootstrap`, not a pile of manual laptop setup steps.

#### Why not just run Claude Code / Codex directly on a laptop?

This isn't a replacement for that — running an agent directly on your laptop with a human watching every action is a perfectly good mode, and for a lot of interactive work it's the right one. The problem shows up specifically when you want the agent to run unattended: a CLI agent running as your own user has your full filesystem, your full network, and every credential in your shell environment or keychain, gated only by per-action safety prompts. Those prompts work when a human is actually reading them, but in practice people learn the agent's patterns and start reflexively approving — at which point the prompt is theater, not a control, and a prompt-injected page or a wrong destructive command has the same reach you do. Claude Code on a laptop is great precisely because a human is there; this project exists for the case where one isn't — durable, unattended, full autonomy — and needs the containment to come from the platform instead of from someone paying attention to every prompt. Nothing about the laptop setup is declarative or reviewable either — config lives in whatever state your laptop happens to be in, drifts silently, and isn't reproducible for a second machine or a second person.

#### Why not a plain container, or a runtime like NVIDIA OpenShell?

A raw container is an improvement over bare-metal but usually still runs as root or with a mounted Docker socket (host-root-equivalent), has unrestricted egress, and mounts one set of credentials that every process inside can read. [NVIDIA OpenShell](https://github.com/NVIDIA/OpenShell) is a closer analogue — per-sandbox isolation, declarative YAML network/filesystem/process policy, and credential injection via named providers — but it's explicitly alpha, single-tenant ("single-player mode": one developer, one environment, one gateway), and its policy engine draws one coarse line per sandbox (filesystem/network/process/inference) rather than combining a kernel-level egress lock with argument-level MCP guardrails and a graded command-approval pipeline. That coarser model also pushes a real tradeoff onto the operator at runtime: without the graded approval pipeline this repo has, a sandbox either needs a human reviewing/approving actions as it goes, or it needs a policy loose enough to let the agent actually act on its own — there's no middle layer that lets an agent run unattended while still catching a specific dangerous call. Neither a plain container nor OpenShell gives you Flux-reconciled, git-auditable state across many independently-credentialed agents sharing one cluster.

#### How this is better

Compromising one agent's shell doesn't leak another agent's credentials or reach the host — the credential and network boundary is enforced by the platform, not by the agent's good behavior. Every capability an agent has is explicit and versioned in git (which model routes, which egress paths, which approval rules), so "what can this agent do" is answerable by reading the repo, not by auditing a running process. Command execution gets real guardrails (static scan + LLM triage) even though the agent is otherwise trusted, and MCP tool calls get an argument-level confidentiality guardrail (no reading Kubernetes Secrets) that a broad tool grant can't override.

#### Drawbacks of this architecture

The flexibility that makes this useful is also complexity to maintain:
- More moving parts than a laptop CLI — Kind, Cilium, Flux, agentgateway, Cerbos, the host ToolHive stack, and the approval pipeline all need to be understood together when something doesn't behave as expected.
- Adding a new integration takes a few more steps than `pip install` — a ToolHive server entry, its host-side secret or OAuth flow, and possibly a Cerbos rule.
- The host-side MCP bridge depends on the developer's machine being on; it's a deliberate tradeoff (see below) rather than a fully in-cluster design.
- Onboarding a new agent (secrets, sandbox config, gateway routes) takes longer than opening a terminal and running a CLI tool.
- It's infrastructure with normal upkeep — cert rotation, image bumps, keeping Flux reconciled — like any platform, not a install-and-forget script.
- The isolation and review overhead pays off most for agents that run unattended or touch things worth protecting; for quick one-off exploration it's more setup than you need.

## Why not run every tool in Kubernetes?

Every MCP server runs on the developer's machine on purpose (see [`host/mcp`](host/mcp)) — OAuth-backed tools and anything tied to laptop-local session state (browser OAuth flows, a local kubeconfig, AWS SSO) don't have a clean cluster-side equivalent. Standing up a service identity for these in-cluster means either provisioning bot accounts/service tokens per integration or building an OAuth flow a headless pod can complete on its own, and most organizations haven't standardized bot identities for every tool an agent might want to use. Rather than block on that, this platform runs those tools under the developer's own already-authenticated session — as ToolHive (`thv`) workloads aggregated behind a single Virtual MCP Server (vMCP) — and tunnels that one endpoint into the cluster over mTLS (ghostunnel), so the agent acts through the developer's existing identity instead of waiting for a separate one to be provisioned.

That's a deliberate design choice, not just a stopgap: it makes the agent a genuine extension of the developer — reviewing a Notion doc or hitting the Kubernetes API as *them*, with their actual permissions — rather than a separate identity whose access has to be independently reasoned about, requested, and kept in sync with the developer's own. The tradeoff is real and stated above: the host bridge only works while the developer's machine is up. Once an organization has bot tokens and service identities sorted out for a given tool, it both can and should move that tool fully into the cluster — at that point the developer laptop dependency for that integration disappears entirely, and the whole system stops needing any one person's machine at all. Taken to its conclusion, this platform can run entirely in the cloud, serving many different people's sandboxes identically, with no host-side bridge in the picture.

## MCP authorization model

All MCP tools reach the cluster through one path: the host vMCP → ghostunnel (mTLS) → agentgateway's `vmcp` backend → the agent. Two separate concerns sit on this path: *tool selection* (which tools exist for the agent) and *argument-level authorization*. Tool selection is done upstream in ToolHive's vMCP (`aggregation.tools`) so operators can scope backends from `toolhive-servers.json`; agentgateway can also enforce a per-tool allowlist itself, and a corporate/centralized deployment would put it there — here it's kept in ToolHive for developer flexibility. Argument-level authorization is a Cerbos guardrail attached to the `vmcp` backend, which denies by resource (`FailClosed` on `tools/call`): reading Kubernetes **Secrets**, reading OpenSearch Grafana datasources, and Jira calls targeting a project other than **CHANGE**.

- **Agentgateway**: MCP ingress gate — routing, bearer auth, mTLS to the host vMCP. Can also enforce a per-tool allowlist centrally; in this setup tool selection is left to ToolHive.
- **Cerbos**: makes the deny decision — blocks the protected resources above (with CEL).
- **mcp-cerbos-shim**: standardizes fields so Cerbos sees an accurate resource; makes no policy decisions.

The shim/Cerbos are a resource guardrail, orthogonal to tool selection. See [`images/mcp-cerbos-shim/README.md`](images/mcp-cerbos-shim/README.md) for the full division of responsibility.

## Forking this repo

This repo is meant to be forked, not shared — each person runs their own fork against
their own laptop and their own local Kind cluster. Nothing here is multi-tenant: two
people bootstrapping the same repo would each try to push Flux's generated manifests
back to it and fight over the same git history.

To stand up your own instance:

1. Fork the repo (GitHub, your own self-hosted git, wherever) and clone your fork.
   `./vicegerent bootstrap` / `install.sh` default `REPO_URL` to this checkout's
   `origin` remote, so cloning your own fork is enough — no env override needed.
2. Make sure the SSH key you'll bootstrap with (`PRIVATE_KEY_FILE`, default
   `~/.ssh/id_rsa`) has **write** access to your fork — `flux bootstrap git` commits
   its generated manifests back to it.
3. If your git host isn't `github.com`, add it to the agent sandbox's SSH egress
   allowlist in `charts/agent/templates/networkpolicy.yaml` (the `toFQDNs` block
   under the "SSH bypasses the HTTP proxy" comment) — otherwise Cilium blocks
   git-over-SSH from inside the sandbox. If that host itself resolves through a
   CNAME (common for self-hosted setups behind dynamic DNS or a tunnel), add every
   name in the chain, not just the one you git-clone with: Cilium's `toFQDNs` DNS
   proxy strips a CNAME answer unless every name in it is itself allowlisted. Find
   the chain with `dig +noall +answer <your-host>`.

After your own `flux bootstrap` run, `clusters/vicegerent/flux-system/gotk-*.yaml`
will diverge from this repo's copies to point at your fork — that's expected (Flux
regenerates them per-target), not something to reconcile back upstream.

## Create the local Kind cluster

Prerequisites:

- macOS with Docker (Kind runs its node as a container)
- `kind`
- `cilium-cli`
- `kubectl`
- `flux`
- `helm`
- `jq`
- SSH access (with write access) to your fork — see "Forking this repo" above

Create the cluster (creates the Kind cluster on its docker network and installs Cilium):

```bash
./vicegerent cluster setup
```

Verify the cluster and CNI:

```bash
kubectl --context kind-vicegerent get nodes -o wide
kubectl --context kind-vicegerent get pods -n kube-system
cilium status --context kind-vicegerent
kubectl --context kind-vicegerent top nodes
```

If metrics are not ready immediately, wait a minute and rerun `kubectl --context kind-vicegerent top nodes`.

## Secrets setup

Cluster secrets are plain Kubernetes Secrets — Kind etcd is the source of truth, and
no secret values live in git. The setup scripts generate crypto material (CAs,
certificates, SSH keys, random tokens) and read user-supplied API keys from the
environment or interactive prompts, then `kubectl apply` the Secrets directly.
They are provisioned in two passes: **platform-wide** material (shared by the whole
cluster) and **per-agent** material (one set per named agent). Both are idempotent —
generated material already present is reused, and re-running reseeds a fresh cluster.

MCP-server API keys are the exception: they are `thv` (ToolHive) secrets on the host,
not Kubernetes Secrets. Configure them with `vicegerent mcp configure` (see
[`host/mcp`](host/mcp)), not the scripts below.

> Secrets are treated as disposable/recreatable. There is no external secret store
> in the loop, so **keep your own copy of any API keys** — re-running a setup script
> is how you rebuild the cluster's secrets after a `kind delete cluster`. (A Velero
> backup of the Secrets is a planned follow-up.)

### Platform-wide

Generates the ghostunnel CA + server/client certificates and the egress-proxy MITM
CA, and applies the shared model/search API keys. The host-side ghostunnel material
is written to `~/.vicegerent/ghostunnel` (override with `GHOSTUNNEL_HOST_DIR`); the
CA private key never enters Kubernetes. The server cert/key + CA cert are mirrored to
a `ghostunnel-server` Secret so a host missing them recovers on start.

```bash
export ANTHROPIC_API_KEY=sk-ant-...   # set any key to apply it non-interactively
./vicegerent secrets setup platform
```

```text
-y, --yes     auto-approve every change (non-interactive)
--force       rebuild the ghostunnel CA and all certificates from scratch
```

This applies these Kubernetes Secrets (and one ConfigMap):

```text
agentgateway-system  vicegerent-secrets         Authorization         (Anthropic API key)
agentgateway-system  vicegerent-openai-secrets  Authorization         (optional OpenAI key)
agentgateway-system  vicegerent-mcp-client      tls.crt, tls.key      (ghostunnel mTLS client cert)
agentgateway-system  ghostunnel-ca (ConfigMap)  ca.crt                (ghostunnel CA cert)
agentgateway-system  ghostunnel-server          server.crt/key,ca.crt (host recovery copy)
searxng              searxng-secret             secret_key            (session/limiter signing key)
egress-proxy         egress-proxy-ca            ca.crt, ca.key        (MITM CA private material)
agent-sandbox        egress-proxy-ca-cert       ca.crt                (MITM CA cert, trust only)
```

MCP-server API keys (tavily/firecrawl/gitlab) are **not** here — they are `thv`
secrets on the host (`vicegerent mcp configure`); notion/linear use OAuth.

The host-only ghostunnel files (`~/.vicegerent/ghostunnel`): `ca.cert`, `ca.key`,
`server.crt`, `server.key`, `client.crt`, `client.key`. The CA key stays host-side
so a re-run can re-issue a leaf without rebuilding the chain, and the host ghostunnel
server reads its material from here.

### Per-agent

Run once per named agent. Each agent gets its own independently generated dashboard
credentials, SSH key, and agentgateway bearer token — no material is shared between
agents.

```bash
./vicegerent secrets setup agent hermes   # accepts -y/--yes
```

This applies these Kubernetes Secrets in namespace `agent-sandbox` (agent `<name>`):

```text
<name>-secrets               password, signing-secret, public-key,
                             SLACK_BOT_TOKEN, SLACK_APP_TOKEN,
                             SLACK_ALLOWED_USERS, SLACK_HOME_CHANNEL (Slack optional)
<name>-agentgateway-api-key  api-key                 (random hex bearer token)
<name>-ssh-key               hermes_agent_ed25519    (ed25519 private key)
```

## Bootstrap Flux

Bootstrap the local Kind cluster against this repo. The script runs `flux
bootstrap git` and is idempotent — re-runs reconcile cleanly. Provision the secrets
(above) before or right after bootstrap so the workloads Flux reconciles have the
material they consume. It confirms before each change; pass `-y`/`--yes` for a
non-interactive run.

```bash
./vicegerent bootstrap
```

The script defaults to:

```text
KUBE_CONTEXT=kind-vicegerent
REPO_URL=<this checkout's 'origin' remote>
BRANCH=main
CLUSTER_PATH=./clusters/vicegerent
PRIVATE_KEY_FILE=$HOME/.ssh/id_rsa
```

Override those with environment variables if needed:

```bash
BRANCH=my-test-branch PRIVATE_KEY_FILE=$HOME/.ssh/id_ed25519 ./vicegerent bootstrap
```

Check reconciliation:

```bash
flux --context kind-vicegerent get all -A
kubectl --context kind-vicegerent get pods -A
```

The committed `gotk-sync.yaml` expects the bootstrap-created `flux-system` Git credential Secret.

## Host-side MCP control plane

Every MCP server runs on the laptop under ToolHive (`thv`) and is aggregated behind a
single Virtual MCP Server (vMCP) that ghostunnel exposes to the cluster over mTLS. The
control plane lives in [`host/mcp`](host/mcp): `vicegerent-mcp` brings up the ToolHive
workloads (kubernetes, gitlab, tavily, firecrawl, notion, linear — off by default) and
supervises the three long-lived host processes — `thv vmcp serve` (aggregates the group
on `127.0.0.1:4483`), `ghostunnel` (terminates cluster mTLS, listens `127.0.0.1:8453`,
forwards to the vMCP), and an opt-in `caffeinate` that keeps macOS awake while the stack
runs.

The cluster reaches the vMCP at `host.docker.internal:8453`; agentgateway carries a
`vmcp` `AgentgatewayBackend` and a single `/mcp/vmcp` HTTPRoute. Through the vMCP, tools
are named `{workload}_<tool>` (e.g. `kubernetes_resources_get`).

Enable and configure servers interactively (API keys become `thv` secrets; notion/linear
use browser OAuth):

```bash
./vicegerent mcp configure
```

Start and stop the whole local platform — the Kind cluster and the host MCP stack together — with the top-level commands:

```bash
./vicegerent start   # resume the Kind cluster, then bring up the host MCP stack
./vicegerent stop    # stop the host MCP stack (including ToolHive workloads), then pause the cluster
```

For finer control of just the host stack, drive it with `./vicegerent-mcp` (`start [--caffeinate]`, `stop`, `status`, `logs`, `doctor`, `configure`, `enable`/`disable`, `tui`); see [`host/mcp/README.md`](host/mcp/README.md) for the full reference.

```bash
./vicegerent-mcp start
./vicegerent-mcp status
```

## Dashboards

Each agent's Hermes dashboard is published on a Kind NodePort (pool `30119-30128`,
mapped to the host via kind `extraPortMappings`) and reachable directly at
`http://127.0.0.1:<nodePort>/`. Print the URL + basic-auth credentials, or open it:

```bash
./vicegerent agent creds hermes       # print URL + credentials
./vicegerent agent dashboard hermes   # open in a browser
```

VictoriaLogs (cluster-wide log aggregation) has no NodePort — it's opened via an
auto-torn-down port-forward instead:

```bash
./vicegerent logs dashboard   # port-forward + open the VictoriaLogs web UI (vmui)
```

## Development

Install and run the repo hooks before committing:

```bash
pre-commit install
pre-commit run --all-files
```

The local Flux validation hook expects `yq` v4, `kustomize`, `kubeconform`, and `curl` on `PATH`.
