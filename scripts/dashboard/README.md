# Hermes dashboard — host access

Use the **full** Hermes dashboard (sessions, kanban, chat, plugins) from your
laptop against an egress-sealed agent sandbox — no `kubectl port-forward`,
survives pod and laptop restarts.

## How access is controlled

The dashboard binds non-loopback (`HERMES_DASHBOARD_HOST=0.0.0.0`) inside the
pod and is exposed on a NodePort. Two layers gate it:

**App layer — basic-auth login.** Binding non-loopback engages Hermes' auth
gate. Hermes' bundled **`basic` dashboard-auth provider** (username + password,
scrypt-hashed, no external IDP — works fully offline in the sealed pod) then
satisfies the gate, so Desktop shows a real login form. No `--insecure`.

**Network layer — ingress NetworkPolicy.** `hermes-dashboard-ingress` allows the
dashboard port (9119) only from off-cluster (`world`) + node-local probes. Other
agent pods carry in-cluster pod identities, never `world`, so they cannot reach
`9119` (dashboard) or `8642` (gateway API) on the pod IP.

```
Hermes Desktop ──► nodeIP:30119 (NodePort) ──► pod :9119 (dashboard, basic-auth)
```

> **Transport is plain HTTP** over the minikube host-only network
> (`192.168.64.x` / `192.168.49.x`), which only your laptop and the cluster VM
> sit on. The basic-auth password crosses that wire in cleartext. This is an
> accepted trade for a single-user homelab on an isolated host-only network. If
> you ever expose an agent beyond it (Tailscale, LoadBalancer, anything
> routable), put TLS termination back in front before doing so.

## One-time setup

1. **Generate the per-agent login** (idempotent):
   ```sh
   ./scripts/install/setup-secrets.sh
   ```
   This mints the per-agent 1Password item `Dashboard Auth - <agent>` (random
   password + session-signing secret), synced into the cluster as a Secret and
   mounted only into that agent's pod.

2. **Deploy** the manifests (Flux picks them up): the `hermes-dashboard`
   NodePort Service + the ingress policy are in `apps/vicegerent/agents/hermes/`.

## Connect

Point **Hermes Desktop → Remote gateway** at `http://<nodeIP>:30119`, where
`<nodeIP>` is `minikube -p vicegerent ip`. Enter the agent's username + password
at the login form.

## Multiple agents

Each agent gets its own NodePort. To add `agent2`:

1. Copy `agents/hermes`, give the new Sandbox a unique
   `vicegerent.io/dashboard: <agent2>` pod label + a `hermes-dashboard`-style
   Service with `nodePort: 30120`.
2. Add the new agent's name to `DASHBOARD_AUTH_AGENTS` in `setup-secrets.sh`
   (mints a fresh per-agent login item) and point the new pod's `secretKeyRef`
   at that agent's Secret.

Connect Desktop to `http://<nodeIP>:30120` for agent2, and so on.

## Login credentials

Each agent has its own dashboard login backed by its **own** 1Password item
(`Dashboard Auth - <agent>`) with a random password — mounted only into that
agent's pod via a per-agent `OnePasswordItem` → Secret → `secretKeyRef`. No
shared salt, no derivation: one agent physically cannot read or compute
another's credentials. The username is the sandbox name. Retrieve an agent's
credentials on the host:

```sh
./scripts/dashboard/open-dashboard.sh hermes
```

The helper reads `Dashboard Auth - hermes` from 1Password, discovers the
agent's NodePort Service, starts a local authenticated proxy, and opens the
browser. If you only need to copy credentials manually:

```sh
./scripts/dashboard/dashboard-basic-cred.sh hermes
# username: hermes
# password: <random>
```

The password is stable across pod restarts (it's a stored random value, not
regenerated).
