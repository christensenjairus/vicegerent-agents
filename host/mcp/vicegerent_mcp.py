#!/usr/bin/env python3
"""Host-side MCP control helper for vicegerent.

Manages N stdio MCP servers via mcp-proxy-server with supervisord supervision,
hot-reload on enable/disable, and a rich CLI status display.
"""

from __future__ import annotations

import argparse
import hashlib
import http.cookiejar
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request
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


# ---------------------------------------------------------------------------
# Config & state
# ---------------------------------------------------------------------------


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


def load_state(state_path: Path) -> dict[str, bool]:
    """Return runtime enable/disable overrides. Missing key = use servers.json default."""
    if not state_path.exists():
        return {}
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
        return {k: bool(v) for k, v in (data.get("enabled") or {}).items()}
    except Exception:
        return {}


def save_state(state_path: Path, overrides: dict[str, bool]) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps({"enabled": overrides}, indent=2) + "\n", encoding="utf-8")


def iter_servers(config: dict[str, Any], state: dict[str, bool] | None = None) -> list[Server]:
    overrides = state or {}
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
        # Expand ~ in env values (e.g. KUBECONFIG=~/.kube/config)
        env = {k: str(Path(v).expanduser()) if v.startswith("~") else v for k, v in env.items()}
        enabled = overrides[key] if key in overrides else bool(raw.get("enabled", True))
        servers.append(
            Server(
                key=key,
                enabled=enabled,
                mode=str(mode),
                name=str(raw.get("name") or key),
                url=raw.get("url"),
                command=command,
                args=args,
                env=env,
            )
        )
    return servers


# ---------------------------------------------------------------------------
# Proxy config generation
# ---------------------------------------------------------------------------


def make_proxy_config(servers: list[Server]) -> dict[str, Any]:
    """Build the mcp-proxy-server JSON config from a server list."""
    mcp_servers: dict[str, dict[str, Any]] = {}
    for server in servers:
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
    return f"""\
{{
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


def make_proxy_env(config: dict[str, Any], admin_password: str, session_secret: str) -> dict[str, str]:
    """Return env vars for the proxy supervisord program."""
    proxy = proxy_settings(config)
    env: dict[str, str] = {
        "PORT": str(int(proxy["proxy_port"])),
        "ENABLE_ADMIN_UI": "true",
        "LOGGING": "info",
        "ADMIN_USERNAME": "admin",
        "ADMIN_PASSWORD": admin_password,
        "SESSION_SECRET": session_secret,
    }
    if proxy.get("disable_stdio_retries", True):
        env["RETRY_STDIO_TOOL_CALL"] = "false"
        env["STDIO_TOOL_CALL_MAX_RETRIES"] = "0"
    return env


# ---------------------------------------------------------------------------
# Runtime paths & supervisord config
# ---------------------------------------------------------------------------


def runtime_paths(runtime_dir: Path) -> dict[str, Path]:
    return {
        "runtime": runtime_dir,
        "proxy_config_dir": runtime_dir / "mcp-proxy-server" / "config",
        "caddyfile": runtime_dir / "caddy" / "Caddyfile",
        "logs": runtime_dir / "logs",
        "admin_password": runtime_dir / "admin_password",
        "session_secret": runtime_dir / "session_secret",
        "supervisord_conf": runtime_dir / "supervisord.conf",
        "supervisord_sock": runtime_dir / "supervisor.sock",
        "supervisord_pid": runtime_dir / "supervisord.pid",
        "state": runtime_dir / "state.json",
    }


def supervisord_env_str(env: dict[str, str]) -> str:
    """Format a dict as a supervisord environment= value."""
    parts = []
    for k, v in sorted(env.items()):
        v_escaped = v.replace("%", "%%").replace('"', '\\"')
        parts.append(f'{k}="{v_escaped}"')
    return ",".join(parts)


def build_supervisord_conf(
    paths: dict[str, Path],
    proxy_dir: Path,
    proxy_env: dict[str, str],
    ghostshell: Path,
    tunnel_env: dict[str, str],
) -> str:
    sock = paths["supervisord_sock"]
    pidfile = paths["supervisord_pid"]
    logs = paths["logs"]
    caddyfile_path = paths["caddyfile"]
    return f"""\
[unix_http_server]
file={sock}

[supervisord]
pidfile={pidfile}
logfile={logs}/supervisord.log
logfile_maxbytes=5MB
logfile_backups=2
loglevel=info
nodaemon=false
directory={REPO_ROOT}

[rpcinterface:supervisor]
supervisor.rpcinterface_factory = supervisor.rpcinterface:make_main_rpcinterface

[supervisorctl]
serverurl=unix://{sock}

[program:proxy]
command=node build/sse.js
directory={proxy_dir}
environment={supervisord_env_str(proxy_env)}
autostart=true
autorestart=true
startsecs=2
stopwaitsecs=8
redirect_stderr=true
stdout_logfile={logs}/proxy.log
stdout_logfile_maxbytes=5MB
stdout_logfile_backups=2

[program:caddy]
command=caddy run --config {caddyfile_path}
autostart=true
autorestart=true
startsecs=2
stopwaitsecs=8
redirect_stderr=true
stdout_logfile={logs}/caddy.log
stdout_logfile_maxbytes=5MB
stdout_logfile_backups=2

[program:ghostunnel]
command={ghostshell}
directory={REPO_ROOT}
environment={supervisord_env_str(tunnel_env)}
autostart=true
autorestart=true
startsecs=2
stopwaitsecs=8
redirect_stderr=true
stdout_logfile={logs}/ghostunnel.log
stdout_logfile_maxbytes=5MB
stdout_logfile_backups=2
"""


def get_or_create_admin_password(path: Path) -> str:
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    password = secrets.token_urlsafe(24)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(password + "\n", encoding="utf-8")
    path.chmod(0o600)
    return password


def get_or_create_session_secret(path: Path) -> str:
    """Persist the session secret so proxy restarts don't invalidate admin sessions."""
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    secret = secrets.token_hex(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(secret + "\n", encoding="utf-8")
    path.chmod(0o600)
    return secret


def render_runtime(config: dict[str, Any], servers: list[Server], runtime_dir: Path) -> dict[str, Path]:
    paths = runtime_paths(runtime_dir)
    paths["proxy_config_dir"].mkdir(parents=True, exist_ok=True)
    paths["caddyfile"].parent.mkdir(parents=True, exist_ok=True)
    paths["logs"].mkdir(parents=True, exist_ok=True)
    write_json(paths["proxy_config_dir"] / "mcp_server.json", make_proxy_config(servers))
    tool_config = paths["proxy_config_dir"] / "tool_config.json"
    if not tool_config.exists():
        write_json(tool_config, {"tools": {}})
    paths["caddyfile"].write_text(caddyfile(config), encoding="utf-8")
    return paths


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


def ensure_list_changed_notification(proxy_dir: Path) -> None:
    """Patch mcp-proxy-server to emit notifications/tools/list_changed after admin reload.

    Patches src/sse.ts then rebuilds so build/sse.js picks up the change.
    Idempotent: skips if already patched. Re-applies after an upstream npm rebuild.
    Chain: enable/disable -> admin reload -> list_changed -> agentgateway -> Hermes auto-refresh.
    """
    sentinel = "notifications/tools/list_changed"
    src = proxy_dir / "src" / "sse.ts"
    build = proxy_dir / "build" / "sse.js"

    if not src.exists():
        print(f"warning: {src} not found; skipping list_changed patch", file=sys.stderr)
        return

    text = src.read_text(encoding="utf-8")
    if sentinel in text:
        return  # already patched

    marker = "await updateBackendConnections(latestServerConfig, latestToolConfig);"
    if marker not in text:
        print(f"warning: could not find reload marker in {src}; skipping list_changed patch", file=sys.stderr)
        return

    notification_block = """\

            // Notify all connected MCP clients that the tool list has changed.
            // Hermes receives this via agentgateway and auto-refreshes its tool list.
            const listChangedNotification = {
              jsonrpc: '2.0' as const,
              method: 'notifications/tools/list_changed',
            };
            for (const transport of sseTransports.values()) {
              transport.send(listChangedNotification).catch((err: Error) => {
                logger.error('Failed to send list_changed to SSE client:', err);
              });
            }
            for (const transport of streamableHttpTransports.values()) {
              transport.send(listChangedNotification).catch((err: Error) => {
                logger.error('Failed to send list_changed to StreamableHTTP client:', err);
              });
            }"""

    patched = text.replace(marker, marker + notification_block, 1)
    src.write_text(patched, encoding="utf-8")
    print(f"patched {src} to emit notifications/tools/list_changed")

    print("rebuilding mcp-proxy-server...")
    result = subprocess.run(["npm", "run", "build"], cwd=str(proxy_dir), capture_output=True, text=True)
    if result.returncode != 0:
        print(f"warning: npm run build failed:\n{result.stderr}", file=sys.stderr)
    elif not build.exists() or sentinel not in build.read_text(encoding="utf-8"):
        print(f"warning: built {build} does not contain the list_changed notification", file=sys.stderr)
    else:
        print("rebuild complete")


def default_tunnel_listen() -> str:
    host_only_ip = os.environ.get("HOST_ONLY_IP", DEFAULT_HOST_ONLY_IP)
    return f"{host_only_ip}:{DEFAULT_HOST_MCP_TUNNEL_PORT}"


# ---------------------------------------------------------------------------
# Supervisor interaction
# ---------------------------------------------------------------------------


def supervisorctl_cmd(runtime_dir: Path, *args: str) -> subprocess.CompletedProcess[str]:
    conf = runtime_paths(runtime_dir)["supervisord_conf"]
    return subprocess.run(
        ["supervisorctl", "-c", str(conf), *args],
        capture_output=True, text=True, check=False,
    )


def get_supervisor_states(runtime_dir: Path) -> dict[str, str]:
    """Return {program_name: state_string} for all supervisord programs."""
    sock = runtime_paths(runtime_dir)["supervisord_sock"]
    if not sock.exists():
        return {}
    result = supervisorctl_cmd(runtime_dir, "status")
    states: dict[str, str] = {}
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            states[parts[0]] = parts[1]
    return states


def reload_proxy(runtime_dir: Path, config: dict[str, Any]) -> None:
    """Hot-reload mcp-proxy-server: login for session cookie, then POST reload.

    After updateBackendConnections(), the patched proxy sends list_changed to all
    connected MCP sessions so Hermes auto-refreshes its tool list.
    """
    proxy = proxy_settings(config)
    base = f"http://{proxy['listen_host']}:{proxy['proxy_port']}"
    password = get_or_create_admin_password(runtime_paths(runtime_dir)["admin_password"])

    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

    login_data = urllib.parse.urlencode({"username": "admin", "password": password}).encode()
    try:
        opener.open(f"{base}/admin/login", login_data, timeout=5)
    except Exception as e:
        print(f"  proxy reload: login failed: {e}")
        return

    try:
        resp = opener.open(
            urllib.request.Request(f"{base}/admin/server/reload", data=b"", method="POST"),
            timeout=10,
        )
        if resp.status == 200:
            print("  proxy reloaded — notifications/tools/list_changed sent to clients")
        else:
            print(f"  proxy reload returned HTTP {resp.status}")
    except Exception as e:
        print(f"  proxy reload failed: {e} — is the stack running?")


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------


def write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def mcp_remote_hash(server_url: str, authorize_resource: str | None = None, headers: dict[str, str] | None = None) -> str:
    """Match mcp-remote getServerUrlHash(): md5(parts.join('|'))."""
    parts = [server_url]
    if authorize_resource:
        parts.append(authorize_resource)
    if headers:
        sorted_keys = sorted(headers.keys())
        parts.append(json.dumps(headers, sort_keys=True, separators=(",", ":")))
        if sorted_keys:
            raise SystemExit("mcp-remote header hashing is not supported yet")
    return hashlib.md5("|".join(parts).encode("utf-8")).hexdigest()


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


# ---------------------------------------------------------------------------
# Rich display helpers
# ---------------------------------------------------------------------------


def _auth_display(server: Server, auth_dir: Path) -> str:
    if server.mode != "remote-oauth":
        return "n/a"
    state, _ = auth_state(server, auth_dir)
    return state


def _auth_style(state: str) -> str:
    if state == "authenticated":
        return f"[green]{state}[/green]"
    if state == "n/a":
        return f"[dim]{state}[/dim]"
    return f"[yellow]{state}[/yellow]"


def _proc_style(state: str) -> str:
    if state == "RUNNING":
        return f"[green]{state}[/green]"
    if state in ("STARTING", "BACKOFF"):
        return f"[yellow]{state}[/yellow]"
    if state in ("STOPPED", "EXITED", "FATAL", "UNKNOWN"):
        return f"[red]{state}[/red]"
    return f"[dim]{state or '—'}[/dim]"


def _require_rich() -> tuple[Any, Any]:
    try:
        from rich.console import Console
        from rich.table import Table
        return Console(), Table
    except ImportError:
        raise SystemExit("rich is not installed; run: pip install -r host/mcp/requirements-host.txt")


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_list(args: argparse.Namespace) -> int:
    """Show all configured MCP servers and their state (no process state needed)."""
    console, Table = _require_rich()
    config = load_config(args.config)
    state = load_state(runtime_paths(args.runtime_dir)["state"])
    servers = iter_servers(config, state)

    table = Table(title="Host MCP Servers", show_header=True, header_style="bold magenta")
    table.add_column("Server", style="bold")
    table.add_column("Mode")
    table.add_column("Auth")
    table.add_column("Enabled")
    for server in servers:
        enabled_str = "[green]yes[/green]" if server.enabled else "[dim]no[/dim]"
        table.add_row(server.key, server.mode, _auth_style(_auth_display(server, args.auth_dir)), enabled_str)
    console.print(table)
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    """Show server and process state as rich tables."""
    console, Table = _require_rich()
    config = load_config(args.config)
    state = load_state(runtime_paths(args.runtime_dir)["state"])
    servers = iter_servers(config, state)
    sup_states = get_supervisor_states(args.runtime_dir)

    srv_table = Table(title="Host MCP Servers", show_header=True, header_style="bold magenta")
    srv_table.add_column("Server", style="bold")
    srv_table.add_column("Mode")
    srv_table.add_column("Auth")
    srv_table.add_column("Enabled")
    for server in servers:
        enabled_str = "[green]yes[/green]" if server.enabled else "[dim]no[/dim]"
        srv_table.add_row(server.key, server.mode, _auth_style(_auth_display(server, args.auth_dir)), enabled_str)
    console.print(srv_table)

    inf_table = Table(title="Infrastructure Processes", show_header=True, header_style="bold cyan")
    inf_table.add_column("Process", style="bold")
    inf_table.add_column("State")
    for prog in ("proxy", "caddy", "ghostunnel"):
        inf_table.add_row(prog, _proc_style(sup_states.get(prog, "")))
    console.print(inf_table)
    return 0


def _update_enabled(args: argparse.Namespace, enabled: bool) -> int:
    config = load_config(args.config)
    paths = runtime_paths(args.runtime_dir)
    all_keys = {s.key for s in iter_servers(config)}
    if args.server not in all_keys:
        raise SystemExit(f"unknown server: {args.server}")

    state = load_state(paths["state"])
    state[args.server] = enabled
    save_state(paths["state"], state)

    servers = iter_servers(config, state)
    paths["proxy_config_dir"].mkdir(parents=True, exist_ok=True)
    write_json(paths["proxy_config_dir"] / "mcp_server.json", make_proxy_config(servers))
    copy_proxy_config(args.runtime_dir, args.proxy_dir)

    verb = "enabled" if enabled else "disabled"
    print(f"{verb} {args.server}")

    if paths["supervisord_sock"].exists():
        reload_proxy(args.runtime_dir, config)
    else:
        print("  stack not running — changes take effect on next start")
    return 0


def cmd_enable(args: argparse.Namespace) -> int:
    return _update_enabled(args, True)


def cmd_disable(args: argparse.Namespace) -> int:
    return _update_enabled(args, False)


def cmd_reload(args: argparse.Namespace) -> int:
    """Re-render proxy config from current state and hot-reload the proxy."""
    config = load_config(args.config)
    paths = runtime_paths(args.runtime_dir)
    state = load_state(paths["state"])
    servers = iter_servers(config, state)
    paths["proxy_config_dir"].mkdir(parents=True, exist_ok=True)
    write_json(paths["proxy_config_dir"] / "mcp_server.json", make_proxy_config(servers))
    copy_proxy_config(args.runtime_dir, args.proxy_dir)
    print("proxy config re-rendered")

    if paths["supervisord_sock"].exists():
        reload_proxy(args.runtime_dir, config)
    else:
        print("stack not running — changes take effect on next start")
    return 0


def cmd_render(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    paths = runtime_paths(args.runtime_dir)
    state = load_state(paths["state"])
    servers = iter_servers(config, state)
    render_runtime(config, servers, args.runtime_dir)
    print(f"rendered runtime files under {args.runtime_dir}")
    print(f"mcp-proxy config: {paths['proxy_config_dir'] / 'mcp_server.json'}")
    print(f"caddy config:     {paths['caddyfile']}")
    return 0


def cmd_start(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    paths = runtime_paths(args.runtime_dir)
    state = load_state(paths["state"])
    servers = iter_servers(config, state)
    active = [s for s in servers if s.enabled]

    if not active:
        print("no enabled MCP servers; not starting proxy/tunnel")
        return 0

    proxy_dir: Path = args.proxy_dir
    if not (proxy_dir / "build" / "sse.js").exists():
        raise SystemExit(f"mcp-proxy-server build not found: {proxy_dir / 'build' / 'sse.js'}")

    if paths["supervisord_sock"].exists():
        result = supervisorctl_cmd(args.runtime_dir, "status")
        if result.returncode == 0:
            print("supervisord is already running. Use 'reload' to update config or 'stop' first.")
            return 1

    proxy = proxy_settings(config)
    ensure_proxy_binds_loopback(proxy_dir, str(proxy["listen_host"]))
    ensure_list_changed_notification(proxy_dir)

    render_runtime(config, servers, args.runtime_dir)
    copy_proxy_config(args.runtime_dir, proxy_dir)

    admin_password = get_or_create_admin_password(paths["admin_password"])
    session_secret = get_or_create_session_secret(paths["session_secret"])
    proxy_env = make_proxy_env(config, admin_password, session_secret)

    ghostshell = args.ghostshell or DEFAULT_GHOSTSHELL
    listen = args.listen or default_tunnel_listen()
    tunnel_env: dict[str, str] = {
        "TARGET": f"{proxy['listen_host']}:{int(proxy['filtered_port'])}",
        "LISTEN": listen,
    }
    if args.allow_cn:
        tunnel_env["ALLOW_CN"] = args.allow_cn

    paths["logs"].mkdir(parents=True, exist_ok=True)
    conf_text = build_supervisord_conf(paths, proxy_dir, proxy_env, ghostshell, tunnel_env)
    paths["supervisord_conf"].write_text(conf_text, encoding="utf-8")

    subprocess.run(["supervisord", "-c", str(paths["supervisord_conf"])], check=True)

    # Wait for all programs to reach RUNNING
    deadline = time.time() + 10
    while time.time() < deadline:
        sup_states = get_supervisor_states(args.runtime_dir)
        if all(sup_states.get(p) == "RUNNING" for p in ("proxy", "caddy", "ghostunnel")):
            break
        time.sleep(0.5)

    sup_states = get_supervisor_states(args.runtime_dir)
    print("enabled servers: " + ", ".join(s.key for s in active))
    for prog in ("proxy", "caddy", "ghostunnel"):
        print(f"  {prog}: {sup_states.get(prog, 'unknown')}")
    print(f"filtered MCP endpoint: http://{proxy['listen_host']}:{int(proxy['filtered_port'])}/mcp")
    print(f"ghostunnel listen: {listen}")
    return 0


def cmd_stop(args: argparse.Namespace) -> int:
    sock = runtime_paths(args.runtime_dir)["supervisord_sock"]
    if not sock.exists():
        print("supervisord is not running")
        return 0
    result = supervisorctl_cmd(args.runtime_dir, "shutdown")
    print(result.stdout.strip() or "supervisord shutdown initiated")
    return 0


def cmd_auth_status(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    state = load_state(runtime_paths(args.runtime_dir)["state"])
    servers = {s.key: s for s in iter_servers(config, state)}
    selected = [servers[args.server]] if getattr(args, "server", None) else list(servers.values())
    for server in selected:
        if server.mode != "remote-oauth":
            print(f"{server.key}: local-stdio")
            continue
        st, files = auth_state(server, args.auth_dir)
        print(f"{server.key}: {st}")
        if server.url:
            print(f"  url: {server.url}")
            print(f"  mcp-remote hash: {mcp_remote_hash(server.url)}")
        for path in files:
            print(f"  {path}")
    return 0


def cmd_auth_reset(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    state = load_state(runtime_paths(args.runtime_dir)["state"])
    servers = {s.key: s for s in iter_servers(config, state)}
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
    for binary in ("node", "npx", "caddy", "ghostunnel", "op", "kubectl", "k8s-mcp-server", "supervisord", "supervisorctl"):
        found = shutil.which(binary)
        print(f"  {binary}: {found or 'MISSING'}")
    print(f"proxy port:    {proxy['proxy_port']}")
    print(f"filtered port: {proxy['filtered_port']}")
    print(f"auth dir:      {args.auth_dir}")
    print()
    ns = argparse.Namespace(config=args.config, auth_dir=args.auth_dir, runtime_dir=args.runtime_dir, server=None)
    return cmd_auth_status(ns)


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="vicegerent host MCP helper")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--auth-dir", type=Path, default=DEFAULT_AUTH_DIR)
    parser.add_argument("--runtime-dir", type=Path, default=DEFAULT_RUNTIME_DIR)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="show all configured MCP servers and their state").set_defaults(func=cmd_list)

    sub.add_parser("status", help="show server and infrastructure process state (rich tables)").set_defaults(func=cmd_status)

    for verb, fn, help_str in [
        ("enable", cmd_enable, "enable a server and hot-reload the proxy"),
        ("disable", cmd_disable, "disable a server and hot-reload the proxy"),
    ]:
        p = sub.add_parser(verb, help=help_str)
        p.add_argument("server")
        p.add_argument("--proxy-dir", type=Path, default=DEFAULT_PROXY_DIR)
        p.set_defaults(func=fn)

    rl = sub.add_parser("reload", help="re-render proxy config from current state and hot-reload")
    rl.add_argument("--proxy-dir", type=Path, default=DEFAULT_PROXY_DIR)
    rl.set_defaults(func=cmd_reload)

    sub.add_parser("render", help="render runtime config files (no start)").set_defaults(func=cmd_render)

    start = sub.add_parser("start", help="start proxy, Caddy, and ghostunnel via supervisord")
    start.add_argument("--proxy-dir", type=Path, default=DEFAULT_PROXY_DIR)
    start.add_argument("--ghostshell", type=Path, default=None)
    start.add_argument(
        "--listen",
        default=None,
        help=f"ghostunnel listen address (default: $HOST_ONLY_IP:{DEFAULT_HOST_MCP_TUNNEL_PORT})",
    )
    start.add_argument("--allow-cn", default=None, help="ghostunnel client certificate CN")
    start.set_defaults(func=cmd_start)

    sub.add_parser("stop", help="shut down supervisord and all managed processes").set_defaults(func=cmd_stop)

    ast = sub.add_parser("auth-status", help="show mcp-remote OAuth cache state")
    ast.add_argument("server", nargs="?")
    ast.set_defaults(func=cmd_auth_status)

    reset = sub.add_parser("auth-reset", help="delete OAuth cache for a server (stop stack first)")
    reset.add_argument("server")
    reset.add_argument("--yes", action="store_true")
    reset.add_argument("--force", action="store_true", help="delete even if matching MCP processes are running")
    reset.set_defaults(func=cmd_auth_reset)

    sub.add_parser("doctor", help="check host prerequisites and auth state").set_defaults(func=cmd_doctor)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "auth-status" and getattr(args, "server", None):
        servers = {s.key: s for s in iter_servers(load_config(args.config))}
        if args.server not in servers:
            raise SystemExit(f"unknown server: {args.server}")
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
