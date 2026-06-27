#!/usr/bin/env python3
"""Host-side MCP control helper for vicegerent.

This is intentionally a thin host helper, not a daemon. It renders the proven
mcp-proxy-server + mcp-remote + Caddy shape and reports/reset OAuth cache state.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = REPO_ROOT / "host" / "mcp" / "servers.json"
DEFAULT_RUNTIME_DIR = Path.home() / ".vicegerent" / "mcp"
DEFAULT_PROXY_DIR = Path.home() / "HomeLab" / "mcp-proxy-server"
DEFAULT_GHOSTSHELL = REPO_ROOT / "scripts" / "ghostunnel" / "ghostshell.sh"
DEFAULT_AUTH_DIR = Path.home() / ".mcp-auth"
DEFAULT_HOST_ONLY_IP = "192.168.64.1"
DEFAULT_HOST_MCP_TUNNEL_PORT = 8453
AUTH_FILENAMES = ("client_info.json", "code_verifier.txt", "tokens.json", "lock.json")


@dataclass(frozen=True)
class Server:
    key: str
    enabled: bool
    mode: str
    name: str
    url: str | None
    command: str
    args: list[str]
    env: dict[str, str]


def load_config(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise SystemExit(f"config must be a JSON object: {path}")
    data.setdefault("proxy", {})
    data.setdefault("servers", {})
    return data


def proxy_settings(config: dict[str, Any]) -> dict[str, Any]:
    proxy = dict(config.get("proxy") or {})
    proxy.setdefault("listen_host", "127.0.0.1")
    proxy.setdefault("proxy_port", 3663)
    proxy.setdefault("filtered_port", 3777)
    proxy.setdefault("disable_stdio_retries", True)
    return proxy


def iter_servers(config: dict[str, Any]) -> list[Server]:
    servers = []
    for key, raw in sorted((config.get("servers") or {}).items()):
        if not isinstance(raw, dict):
            raise SystemExit(f"server {key!r} must be an object")
        mode = raw.get("mode")
        command = raw.get("command")
        args = raw.get("args", [])
        env = raw.get("env", {})
        if not isinstance(args, list) or not all(isinstance(v, str) for v in args):
            raise SystemExit(f"server {key!r} args must be a list of strings")
        if not isinstance(env, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in env.items()):
            raise SystemExit(f"server {key!r} env must be an object of string keys and values")
        if not isinstance(command, str) or not command:
            raise SystemExit(f"server {key!r} command must be a non-empty string")
        servers.append(
            Server(
                key=key,
                enabled=bool(raw.get("enabled", True)),
                mode=str(mode),
                name=str(raw.get("name") or key),
                url=raw.get("url"),
                command=command,
                args=args,
                env=env,
            )
        )
    return servers


def mcp_remote_hash(server_url: str, authorize_resource: str | None = None, headers: dict[str, str] | None = None) -> str:
    """Match mcp-remote getServerUrlHash(): md5(parts.join('|'))."""
    parts = [server_url]
    if authorize_resource:
        parts.append(authorize_resource)
    if headers:
        sorted_keys = sorted(headers.keys())
        parts.append(json.dumps(headers, sort_keys=True, separators=(",", ":")))
        # json.dumps(sort_keys=True) matches semantic ordering; mcp-remote's value
        # includes default JSON spacing, but host configs here do not use headers.
        # Keep this branch explicit for future work instead of silently guessing.
        if sorted_keys:
            raise SystemExit("mcp-remote header hashing is not supported yet")
    return hashlib.md5("|".join(parts).encode("utf-8")).hexdigest()


def proxy_config(config: dict[str, Any]) -> dict[str, Any]:
    mcp_servers: dict[str, dict[str, Any]] = {}
    for server in iter_servers(config):
        if server.mode not in {"remote-oauth", "local-stdio"}:
            raise SystemExit(f"unsupported server mode for {server.key}: {server.mode}")
        command = server.command
        if command.startswith("scripts/") or command.startswith("host/"):
            command = str(REPO_ROOT / command)
        entry: dict[str, Any] = {
            "type": "stdio",
            "name": server.name,
            "active": server.enabled,
            "command": command,
            "args": server.args,
        }
        if server.env:
            entry["env"] = server.env
        mcp_servers[server.key] = entry
    return {"mcpServers": mcp_servers}


def caddyfile(config: dict[str, Any]) -> str:
    proxy = proxy_settings(config)
    host = proxy["listen_host"]
    filtered_port = int(proxy["filtered_port"])
    proxy_port = int(proxy["proxy_port"])
    return f"""{{
  admin off
  auto_https off
}}

http://{host}:{filtered_port} {{
  @mcp_post {{
    method POST
    path /mcp
  }}

  handle @mcp_post {{
    reverse_proxy {host}:{proxy_port}
  }}

  respond 404
}}
"""


def env_file(config: dict[str, Any]) -> str:
    proxy = proxy_settings(config)
    lines = [
        f"PORT={int(proxy['proxy_port'])}",
        "ENABLE_ADMIN_UI=true",
        "LOGGING=info",
    ]
    if proxy.get("disable_stdio_retries", True):
        lines.extend(["RETRY_STDIO_TOOL_CALL=false", "STDIO_TOOL_CALL_MAX_RETRIES=0"])
    return "\n".join(lines) + "\n"


def enabled_servers(config: dict[str, Any]) -> list[Server]:
    return [server for server in iter_servers(config) if server.enabled]


def runtime_paths(runtime_dir: Path) -> dict[str, Path]:
    return {
        "runtime": runtime_dir,
        "proxy_config_dir": runtime_dir / "mcp-proxy-server" / "config",
        "caddyfile": runtime_dir / "caddy" / "Caddyfile",
        "env": runtime_dir / "proxy.env",
        "logs": runtime_dir / "logs",
        "pids": runtime_dir / "pids",
        "admin_password": runtime_dir / "admin_password",
    }


def render_runtime(config: dict[str, Any], runtime_dir: Path) -> dict[str, Path]:
    paths = runtime_paths(runtime_dir)
    paths["proxy_config_dir"].mkdir(parents=True, exist_ok=True)
    paths["caddyfile"].parent.mkdir(parents=True, exist_ok=True)
    paths["logs"].mkdir(parents=True, exist_ok=True)
    paths["pids"].mkdir(parents=True, exist_ok=True)
    write_json(paths["proxy_config_dir"] / "mcp_server.json", proxy_config(config))
    tool_config = paths["proxy_config_dir"] / "tool_config.json"
    if not tool_config.exists():
        write_json(tool_config, {"tools": {}})
    paths["caddyfile"].write_text(caddyfile(config), encoding="utf-8")
    paths["env"].write_text(env_file(config), encoding="utf-8")
    return paths


def parse_env_file(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key] = value
    return env


def get_or_create_admin_password(path: Path) -> str:
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    password = secrets.token_urlsafe(24)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(password + "\n", encoding="utf-8")
    path.chmod(0o600)
    return password


def read_pid(path: Path) -> int | None:
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except Exception:
        return None


def pid_running(path: Path) -> bool:
    pid = read_pid(path)
    return bool(pid and pid_alive(pid))


def start_process(name: str, command: list[str], cwd: Path | None, env: dict[str, str], runtime_dir: Path) -> bool:
    paths = runtime_paths(runtime_dir)
    pidfile = paths["pids"] / f"{name}.pid"
    if pid_running(pidfile):
        print(f"{name}: already running (pid {read_pid(pidfile)})")
        return False
    logfile = paths["logs"] / f"{name}.log"
    logfile.parent.mkdir(parents=True, exist_ok=True)
    out = logfile.open("ab")
    proc = subprocess.Popen(command, cwd=str(cwd) if cwd else None, env=env, stdout=out, stderr=subprocess.STDOUT, start_new_session=True)
    pidfile.write_text(str(proc.pid) + "\n", encoding="utf-8")
    print(f"{name}: started pid {proc.pid}; log {logfile}")
    time.sleep(0.5)
    if proc.poll() is not None:
        pidfile.unlink(missing_ok=True)
        raise SystemExit(f"{name}: exited immediately with code {proc.returncode}; see {logfile}")
    return True


def stop_process(name: str, runtime_dir: Path, timeout: float = 8.0) -> None:
    paths = runtime_paths(runtime_dir)
    pidfile = paths["pids"] / f"{name}.pid"
    pid = read_pid(pidfile)
    if not pid:
        print(f"{name}: not running (no pid file)")
        return
    if not pid_alive(pid):
        print(f"{name}: stale pid {pid}")
        pidfile.unlink(missing_ok=True)
        return
    print(f"{name}: stopping pid {pid}")
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        pidfile.unlink(missing_ok=True)
        return
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not pid_alive(pid):
            pidfile.unlink(missing_ok=True)
            print(f"{name}: stopped")
            return
        time.sleep(0.2)
    print(f"{name}: still running after SIGTERM; sending SIGKILL")
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    pidfile.unlink(missing_ok=True)


def copy_proxy_config(runtime_dir: Path, proxy_dir: Path) -> None:
    src = runtime_paths(runtime_dir)["proxy_config_dir"]
    dst = proxy_dir / "config"
    dst.mkdir(parents=True, exist_ok=True)
    for file in src.iterdir():
        if file.is_file():
            shutil.copy2(file, dst / file.name)


def ensure_proxy_binds_loopback(proxy_dir: Path, host: str) -> None:
    """Patch mcp-proxy-server's HTTP listener to loopback.

    Upstream listens on all interfaces. The host stack must keep the raw admin UI
    local-only and expose only the filtered Caddy port to ghostunnel.
    """
    changed: list[Path] = []
    candidates = [proxy_dir / "src" / "sse.ts", proxy_dir / "build" / "sse.js"]
    pattern = re.compile(r"expressServer\.listen\(\s*PORT\s*,\s*\(\)\s*=>\s*\{")
    desired = f"expressServer.listen(Number(PORT), {host!r}, () => {{"
    for path in candidates:
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        if desired in text:
            continue
        new_text, count = pattern.subn(desired, text, count=1)
        if count:
            path.write_text(new_text, encoding="utf-8")
            changed.append(path)
        elif "expressServer.listen" in text:
            raise SystemExit(f"could not patch listener in {path}; inspect mcp-proxy-server before exposing it")
    for path in changed:
        print(f"patched {path} to bind {host}")

    build = proxy_dir / "build" / "sse.js"
    if not build.exists():
        raise SystemExit(f"mcp-proxy-server build not found: {build}")
    if desired not in build.read_text(encoding="utf-8"):
        raise SystemExit(f"mcp-proxy-server build is not bound to {host}: {build}")

    source = proxy_dir / "src" / "sse.ts"
    if source.exists() and desired not in source.read_text(encoding="utf-8"):
        print(f"warning: source listener was not patched; npm run build may restore a non-loopback bind: {source}", file=sys.stderr)


def default_tunnel_listen() -> str:
    host_only_ip = os.environ.get("HOST_ONLY_IP", DEFAULT_HOST_ONLY_IP)
    return f"{host_only_ip}:{DEFAULT_HOST_MCP_TUNNEL_PORT}"


def cmd_render(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    runtime_dir: Path = args.runtime_dir
    paths = render_runtime(config, runtime_dir)

    print(f"rendered runtime files under {runtime_dir}")
    print(f"mcp-proxy config: {paths['proxy_config_dir'] / 'mcp_server.json'}")
    print(f"caddy config:     {paths['caddyfile']}")
    print("\nNext steps:")
    print(f"  mkdir -p config && cp -R {paths['proxy_config_dir']}/. ./config/")
    print(f"  set -a; source {paths['env']}; set +a")
    print("  export ADMIN_USERNAME=admin ADMIN_PASSWORD='<local password>' SESSION_SECRET='<random hex>'")
    print("  # Leave ALLOWED_KEYS unset for the ghostunnel/Caddy path; ghostunnel mTLS + Caddy path filtering gate access.")
    print("  node build/sse.js")
    print(f"  caddy run --config {paths['caddyfile']}")
    return 0


def write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def auth_prefixes(server: Server) -> list[str]:
    if server.mode != "remote-oauth" or not server.url:
        return []
    return [mcp_remote_hash(server.url)]


def auth_files(prefix: str, auth_dir: Path) -> list[Path]:
    matches: list[Path] = []
    for version_dir in sorted(auth_dir.glob("mcp-remote-*")):
        for name in AUTH_FILENAMES:
            candidate = version_dir / f"{prefix}_{name}"
            if candidate.exists():
                matches.append(candidate)
    return matches


def read_json(path: Path) -> Any | None:
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def auth_state(server: Server, auth_dir: Path) -> tuple[str, list[Path]]:
    prefixes = auth_prefixes(server)
    files = [p for prefix in prefixes for p in auth_files(prefix, auth_dir)]
    has_client_info = any(p.name.endswith("_client_info.json") for p in files)
    has_code_verifier = any(p.name.endswith("_code_verifier.txt") for p in files)
    token_files = [p for p in files if p.name.endswith("_tokens.json")]
    lock_files = [p for p in files if p.name.endswith("_lock.json")]
    if token_files:
        token = read_json(token_files[0]) or {}
        if token.get("access_token") and token.get("refresh_token"):
            return "authenticated", files
        return "auth-needed", files
    for lock in lock_files:
        data = read_json(lock) or {}
        pid = data.get("pid")
        if isinstance(pid, int) and pid_alive(pid):
            return "auth-in-progress", files
    if has_client_info or has_code_verifier:
        return "auth-incomplete", files
    return "unknown", files


def cmd_auth_status(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    servers = {s.key: s for s in iter_servers(config)}
    selected = [servers[args.server]] if args.server else list(servers.values())
    for server in selected:
        if server.mode != "remote-oauth":
            print(f"{server.key}: local-stdio")
            continue
        state, files = auth_state(server, args.auth_dir)
        print(f"{server.key}: {state}")
        if server.url:
            print(f"  url: {server.url}")
            print(f"  mcp-remote hash: {mcp_remote_hash(server.url)}")
        for path in files:
            print(f"  {path}")
    return 0


def process_matches() -> list[str]:
    try:
        proc = subprocess.run(["pgrep", "-af", "mcp-remote|node .*build/sse\\.js"], check=False, capture_output=True, text=True)
    except FileNotFoundError:
        return []
    lines = []
    for line in proc.stdout.splitlines():
        if str(os.getpid()) in line:
            continue
        if "pgrep -af" in line or "vicegerent_mcp.py" in line or "scripts/host/vicegerent-mcp" in line:
            continue
        if "mcp-remote" in line or "node build/sse.js" in line or "node " in line and "build/sse.js" in line:
            lines.append(line)
    return lines


def cmd_auth_reset(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    servers = {s.key: s for s in iter_servers(config)}
    server = servers[args.server]
    if server.mode != "remote-oauth" or not server.url:
        raise SystemExit(f"{server.key} is not a remote-oauth server")
    matches = process_matches()
    if matches and not args.force:
        print("Refusing to delete OAuth cache while MCP processes may be alive:", file=sys.stderr)
        for line in matches:
            print(f"  {line}", file=sys.stderr)
        print("Stop proxy/backend first, or pass --force if you know these are unrelated.", file=sys.stderr)
        return 2
    files = auth_files(mcp_remote_hash(server.url), args.auth_dir)
    if not files:
        print(f"no auth files found for {server.key}")
        return 0
    if not args.yes:
        print(f"would delete {len(files)} auth file(s) for {server.key}:")
        for path in files:
            print(f"  {path}")
        print("rerun with --yes to delete")
        return 1
    for path in files:
        path.unlink(missing_ok=True)
        print(f"deleted {path}")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    proxy = proxy_settings(config)
    print("host MCP doctor")
    for binary in ("node", "npx", "caddy", "ghostunnel", "op"):
        found = shutil.which(binary)
        print(f"{binary}: {found or 'MISSING'}")
    # k8s-mcp-server lives in-repo; check the built binary
    repo_root = Path(__file__).resolve().parents[2]
    k8s_bin = repo_root / "host" / "k8s-mcp-server" / "k8s-mcp-server"
    if k8s_bin.exists():
        print(f"k8s-mcp-server: {k8s_bin}")
    else:
        print(f"k8s-mcp-server: MISSING (run: make -C host/k8s-mcp-server)")
    print(f"proxy port:    {proxy['proxy_port']}")
    print(f"filtered port: {proxy['filtered_port']}")
    print(f"auth dir:      {args.auth_dir}")
    print()
    ns = argparse.Namespace(config=args.config, auth_dir=args.auth_dir, server=None)
    return cmd_auth_status(ns)


def cmd_start(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    active = enabled_servers(config)
    if not active:
        print("no enabled MCP servers; not starting proxy/tunnel")
        return 0
    runtime_dir: Path = args.runtime_dir
    proxy_dir: Path = args.proxy_dir
    if not (proxy_dir / "build" / "sse.js").exists():
        raise SystemExit(f"mcp-proxy-server build not found: {proxy_dir / 'build' / 'sse.js'}")
    paths = render_runtime(config, runtime_dir)
    copy_proxy_config(runtime_dir, proxy_dir)
    proxy = proxy_settings(config)
    ensure_proxy_binds_loopback(proxy_dir, str(proxy["listen_host"]))

    base_env = os.environ.copy()
    base_env.update(parse_env_file(paths["env"]))
    base_env.setdefault("ADMIN_USERNAME", "admin")
    base_env.setdefault("ADMIN_PASSWORD", get_or_create_admin_password(paths["admin_password"]))
    base_env.setdefault("SESSION_SECRET", secrets.token_hex(32))
    # Deliberately leave ALLOWED_KEYS unset by default. The cluster path is gated by
    # ghostunnel mTLS and Caddy exposes only POST /mcp.
    base_env.pop("ALLOWED_KEYS", None)

    started: list[str] = []
    try:
        if start_process("proxy", ["node", "build/sse.js"], proxy_dir, base_env, runtime_dir):
            started.append("proxy")
        if start_process("caddy", ["caddy", "run", "--config", str(paths["caddyfile"])], None, base_env, runtime_dir):
            started.append("caddy")

        tunnel_env = base_env.copy()
        # Never inherit TARGET/LISTEN from the user's shell: those are the security boundary.
        tunnel_env["TARGET"] = f"{proxy['listen_host']}:{int(proxy['filtered_port'])}"
        tunnel_env["LISTEN"] = args.listen or default_tunnel_listen()
        if args.allow_cn:
            tunnel_env["ALLOW_CN"] = args.allow_cn
        if args.ghostshell:
            ghostshell = args.ghostshell
        else:
            ghostshell = DEFAULT_GHOSTSHELL
        if start_process("ghostunnel", [str(ghostshell)], REPO_ROOT, tunnel_env, runtime_dir):
            started.append("ghostunnel")
    except BaseException:
        for name in reversed(started):
            stop_process(name, runtime_dir)
        raise

    print("enabled servers: " + ", ".join(server.key for server in active))
    print(f"raw proxy admin: http://{proxy['listen_host']}:{int(proxy['proxy_port'])}/admin")
    print(f"filtered MCP endpoint: http://{proxy['listen_host']}:{int(proxy['filtered_port'])}/mcp")
    print(f"ghostunnel listen: {tunnel_env['LISTEN']}")
    print(f"ghostunnel target: {tunnel_env['TARGET']}")
    return 0


def cmd_stop(args: argparse.Namespace) -> int:
    # Stop in reverse dependency order: tunnel -> filter -> proxy.
    for name in ("ghostunnel", "caddy", "proxy"):
        stop_process(name, args.runtime_dir)
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    paths = runtime_paths(args.runtime_dir)
    for name in ("proxy", "caddy", "ghostunnel"):
        pidfile = paths["pids"] / f"{name}.pid"
        pid = read_pid(pidfile)
        if pid and pid_alive(pid):
            print(f"{name}: running pid {pid}")
        elif pid:
            print(f"{name}: stale pid {pid}")
        else:
            print(f"{name}: stopped")
    print()
    return cmd_auth_status(argparse.Namespace(config=args.config, auth_dir=args.auth_dir, server=None))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="vicegerent host MCP helper")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--auth-dir", type=Path, default=DEFAULT_AUTH_DIR)
    sub = parser.add_subparsers(dest="command", required=True)

    render = sub.add_parser("render", help="render mcp-proxy-server and Caddy config")
    render.add_argument("--runtime-dir", type=Path, default=DEFAULT_RUNTIME_DIR)
    render.set_defaults(func=cmd_render)

    start = sub.add_parser("start", help="render config and start proxy, Caddy, and ghostunnel")
    start.add_argument("--runtime-dir", type=Path, default=DEFAULT_RUNTIME_DIR)
    start.add_argument("--proxy-dir", type=Path, default=DEFAULT_PROXY_DIR)
    start.add_argument("--ghostshell", type=Path, default=DEFAULT_GHOSTSHELL)
    start.add_argument("--listen", default=None, help=f"ghostunnel listen address (default: $HOST_ONLY_IP:{DEFAULT_HOST_MCP_TUNNEL_PORT}, HOST_ONLY_IP defaults to {DEFAULT_HOST_ONLY_IP})")
    start.add_argument("--allow-cn", default=None, help="ghostunnel client certificate CN (default: ghostshell.sh default)")
    start.set_defaults(func=cmd_start)

    stop = sub.add_parser("stop", help="stop ghostunnel, Caddy, and proxy started by this helper")
    stop.add_argument("--runtime-dir", type=Path, default=DEFAULT_RUNTIME_DIR)
    stop.set_defaults(func=cmd_stop)

    proc_status = sub.add_parser("status", help="show process and auth state")
    proc_status.add_argument("--runtime-dir", type=Path, default=DEFAULT_RUNTIME_DIR)
    proc_status.set_defaults(func=cmd_status)

    status = sub.add_parser("auth-status", help="show mcp-remote OAuth cache state")
    status.add_argument("server", nargs="?")
    status.set_defaults(func=cmd_auth_status)

    reset = sub.add_parser("auth-reset", help="delete OAuth cache for a server after stopping proxy/backend")
    reset.add_argument("server")
    reset.add_argument("--yes", action="store_true")
    reset.add_argument("--force", action="store_true", help="delete even if matching MCP processes are running")
    reset.set_defaults(func=cmd_auth_reset)

    doctor = sub.add_parser("doctor", help="show host prerequisites and auth state")
    doctor.set_defaults(func=cmd_doctor)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "auth-status" and args.server:
        servers = {s.key: s for s in iter_servers(load_config(args.config))}
        if args.server not in servers:
            raise SystemExit(f"unknown server: {args.server}")
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
