# Setup

Full step-by-step for standing up your own instance. See the [README](../README.md) for the condensed quickstart and an overview of what you're setting up.

## Forking this repo

This repo is meant to be forked, not shared — each person runs their own fork against their own laptop and their own local Kind cluster. Nothing here is multi-tenant: two people bootstrapping the same repo would each try to push Flux's generated manifests back to it and fight over the same git history.

### Values to change for your fork

This repo ships with the original operator's own identity baked into a few files as concrete values, not templated placeholders — Flux will happily reconcile them as-is, so nothing fails loudly if you miss one. Go through this list before (or right after) your first bootstrap:

- **`clusters/personal/cluster-vars.yaml`** — every field here is machine/operator-scoped and feeds the Cerbos authorization policies via Flux substitution. Read the inline comment on each field, then replace:
  - `githubAllowedRepos` — the GitHub `owner/repo` pairs your agents are allowed to touch (`resource_github.yaml`). Currently someone else's repos.
  - `jiraAllowedProjects` — ships as the literal placeholder `"CHANGE"`; set to a CEL-list-ready set of Jira project keys your agent may WRITE to (reads are unrestricted) — or leave it if you don't use Jira.
  - `linearAllowedTeams` — your Linear team's UUID, display name, and issue-key prefix (`resource_linear.yaml`).
  - `cnameChainedFQDN` / `cnameChain` — your git host's FQDN and its full CNAME chain (see step 3 below). Currently the original operator's self-hosted GitLab + dynamic-DNS chain.
  - `apexWildcardDomains` / `exactOnlyDomains` — the external HTTP(S) destinations your agents' egress-proxy should allow. Add/remove as your workflow needs.
  - `grafanaDeniedDatasourceUids` / `grafanaDeniedDatasourceNames` — the Grafana datasource(s) (by uid or name) your agents may not read from (`resource_grafana.yaml`), CEL-list-ready. Currently someone else's OpenSearch datasource.
  - `notionScratchpadPageId` — the Notion page id that `notion-create-pages` force-rewrites every new page's parent to (`infrastructure/controllers/mcp-cerbos-shim/mapping.yaml`'s `force` block, substituted the same way as the Cerbos policy fields above). Currently someone else's Notion page.
- **`apps/personal/agents/hermes/values.yaml`** — `git.userName` / `git.userEmail` are the identity the agent commits as (e.g. when Flux-driven changes get pushed back). Currently someone else's name and email.
- **Container registry** (`charts/agent/values.yaml`, `infrastructure/controllers/mcp-cerbos-shim/deployment.yaml`, `apps/base/gateway/gateway.yaml`) — these point at the original operator's Harbor registry (`harbor.hahomelabs.com/vicegerent/...`), which is public to pull from, so you can leave these as-is and bootstrap directly against it. Only repoint them if you want to build and host your own copies of `hermes-agent`, `mcp-cerbos-shim`, or the agentgateway proxy image — see each image's README under `images/*/README.md` for the build & push steps.
- **`.gitlab-ci.yml` / `renovate.json`** — wired for this repo's own self-hosted GitLab instance (`RENOVATE_PLATFORM: gitlab`, `RENOVATE_REPOSITORIES`, `RENOVATE_ENDPOINT`, the `harbor.hahomelabs.com` CI runner images). If you fork to GitHub or a different GitLab instance, this file won't run as-is — either adapt it to your CI platform or ignore it; nothing in the platform itself depends on it (it's validation/dependency-update tooling, not part of the reconciled cluster state).

Steps 1-3 below get the cluster up; the fork-identity values above are what make the Cerbos policies, agent commits, and egress allowlist actually reflect *your* setup instead of the original operator's.

To stand up your own instance:

1. Fork the repo (GitHub, your own self-hosted git, wherever) and clone your fork. `./vicegerent bootstrap` / `install.sh` default `REPO_URL` to this checkout's `origin` remote, so cloning your own fork is enough — no env override needed.
2. Make sure the SSH key you'll bootstrap with (`PRIVATE_KEY_FILE`, default `~/.ssh/id_rsa`) has **write** access to your fork — `flux bootstrap git` commits its generated manifests back to it.
3. If your git host isn't `github.com`, add it to the agent sandbox's SSH egress allowlist in `charts/agent/templates/networkpolicy.yaml` (the `toFQDNs` block under the "SSH bypasses the HTTP proxy" comment) — otherwise Cilium blocks git-over-SSH from inside the sandbox. If that host itself resolves through a CNAME (common for self-hosted setups behind dynamic DNS or a tunnel), add every name in the chain, not just the one you git-clone with: Cilium's `toFQDNs` DNS proxy strips a CNAME answer unless every name in it is itself allowlisted. Find the chain with `dig +noall +answer <your-host>`.

One fork can drive many machines — each is a separate `clusters/<machine>/` + `apps/<machine>/` pair in the same git history, not a new fork per machine (see [Adding a second machine](#adding-a-second-machine) below). This is still not multi-tenant: each pair is bootstrapped to its own Kind cluster, and only that machine writes Flux's generated manifests back under its own `clusters/<machine>/`.

After your own `flux bootstrap` run, `clusters/personal/flux-system/gotk-*.yaml` will diverge from this repo's copies to point at your fork — that's expected (Flux regenerates them per-target), not something to reconcile back upstream.

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

Cluster secrets are plain Kubernetes Secrets — Kind etcd is the source of truth, and no secret values live in git. The setup scripts generate crypto material (CAs, certificates, SSH keys, random tokens) and read user-supplied API keys from the environment or interactive prompts, then `kubectl apply` the Secrets directly. They are provisioned in two passes: **platform-wide** material (shared by the whole cluster) and **per-agent** material (one set per named agent). Both are idempotent — generated material already present is reused, and re-running reseeds a fresh cluster.

MCP-server API keys are the exception: they are `thv` (ToolHive) secrets on the host, not Kubernetes Secrets. Configure them with `vicegerent mcp configure` (see [`host/mcp`](../host/mcp)), not the scripts below.

> Secrets are treated as disposable/recreatable. There is no external secret store in the loop, so **keep your own copy of any API keys** — re-running a setup script is how you rebuild the cluster's secrets after a `kind delete cluster`. (A Velero backup of the Secrets is a planned follow-up.)

### Platform-wide

Generates the ghostunnel CA + server/client certificates and the egress-proxy MITM CA, and applies the shared model API keys and the SearXNG signing key. The host-side ghostunnel material is written to `~/.vicegerent/ghostunnel` (override with `GHOSTUNNEL_HOST_DIR`); the CA private key never enters Kubernetes. The server cert/key + CA cert are mirrored to a `ghostunnel-server` Secret so a host missing them recovers on start.

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

MCP-server API keys (tavily/firecrawl/gitlab) are **not** here — they are `thv` secrets on the host (`vicegerent mcp configure`); notion/linear use OAuth.

The host-only ghostunnel files (`~/.vicegerent/ghostunnel`): `ca.cert`, `ca.key`, `server.crt`, `server.key`, `client.crt`, `client.key`. The CA key stays host-side so a re-run can re-issue a leaf without rebuilding the chain, and the host ghostunnel server reads its material from here.

### Per-agent

Run once per named agent. Each agent gets its own independently generated dashboard credentials and SSH key — no material is shared between agents.

```bash
./vicegerent secrets setup agent hermes   # accepts -y/--yes
```

This applies these Kubernetes Secrets in namespace `agent-sandbox` (agent `<name>`):

```text
<name>-secrets               password, signing-secret, public-key,
                             SLACK_BOT_TOKEN, SLACK_APP_TOKEN,
                             SLACK_ALLOWED_USERS, SLACK_HOME_CHANNEL (Slack optional)
<name>-ssh-key               hermes_agent_ed25519    (ed25519 private key)
```

## Bootstrap Flux

Bootstrap the local Kind cluster against this repo. The script runs `flux bootstrap git` and is idempotent — re-runs reconcile cleanly. Provision the secrets (above) before or right after bootstrap so the workloads Flux reconciles have the material they consume. It confirms before each change; pass `-y`/`--yes` for a non-interactive run.

```bash
./vicegerent bootstrap
```

The script defaults to:

```text
KUBE_CONTEXT=kind-vicegerent
REPO_URL=<this checkout's 'origin' remote>
BRANCH=main
CLUSTER_PATH=./clusters/personal
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

## Adding a second machine

One fork drives as many machines as you like — each is its own Kind cluster with its own `clusters/<machine>/` (Flux entrypoint + `cluster-vars.yaml`) and `apps/<machine>/` (a thin overlay that pulls in `apps/base` plus that machine's own `agents/`). The shared platform under `apps/base/` is not duplicated. The first machine is `personal`; to stand up another — say `macbook-office`:

1. Copy the two directory trees and rename them:

   ```bash
   cp -r clusters/personal clusters/macbook-office
   cp -r apps/personal apps/macbook-office
   ```

2. Repoint and re-scope the copy:

   - In `clusters/macbook-office/apps.yaml`, set `path: ./apps/macbook-office`.
   - In `clusters/macbook-office/cluster-vars.yaml`, set this machine's GitHub repos, Jira project, Linear team, and git host (each value is documented inline in that file).
   - Add or remove agent folders under `apps/macbook-office/agents/` as that machine needs (copy `hermes` for a new agent).

3. Create the cluster, provision secrets, and bootstrap Flux against the new path. The `vicegerent` CLI honors `CLUSTER_NAME` / `CLUSTER_PATH` / `KUBE_CONTEXT` overrides, and `kind create --name` (from `CLUSTER_NAME`) names the cluster regardless of `kind-config.yaml`'s `name:`:

   ```bash
   export CLUSTER_NAME=macbook-office
   export CLUSTER_PATH=./clusters/macbook-office
   export KUBE_CONTEXT=kind-macbook-office
   ./vicegerent cluster setup
   ./vicegerent secrets setup platform
   ./vicegerent secrets setup agent hermes
   ./vicegerent bootstrap
   ```

The new `clusters/macbook-office/flux-system/gotk-*.yaml` are placeholders until that machine's own `flux bootstrap` regenerates them. `scripts/install/kind-config.yaml`'s NodePort pool (`30119-30128`) only needs editing if two of your clusters run on the same host at once — a single laptop running one cluster can leave it as-is.

## Host-side MCP control plane

Every MCP server runs on the laptop under ToolHive (`thv`) and is aggregated behind a single Virtual MCP Server (vMCP) that ghostunnel exposes to the cluster over mTLS. The control plane lives in [`host/mcp`](../host/mcp): `vicegerent-mcp` brings up the 11 ToolHive workloads declared in `toolhive-servers.json` (kubernetes, github, gitlab, tavily, firecrawl, notion, linear, jira, grafana, alertmanager, pagerduty — all off by default) and supervises the three long-lived host processes — `thv vmcp serve` (aggregates the group on `127.0.0.1:4483`), `ghostunnel` (terminates cluster mTLS, listens `127.0.0.1:8453`, forwards to the vMCP), and an opt-in `caffeinate` that keeps macOS awake while the stack runs.

The cluster reaches the vMCP at `host.docker.internal:8453`; agentgateway carries a `vmcp` `AgentgatewayBackend` and a single `/mcp/vmcp` HTTPRoute. Through the vMCP, tools are named `{workload}_<tool>` (e.g. `kubernetes_resources_get`).

First-time setup installs the host prerequisites (`thv`, `ghostunnel`, `supervisor`, and the Python venv `vicegerent-mcp` runs under):

```bash
./vicegerent mcp setup
```

Then enable and configure servers interactively (API keys become `thv` secrets; notion/linear use browser OAuth):

```bash
./vicegerent mcp configure
```

Start and stop the whole local platform — the Kind cluster and the host MCP stack together — with the top-level commands:

```bash
./vicegerent start   # resume the Kind cluster, then bring up the host MCP stack
./vicegerent stop    # stop the host MCP stack (including ToolHive workloads), then pause the cluster
```

For finer control of just the host stack, drive it with `./vicegerent-mcp` (`start [--caffeinate]`, `stop`, `status`, `logs`, `doctor`, `configure`, `enable`/`disable`, `tui`); see [`host/mcp/README.md`](../host/mcp/README.md) for the full reference.

```bash
./vicegerent-mcp start
./vicegerent-mcp status
```

## Dashboards

Each agent's Hermes dashboard is published on a Kind NodePort (pool `30119-30128`, mapped to the host via kind `extraPortMappings`) and reachable directly at `http://127.0.0.1:<nodePort>/`. Print the URL + basic-auth credentials, or open it:

```bash
./vicegerent agent creds hermes       # print URL + credentials
./vicegerent agent dashboard hermes   # open in a browser
```

VictoriaLogs (cluster-wide log aggregation) has no NodePort — it's opened via an auto-torn-down port-forward instead:

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
