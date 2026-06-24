# vicegerent

GitOps repository for the **vicegerent** infra agent platform — credential-isolated, egress-locked Hermes agent sandboxes on a local minikube cluster, managed by Flux.

See the design docs in Obsidian (`01-Infrastructure/2026-06-20-readonly-infra-agent-sandbox-design.md`) for the architecture and threat model.

## Create the local minikube cluster

Prerequisites:

- macOS with minikube + vfkit installed
- `kubectl`
- `flux`
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
  --memory 8192 \
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

## Bootstrap Flux

Bootstrap the local minikube cluster against this repo:

```bash
./tools/install/install.sh
```

The script defaults to:

```text
KUBE_CONTEXT=vicegerent
REPO_URL=ssh://git@gitlab.hahomelabs.com/jchristensen/vicegerent-agents.git
BRANCH=main
CLUSTER_PATH=./clusters/vicegerent
PRIVATE_KEY_FILE=$HOME/.ssh/id_rsa
```

Override those with environment variables if needed:

```bash
BRANCH=my-test-branch PRIVATE_KEY_FILE=$HOME/.ssh/id_ed25519 ./tools/install/install.sh
```

Check reconciliation:

```bash
flux --context vicegerent get all -A
kubectl --context vicegerent get pods -A
```

The committed `gotk-sync.yaml` expects the bootstrap-created `flux-system` Git credential Secret.

## Development

Install and run the repo hooks before committing:

```bash
pre-commit install
pre-commit run --all-files
```

The local Flux validation hook expects `yq` v4, `kustomize`, `kubeconform`, and `curl` on `PATH`.
