# vicegerent

GitOps repository for the **vicegerent** infra agent platform — credential-isolated, egress-locked Hermes agent sandboxes on a local minikube cluster, managed by Flux.

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

Install `socket_vmnet` (one-time). The cluster uses `socket_vmnet` instead of
`vmnet-shared` for reliable networking across macOS sleep/wake cycles. The
`sudo brew services` form is required because `socket_vmnet` must run as root to
bind the vmnet interface, and the firewall rules allow it to hand out DHCP leases:

```bash
brew install socket_vmnet
HOMEBREW=$(which brew) && sudo ${HOMEBREW} services start socket_vmnet
sudo /usr/libexec/ApplicationFirewall/socketfilterfw --add /usr/libexec/bootpd
sudo /usr/libexec/ApplicationFirewall/socketfilterfw --unblock /usr/libexec/bootpd
```

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

Run the idempotent setup script. It creates the `Vicegerent` vault, the 1Password Connect server and token, the ghostunnel CA, and the server/client certificates — storing everything in 1Password and leaving nothing on disk. It is safe to re-run: anything already in 1Password is reused, never regenerated, and it only prompts before a step that actually changes something.

```bash
export ANTHROPIC_API_KEY=sk-ant-...   # set for any key to be stored non-interactively
./vicegerent secrets setup
```

```text
-y, --yes     auto-approve every change (non-interactive)
--force       rebuild the CA and all certificates from scratch
```

This provisions, in vault `Vicegerent`:

```text
Connect Credentials  1password-credentials.json   (Connect bootstrap)
Connect Token        token                         (operator token)
Runtime              Authorization, tls.crt, tls.key   (synced into the cluster)
MCP CA               ca.cert                       (public CA, synced into the cluster)
OpenAI               Authorization                 (optional OpenAI key, synced into the cluster)
Ghostunnel Host      server.crt, server.key, ca.cert, ca.key   (host-only, never synced)
```

The CA private key lives only in `Ghostunnel Host` so a re-run can re-issue a missing leaf certificate without rebuilding the chain. The server private key never enters Kubernetes. 1Password is the single source of truth for this material, for both the laptop and the cluster.

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

OAuth-backed or laptop-context MCPs run in the macOS GUI session and are exposed to the cluster through ghostunnel. The v1 host scaffold lives in [`host/mcp`](host/mcp): it renders `mcp-proxy-server` config, starts `mcp-proxy-server` + Caddy + a second ghostunnel, filters the host endpoint to `POST /mcp`, and includes helper commands for `mcp-remote` OAuth cache status/reset.

```bash
./vicegerent-mcp start --proxy-dir ~/HomeLab/mcp-proxy-server
./vicegerent-mcp status
```

The host-MCP tunnel defaults to `192.168.64.1:8453` so it can run beside the existing Kubernetes MCP tunnel on `:8443`. This scaffold is host-side only; add cluster-side MCP backend/route wiring before agents can call the new SaaS tools.

## Development

Install and run the repo hooks before committing:

```bash
pre-commit install
pre-commit run --all-files
```

The local Flux validation hook expects `yq` v4, `kustomize`, `kubeconform`, and `curl` on `PATH`.
