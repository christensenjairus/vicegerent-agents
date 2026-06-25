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
- `ghostunnel`
- SSH access to `gitlab.hahomelabs.com:jchristensen/vicegerent-agents`

Create the cluster:

```bash
minikube start \
  --profile vicegerent \
  --driver vfkit \
  --container-runtime containerd \
  --cni=cilium \
  --addons gvisor,metrics-server \
  --cpus 4 \
  --memory 12288 \
  --disk-size 50g
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
export ANTHROPIC_API_KEY=sk-ant-...   # so the key can be stored non-interactively
./scripts/install/setup-secrets.sh
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
./scripts/install/install.sh
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
BRANCH=my-test-branch PRIVATE_KEY_FILE=$HOME/.ssh/id_ed25519 ./scripts/install/install.sh
```

Check reconciliation:

```bash
flux --context vicegerent get all -A
kubectl --context vicegerent get pods -A
```

The committed `gotk-sync.yaml` expects the bootstrap-created `flux-system` Git credential Secret.

## Run the host-side ghostunnel

The Kubernetes MCP backend runs on the host and is reached from agentgateway through ghostunnel mTLS. `setup-secrets.sh` already generated the certificates and stored them in 1Password; this step only runs the tunnel.

Run the tunnel after the plaintext Kubernetes MCP server is listening on `127.0.0.1:8080`. `ghostshell` pulls the server certificate, key, and CA from 1Password into an ephemeral tmpdir, runs ghostunnel, and wipes them on exit — nothing is persisted to disk:

```bash
cd /path/to/vicegerent-agents
./scripts/ghostunnel/ghostshell.sh
```

Defaults (override with environment variables):

```text
OP_VAULT=Vicegerent
OP_HOST_ITEM=Ghostunnel Host
HOST_ONLY_IP=192.168.64.1
LISTEN=192.168.64.1:8443
TARGET=127.0.0.1:8080
ALLOW_CN=agent-client
GHOSTUNNEL=ghostunnel
```

`--listen` must bind the host-only IP, not `0.0.0.0`. The `--allow-cn` value must match the client certificate CN. For vfkit the host-only IP is normally `192.168.64.1`; confirm with `ifconfig bridge100 | grep 'inet '`.

## Development

Install and run the repo hooks before committing:

```bash
pre-commit install
pre-commit run --all-files
```

The local Flux validation hook expects `yq` v4, `kustomize`, `kubeconform`, and `curl` on `PATH`.
