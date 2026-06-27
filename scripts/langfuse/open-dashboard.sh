#!/usr/bin/env bash
# Open the self-hosted Langfuse dashboard through a local authenticated proxy.
set -euo pipefail

VAULT="${OP_VAULT:-Vicegerent}"
ITEM="${LANGFUSE_OP_ITEM:-Langfuse}"
NAMESPACE="${LANGFUSE_NAMESPACE:-langfuse}"
SERVICE="${LANGFUSE_SERVICE:-langfuse-web}"
BACKEND_PORT="${LANGFUSE_FORWARD_PORT:-3000}"
PROXY_PORT="${LANGFUSE_DASHBOARD_PORT:-3001}"
DASHBOARD_PATH="${LANGFUSE_DASHBOARD_PATH:-/project/vicegerent}"
[[ "$DASHBOARD_PATH" == /* ]] || DASHBOARD_PATH="/$DASHBOARD_PATH"
EMAIL="${LANGFUSE_EMAIL:-admin@vicegerent.local}"
CONTEXT_ARG=()
if [[ -n "${KUBECONFIG_CONTEXT:-${KUBE_CONTEXT:-}}" ]]; then
  CONTEXT_ARG=(--context "${KUBECONFIG_CONTEXT:-${KUBE_CONTEXT:-}}")
elif kubectl config get-contexts vicegerent >/dev/null 2>&1; then
  CONTEXT_ARG=(--context vicegerent)
fi

for cmd in kubectl op python3; do
  command -v "$cmd" >/dev/null 2>&1 || { echo "$cmd is required" >&2; exit 1; }
done
op account get >/dev/null 2>&1 || { echo "1Password CLI is not signed in. Run: op signin" >&2; exit 1; }

PASSWORD="$(op read "op://${VAULT}/${ITEM}/init-user-password")"
[[ -n "$PASSWORD" ]] || { echo "Missing op://${VAULT}/${ITEM}/init-user-password. Run scripts/install/setup-secrets.sh." >&2; exit 1; }

tmpdir="$(mktemp -d "${TMPDIR:-/tmp}/vicegerent-langfuse.XXXXXX")"
cleanup() {
  [[ -n "${pf_pid:-}" ]] && kill "$pf_pid" >/dev/null 2>&1 || true
  rm -rf "$tmpdir"
}
trap cleanup EXIT INT TERM

kubectl "${CONTEXT_ARG[@]}" -n "$NAMESPACE" port-forward "svc/${SERVICE}" "127.0.0.1:${BACKEND_PORT}:3000" >"$tmpdir/port-forward.log" 2>&1 &
pf_pid=$!

for _ in $(seq 1 80); do
  if python3 - "$BACKEND_PORT" <<'PY' >/dev/null 2>&1
import socket, sys
s=socket.create_connection(("127.0.0.1", int(sys.argv[1])), timeout=0.25)
s.close()
PY
  then
    break
  fi
  if ! kill -0 "$pf_pid" >/dev/null 2>&1; then
    echo "kubectl port-forward exited:" >&2
    cat "$tmpdir/port-forward.log" >&2
    exit 1
  fi
  sleep 0.25
done

proxy_script="$tmpdir/langfuse-auth-proxy.py"
cat >"$proxy_script" <<'PY'
#!/usr/bin/env python3
import argparse
import http.cookiejar
import http.server
import json
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
        sys.stderr.write("langfuse-proxy: " + fmt % args + "\n")

    def do_GET(self): self._proxy()
    def do_POST(self): self._proxy()
    def do_PUT(self): self._proxy()
    def do_PATCH(self): self._proxy()
    def do_DELETE(self): self._proxy()
    def do_HEAD(self): self._proxy(head=True)

    def _proxy(self, head=False):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None
        url = self.backend + self.path
        headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP_BY_HOP and k.lower() != "host"}
        headers["Host"] = urllib.parse.urlparse(self.backend).netloc
        headers["Cookie"] = self.cookie_header
        headers["Accept-Encoding"] = "identity"
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
            if lk in HOP_BY_HOP:
                continue
            if lk == "location":
                v = v.replace(self.backend, self.proxy_base)
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if not head:
            self.wfile.write(data)


def login(backend, email, password):
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    csrf_resp = opener.open(backend + "/api/auth/csrf", timeout=30).read()
    csrf = json.loads(csrf_resp.decode())["csrfToken"]
    form = urllib.parse.urlencode({
        "csrfToken": csrf,
        "email": email,
        "password": password,
        "callbackUrl": backend + "/",
        "json": "true",
    }).encode()
    req = urllib.request.Request(
        backend + "/api/auth/callback/credentials",
        data=form,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        opener.open(req, timeout=30).read()
    except urllib.error.HTTPError as e:
        # NextAuth may return a redirect-ish status while still setting cookies.
        if e.code not in (302, 303):
            raise
    session = opener.open(backend + "/api/auth/session", timeout=30).read().decode()
    if email.lower() not in session.lower():
        raise RuntimeError(f"login did not produce a session for {email}; /api/auth/session={session!r}")
    cookies = "; ".join(f"{c.name}={c.value}" for c in jar)
    if not cookies:
        raise RuntimeError("login succeeded but no cookies were captured")
    return cookies


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", required=True)
    ap.add_argument("--listen-port", type=int, required=True)
    ap.add_argument("--email", required=True)
    ap.add_argument("--password", required=True)
    args = ap.parse_args()
    backend = args.backend.rstrip("/")
    AuthProxy.backend = backend
    AuthProxy.proxy_base = f"http://127.0.0.1:{args.listen_port}"
    AuthProxy.cookie_header = login(backend, args.email, args.password)
    print(AuthProxy.proxy_base, flush=True)
    ThreadingHTTPServer(("127.0.0.1", args.listen_port), AuthProxy).serve_forever()

if __name__ == "__main__":
    main()
PY
chmod +x "$proxy_script"

python3 "$proxy_script" --backend "http://127.0.0.1:${BACKEND_PORT}" --listen-port "$PROXY_PORT" --email "$EMAIL" --password "$PASSWORD" >"$tmpdir/proxy.url" 2>"$tmpdir/proxy.log" &
proxy_pid=$!
trap 'kill "${proxy_pid:-}" >/dev/null 2>&1 || true; cleanup' EXIT INT TERM

for _ in $(seq 1 80); do
  [[ -s "$tmpdir/proxy.url" ]] && break
  if ! kill -0 "$proxy_pid" >/dev/null 2>&1; then
    echo "Langfuse auth proxy exited:" >&2
    cat "$tmpdir/proxy.log" >&2
    exit 1
  fi
  sleep 0.25
done
url="$(cat "$tmpdir/proxy.url")"
[[ -n "$url" ]] || { echo "Langfuse auth proxy did not report a URL" >&2; cat "$tmpdir/proxy.log" >&2; exit 1; }
url="${url%/}${DASHBOARD_PATH}"

if command -v open >/dev/null 2>&1; then
  open "$url"
elif command -v xdg-open >/dev/null 2>&1; then
  xdg-open "$url" >/dev/null 2>&1 || true
else
  echo "Open this URL: $url"
fi

echo "Langfuse dashboard: $url"
echo "Authenticated as: $EMAIL"
echo "Press Ctrl-C to stop the authenticated proxy and kubectl port-forward."
wait "$proxy_pid"
