# Hermes dashboard — host access tunnel

Use the **full** Hermes dashboard (sessions, kanban, chat, plugins) from your
laptop against an egress-sealed agent sandbox, over a **persistent** mTLS
tunnel — no `kubectl port-forward`, survives pod and laptop restarts.

## Why it's built this way

Two independent auth layers protect the dashboard, and the design wires them so
both can be active at once:

**Network layer — mTLS.** The dashboard is exposed off the egress-sealed pod
over **two ghostunnel hops**, so the only way onto the wire is a valid client
cert:

```
Hermes Desktop ──► 127.0.0.1:9119 (host ghostunnel client)
                      │  mTLS
                      ▼
                 nodeIP:30119  (NodePort)
                      │
                      ▼
            pod: ghostunnel server :8443 ──► 127.0.0.1:9119 (dashboard)
```

**App layer — basic-auth login.** The dashboard binds non-loopback
(`HERMES_DASHBOARD_HOST=0.0.0.0`) inside the pod, which engages Hermes' auth
gate. Hermes' bundled **`basic` dashboard-auth provider** (username + password,
scrypt-hashed, no external IDP — works fully offline in the sealed pod) then
satisfies the gate, so Desktop shows a real login form. No `--insecure`.

The ghostunnel server still targets `127.0.0.1:9119`, so every request the
dashboard sees comes from loopback and passes its DNS-rebinding `Host:` guard.
mTLS stops you reaching the dashboard at all without a cert; basic auth stops a
process that *is* on the wire (e.g. another local app on your Mac, behind the
host tunnel) from driving the agent without the password.

## One-time setup

1. **Issue the tunnel certs** (idempotent; reuses the existing ghostunnel CA):
   ```sh
   ./scripts/install/setup-secrets.sh
   ```
   This populates the 1Password **"Dashboard Tunnel"** item with `server.crt`/
   `server.key` (synced into the cluster for the pod sidecar) and `client.crt`/
   `client.key` (pulled to the host by the tunnel script), plus `ca.cert`.

   The server cert's SAN must cover the minikube node IP your host dials.
   `setup-secrets.sh` auto-detects it from `minikube -p vicegerent ip` (the
   driver sets the range — vfkit/qemu give `192.168.64.x`, docker gives
   `192.168.49.x`). If the node IP ever changes, just re-run the script: it
   detects a SAN mismatch and re-issues the server cert automatically. Override
   with `DASHBOARD_TUNNEL_IP=<ip>` only if auto-detection can't reach minikube.

2. **Deploy** the manifests (Flux picks them up): the `hermes-dashboard-tunnel`
   NodePort Service + the ghostunnel sidecar are in `apps/vicegerent/agents/hermes/`.

3. **Install the host tunnel** (`ghostunnel` + 1Password CLI required):
   ```sh
   brew install ghostunnel
   op signin
   ```

## Run it

**Foreground (interactive):**
```sh
./scripts/dashboard-tunnel/dashboard-tunnel.sh
```

**Persistent (launchd, survives reboots):**
```sh
# 1. Replace the REPLACE_ME paths in the plist with absolute paths.
cp scripts/dashboard-tunnel/com.vicegerent.dashboard-tunnel.plist \
   ~/Library/LaunchAgents/com.vicegerent.dashboard-tunnel.plist
# 2. Load it.
launchctl load -w ~/Library/LaunchAgents/com.vicegerent.dashboard-tunnel.plist
# Stop/unload:
launchctl unload -w ~/Library/LaunchAgents/com.vicegerent.dashboard-tunnel.plist
```

Then point **Hermes Desktop → Remote gateway** at `http://127.0.0.1:9119`.

## Multiple agents

Each agent gets its own NodePort and its own local port. The mTLS server cert is
shared across agents (one node IP, one `dashboard-tunnel` Secret), so adding an
agent needs no new cert. To add `agent2`:

1. Copy `agents/hermes` and give the new Sandbox a unique
   `vicegerent.io/dashboard-tunnel: <agent2>` pod label + a Service (e.g.
   `agent2-dashboard-tunnel`) with `nodePort: 30120`. It mounts the same shared
   `dashboard-tunnel` Secret — no `setup-secrets.sh` re-run required.
2. Add a line to the `AGENTS` array in `dashboard-tunnel.sh`:
   ```sh
   AGENTS=(
     "hermes:9119:30119"
     "agent2:9120:30120"
   )
   ```

`127.0.0.1:9119` is hermes, `127.0.0.1:9120` is agent2, and so on — exactly the
`9119/9120/9121…` model, one Desktop connection per local port.

## Login credentials

Each agent has its own dashboard login backed by its **own** 1Password item
(`Dashboard Auth - <agent>`) with a random password — mounted only into that
agent's pod via a per-agent `OnePasswordItem` → Secret → `secretKeyRef`. No
shared salt, no derivation: one agent physically cannot read or compute
another's credentials. The username is the sandbox name. Retrieve an agent's
credentials on the host:

```sh
./scripts/dashboard-tunnel/dashboard-basic-cred.sh hermes
# username: hermes
# password: <random>
```

The password is stable across pod restarts (it's a stored random value, not
regenerated). Add an agent by appending its name to `DASHBOARD_AUTH_AGENTS` in
`setup-secrets.sh` (it mints a fresh per-agent item) and pointing the new pod's
`secretKeyRef` at that agent's Secret. Enter username + password in the Hermes
Desktop login form after the tunnel is up.
