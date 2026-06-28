#!/usr/bin/env bash
# Open a Hermes agent dashboard through a local authenticated proxy.
set -euo pipefail

OP_VAULT="${OP_VAULT:-Vicegerent}"
NAMESPACE="${HERMES_DASHBOARD_NAMESPACE:-agent-sandbox}"
LOCAL_PORT="${HERMES_DASHBOARD_LOCAL_PORT:-}"
MINIKUBE_PROFILE="${MINIKUBE_PROFILE:-vicegerent}"
DASHBOARD_PATH="${HERMES_DASHBOARD_PATH:-/}"
[[ "$DASHBOARD_PATH" == /* ]] || DASHBOARD_PATH="/$DASHBOARD_PATH"
CONTEXT_ARG=()
if [[ -n "${KUBECONFIG_CONTEXT:-${KUBE_CONTEXT:-}}" ]]; then
  CONTEXT_ARG=(--context "${KUBECONFIG_CONTEXT:-${KUBE_CONTEXT:-}}")
elif kubectl config get-contexts vicegerent >/dev/null 2>&1; then
  CONTEXT_ARG=(--context vicegerent)
fi

usage() {
  echo "usage: $0 <agent-name>" >&2
  echo "  opens that agent's Hermes dashboard with auth already attached" >&2
  exit 2
}

[[ $# -eq 1 ]] || usage
AGENT="$1"
[[ -n "$AGENT" ]] || usage
SERVICE="${HERMES_DASHBOARD_SERVICE:-${AGENT}-dashboard}"

for cmd in kubectl op python3; do
  command -v "$cmd" >/dev/null 2>&1 || { echo "$cmd is required" >&2; exit 1; }
done
op account get >/dev/null 2>&1 || { echo "1Password CLI is not signed in. Run: op signin" >&2; exit 1; }

ITEM="Dashboard Auth - ${AGENT}"
PASSWORD="$(op read "op://${OP_VAULT}/${ITEM}/password")"
[[ -n "$PASSWORD" ]] || {   echo "Missing op://${OP_VAULT}/${ITEM}/password. Run: ./vicegerent secrets setup (with DASHBOARD_AUTH_AGENTS including '${AGENT}')." >&2; exit 1; }

node_port="$(kubectl "${CONTEXT_ARG[@]}" -n "$NAMESPACE" get svc "$SERVICE" -o jsonpath='{.spec.ports[?(@.name=="dashboard")].nodePort}' 2>/dev/null || true)"
if [[ -z "$node_port" ]]; then
  node_port="$(kubectl "${CONTEXT_ARG[@]}" -n "$NAMESPACE" get svc "$SERVICE" -o jsonpath='{.spec.ports[0].nodePort}' 2>/dev/null || true)"
fi
[[ -n "$node_port" ]] || { echo "Could not find NodePort on service ${NAMESPACE}/${SERVICE}." >&2; exit 1; }
[[ -n "$LOCAL_PORT" ]] || LOCAL_PORT="$node_port"

if command -v minikube >/dev/null 2>&1; then
  node_ip="$(minikube -p "$MINIKUBE_PROFILE" ip 2>/dev/null || true)"
else
  node_ip=""
fi
if [[ -z "$node_ip" ]]; then
  node_ip="$(kubectl "${CONTEXT_ARG[@]}" get nodes -o jsonpath='{.items[0].status.addresses[?(@.type=="InternalIP")].address}' 2>/dev/null || true)"
fi
[[ -n "$node_ip" ]] || { echo "Could not determine minikube/node IP." >&2; exit 1; }

backend="http://${node_ip}:${node_port}"
tmpdir="$(mktemp -d "${TMPDIR:-/tmp}/vicegerent-hermes-dashboard.XXXXXX")"
cleanup() {
  [[ -n "${proxy_pid:-}" ]] && kill "$proxy_pid" >/dev/null 2>&1 || true
  rm -rf "$tmpdir"
}
trap cleanup EXIT INT TERM

proxy_script="$tmpdir/hermes-dashboard-auth-proxy.py"
cat >"$proxy_script" <<'PY'
#!/usr/bin/env python3
import argparse
import http.cookiejar
import http.server
import json
import select
import socket
import socketserver
import sys
import urllib.error
import urllib.parse
import urllib.request

HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "content-encoding",
    "content-length",
}

class ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True

class AuthProxy(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    backend = None
    cookie_header = None
    proxy_base = None

    def log_message(self, fmt, *args):
        sys.stderr.write("hermes-dashboard-proxy: " + fmt % args + "\n")

    def do_GET(self): self._proxy()
    def do_POST(self): self._proxy()
    def do_PUT(self): self._proxy()
    def do_PATCH(self): self._proxy()
    def do_DELETE(self): self._proxy()
    def do_HEAD(self): self._proxy(head=True)

    def _proxy(self, head=False):
        if self.headers.get("Upgrade", "").lower() == "websocket":
            self._proxy_websocket()
            return
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None
        url = self.backend + self.path
        headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP_BY_HOP and k.lower() != "host"}
        headers["Host"] = urllib.parse.urlparse(self.backend).netloc
        headers["Cookie"] = self.cookie_header
        headers["Accept-Encoding"] = "identity"
        headers.setdefault("X-Forwarded-Host", urllib.parse.urlparse(self.proxy_base).netloc)
        headers.setdefault("X-Forwarded-Proto", "http")
        req = urllib.request.Request(url, data=body, headers=headers, method=self.command)
        try:
            resp = urllib.request.urlopen(req, timeout=60)
            status = resp.status
            reason = resp.reason
            data = b"" if head else resp.read()
            resp_headers = resp.headers
        except urllib.error.HTTPError as e:
            status = e.code
            reason = e.reason
            data = b"" if head else e.read()
            resp_headers = e.headers
        self.send_response(status, reason)
        for k, v in resp_headers.items():
            lk = k.lower()
            if lk in HOP_BY_HOP or lk == "set-cookie":
                continue
            if lk == "location":
                v = v.replace(self.backend, self.proxy_base)
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if not head:
            self.wfile.write(data)

    def _proxy_websocket(self):
        parsed = urllib.parse.urlparse(self.backend)
        host = parsed.hostname
        port = parsed.port or 80
        with socket.create_connection((host, port), timeout=30) as upstream:
            upstream.settimeout(None)
            headers = []
            for k, v in self.headers.items():
                lk = k.lower()
                if lk in {"host", "cookie"}:
                    continue
                headers.append((k, v))
            headers.append(("Host", parsed.netloc))
            headers.append(("Cookie", self.cookie_header))
            headers.append(("X-Forwarded-Host", urllib.parse.urlparse(self.proxy_base).netloc))
            headers.append(("X-Forwarded-Proto", "http"))
            request = f"{self.command} {self.path} {self.request_version}\r\n" + "".join(
                f"{k}: {v}\r\n" for k, v in headers
            ) + "\r\n"
            upstream.sendall(request.encode("iso-8859-1"))
            sockets = [self.connection, upstream]
            while True:
                readable, _, _ = select.select(sockets, [], [], 60)
                for sock in readable:
                    data = sock.recv(65536)
                    if not data:
                        return
                    (upstream if sock is self.connection else self.connection).sendall(data)


def login(backend, username, password):
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    payload = json.dumps({
        "provider": "basic",
        "username": username,
        "password": password,
        "next": "/",
    }).encode()
    req = urllib.request.Request(
        backend + "/auth/password-login",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    resp = opener.open(req, timeout=30)
    body = resp.read().decode()
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"password login returned non-JSON response: {body!r}") from exc
    if not parsed.get("ok"):
        raise RuntimeError(f"password login failed: {body!r}")
    cookies = "; ".join(f"{c.name}={c.value}" for c in jar)
    if not cookies:
        raise RuntimeError("login succeeded but no cookies were captured")
    # Validate the session through the authenticated endpoint before opening a browser.
    me_req = urllib.request.Request(backend + "/api/auth/me", headers={"Cookie": cookies})
    me = opener.open(me_req, timeout=30).read().decode()
    if username.lower() not in me.lower():
        raise RuntimeError(f"login did not produce a session for {username}; /api/auth/me={me!r}")
    return cookies


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", required=True)
    ap.add_argument("--listen-port", type=int, required=True)
    ap.add_argument("--username", required=True)
    ap.add_argument("--password", required=True)
    args = ap.parse_args()
    backend = args.backend.rstrip("/")
    AuthProxy.backend = backend
    AuthProxy.proxy_base = f"http://127.0.0.1:{args.listen_port}"
    AuthProxy.cookie_header = login(backend, args.username, args.password)
    print(AuthProxy.proxy_base, flush=True)
    ThreadingHTTPServer(("127.0.0.1", args.listen_port), AuthProxy).serve_forever()

if __name__ == "__main__":
    main()
PY
chmod +x "$proxy_script"

python3 "$proxy_script" --backend "$backend" --listen-port "$LOCAL_PORT" --username "$AGENT" --password "$PASSWORD" >"$tmpdir/proxy.url" 2>"$tmpdir/proxy.log" &
proxy_pid=$!

for _ in $(seq 1 80); do
  [[ -s "$tmpdir/proxy.url" ]] && break
  if ! kill -0 "$proxy_pid" >/dev/null 2>&1; then
    echo "Hermes dashboard auth proxy exited:" >&2
    cat "$tmpdir/proxy.log" >&2
    exit 1
  fi
  sleep 0.25
done
url="$(cat "$tmpdir/proxy.url")"
[[ -n "$url" ]] || { echo "Hermes dashboard auth proxy did not report a URL" >&2; cat "$tmpdir/proxy.log" >&2; exit 1; }
url="${url%/}${DASHBOARD_PATH}"

if command -v open >/dev/null 2>&1; then
  open "$url"
elif command -v xdg-open >/dev/null 2>&1; then
  xdg-open "$url" >/dev/null 2>&1 || true
else
  echo "Open this URL: $url"
fi

echo "Hermes dashboard (${AGENT}): $url"
echo "Backend: $backend"
echo "Authenticated as: $AGENT"
echo "Press Ctrl-C to stop the authenticated proxy."
wait "$proxy_pid"
