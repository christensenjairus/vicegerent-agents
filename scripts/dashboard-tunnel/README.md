# Hermes dashboard — host access tunnel

Use the **full** Hermes dashboard (sessions, kanban, chat, plugins) from your
laptop against an egress-sealed agent sandbox, over a **persistent** mTLS
tunnel — no `kubectl port-forward`, survives pod and laptop restarts.

## Why it's built this way

The dashboard inside the pod binds **loopback** (`HERMES_DASHBOARD_HOST=127.0.0.1`).
That is deliberate and not negotiable here:

- A non-loopback bind makes Hermes engage its **OAuth auth gate**, which has to
  reach an external portal to validate logins. The sandbox is egress-sealed, so
  the gate **fails closed** — the dashboard won't serve at all. The only escape
  is `--insecure`, which serves the session token to anyone who loads the page.
- The loopback dashboard also rejects any request whose `Host:` header isn't a
  loopback name (a DNS-rebinding defense). A browser/Desktop pointed straight at
  `nodeIP:port` would get a `400 Invalid Host header`.

So exposure is done with **two ghostunnel hops** that keep the dashboard on
loopback at both ends:

```
Hermes Desktop ──► 127.0.0.1:9119 (host ghostunnel client)
                      │  mTLS
                      ▼
                 nodeIP:30119  (NodePort)
                      │
                      ▼
            pod: ghostunnel server :8443 ──► 127.0.0.1:9119 (dashboard)
```

Every hop the dashboard sees presents `Host: 127.0.0.1`, so it serves normally.
**mTLS** (client cert) is the network auth; the **stable session token** is the
dashboard's own app-layer handshake (kept stable across restarts so a saved
Desktop connection keeps working).

## One-time setup

1. **Issue the tunnel certs** (idempotent; reuses the existing ghostunnel CA):
   ```sh
   ./scripts/install/setup-secrets.sh
   ```
   This populates the 1Password **"Dashboard Tunnel"** item with `server.crt`/
   `server.key` (synced into the cluster for the pod sidecar) and `client.crt`/
   `client.key` (pulled to the host by the tunnel script), plus `ca.cert`.

   The server cert's SAN must cover the minikube node IP your host dials. The
   default is `192.168.49.2`; if `minikube -p vicegerent ip` differs, re-issue
   with `DASHBOARD_TUNNEL_IP="$(minikube -p vicegerent ip)" ./scripts/install/setup-secrets.sh`.

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

Each agent gets its own NodePort and its own local port. To add `agent2`:

1. Copy `agents/hermes` and give the new Sandbox a unique
   `vicegerent.io/dashboard-tunnel: <agent2>` pod label + a `hermes-dashboard-tunnel`-style
   Service with `nodePort: 30120`.
2. Add a line to the `AGENTS` array in `dashboard-tunnel.sh`:
   ```sh
   AGENTS=(
     "hermes:9119:30119"
     "agent2:9120:30120"
   )
   ```

`127.0.0.1:9119` is hermes, `127.0.0.1:9120` is agent2, and so on — exactly the
`9119/9120/9121…` model, one Desktop connection per local port.

## Session token

The dashboard session token is derived deterministically from the sandbox name,
so it is stored nowhere — not in git, not in 1Password. The pod derives it at
boot from its own name (a baked `cont-init.d` hook), and you can compute the
same value on the host:

```sh
./scripts/dashboard-session-token.sh hermes
```

Because it's a pure function of the (public) sandbox name it stays stable across
pod restarts. If Desktop ever asks for the token explicitly, that command prints
it.
