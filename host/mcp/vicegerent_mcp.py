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
from queue import Empty, Queue
from threading import Thread
from typing import Any, Iterator


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = REPO_ROOT / "host" / "mcp" / "servers.json"
DEFAULT_RUNTIME_DIR = Path.home() / ".vicegerent" / "mcp"
DEFAULT_PROXY_DIR = Path(__file__).parent.parent / "mcp-proxy-server"
DEFAULT_GHOSTSHELL = REPO_ROOT / "scripts" / "ghostunnel" / "ghostshell.sh"
DEFAULT_AUTH_DIR = Path.home() / ".mcp-auth"
DEFAULT_HOST_ONLY_IP = "192.168.64.1"
DEFAULT_HOST_MCP_TUNNEL_PORT = 8453
DEFAULT_AGENT_CLIENT_CN = "agent-client"
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
# Config & runtime state
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
        print(
            f"warning: could not parse {state_path}; ignoring runtime overrides.\n"
            "  If you deliberately disabled a server, re-run 'disable <key>' after fixing the file.",
            file=sys.stderr,
        )
        return {}


def save_state(state_path: Path, overrides: dict[str, bool]) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps({"enabled": overrides}, indent=2) + "\n", encoding="utf-8")


def iter_servers(config: dict[str, Any], state: dict[str, bool] | None = None) -> list[Server]:
    """Return servers in sorted key order, applying runtime state overrides."""
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
        # Expand ~ in env values at load time so subprocesses see real paths.
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


def make_caddyfile(config: dict[str, Any]) -> str:
    proxy = proxy_settings(config)
    host = proxy["listen_host"]
    filtered_port = int(proxy["filtered_port"])
    proxy_port = int(proxy["proxy_port"])
    return f"""\
{{
  admin off
  auto_https off
  log {{
    level WARN
  }}
}}

:{filtered_port} {{
  bind {host}

  @mcp_request {{
    method POST GET DELETE
    path /mcp
  }}

  handle @mcp_request {{
    reverse_proxy {host}:{proxy_port} {{
      flush_interval -1
    }}
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
# Runtime paths & secrets
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


def get_or_create_secret(path: Path, generator: Any = None) -> str:
    if path.exists():
        # Re-apply restrictive permissions in case created with a permissive umask.
        path.chmod(0o600)
        return path.read_text(encoding="utf-8").strip()
    value = generator() if generator else secrets.token_urlsafe(24)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value + "\n", encoding="utf-8")
    path.chmod(0o600)
    return value


# ---------------------------------------------------------------------------
# supervisord config generation
# ---------------------------------------------------------------------------


def _supervisord_env_str(env: dict[str, str]) -> str:
    """Format a dict as supervisord environment= value (KEY="val",...).

    Supervisord splits on unescaped commas; double any literal comma in values.
    Also escape % (supervisord expands %(...)s) and quotes.
    """
    parts = []
    for k, v in sorted(env.items()):
        escaped = v.replace("%", "%%").replace('"', '\\"').replace(",", ",,")
        parts.append(f'{k}="{escaped}"')
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
environment={_supervisord_env_str(proxy_env)}
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
environment={_supervisord_env_str(tunnel_env)}
autostart=true
autorestart=true
startsecs=2
stopwaitsecs=8
redirect_stderr=true
stdout_logfile={logs}/ghostunnel.log
stdout_logfile_maxbytes=5MB
stdout_logfile_backups=2
"""


# ---------------------------------------------------------------------------
# Render helpers
# ---------------------------------------------------------------------------


def render_proxy_config(config: dict[str, Any], servers: list[Server], runtime_dir: Path) -> dict[str, Path]:
    """Write mcp_server.json + Caddyfile into the runtime dir."""
    paths = runtime_paths(runtime_dir)
    paths["proxy_config_dir"].mkdir(parents=True, exist_ok=True)
    paths["caddyfile"].parent.mkdir(parents=True, exist_ok=True)
    paths["logs"].mkdir(parents=True, exist_ok=True)
    write_json(paths["proxy_config_dir"] / "mcp_server.json", make_proxy_config(servers))
    tool_config = paths["proxy_config_dir"] / "tool_config.json"
    if not tool_config.exists():
        write_json(tool_config, {"tools": {}})
    paths["caddyfile"].write_text(make_caddyfile(config), encoding="utf-8")
    return paths


def copy_proxy_config(runtime_dir: Path, proxy_dir: Path) -> None:
    src = runtime_paths(runtime_dir)["proxy_config_dir"]
    dst = proxy_dir / "config"
    dst.mkdir(parents=True, exist_ok=True)
    for file in src.iterdir():
        if file.is_file():
            shutil.copy2(file, dst / file.name)


# ---------------------------------------------------------------------------
# mcp-proxy-server patches (idempotent, applied at start)
# ---------------------------------------------------------------------------


def ensure_proxy_binds_loopback(proxy_dir: Path, host: str) -> None:
    """Patch mcp-proxy-server's HTTP listener to bind loopback only.

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
        print(
            f"warning: source listener not patched; npm run build may undo this: {source}",
            file=sys.stderr,
        )


def ensure_list_changed_notification(proxy_dir: Path) -> None:
    """Patch mcp-proxy-server to emit notifications/tools/list_changed after admin reload.

    Covers both sseTransports and streamableHttpTransports maps so agentgateway
    (StreamableHTTP) and any SSE clients both receive the notification.
    Idempotent: skips if already patched. Re-applies if a fresh npm build overwrites.

    Full chain: enable/disable -> admin reload -> list_changed ->
    agentgateway forwards -> Hermes auto-refreshes tool list (no /reload-mcp).
    """
    sentinel = "notifications/tools/list_changed"
    src = proxy_dir / "src" / "sse.ts"
    build = proxy_dir / "build" / "sse.js"

    if not src.exists():
        print(f"warning: {src} not found; skipping list_changed patch", file=sys.stderr)
        return

    text = src.read_text(encoding="utf-8")
    if sentinel in text:
        # Check the build too — a fresh npm build may have overwritten it.
        if build.exists() and sentinel not in build.read_text(encoding="utf-8"):
            print("list_changed patch present in source but missing from build; rebuilding...")
            _npm_build(proxy_dir)
        return

    marker = "await updateBackendConnections(latestServerConfig, latestToolConfig);"
    if marker not in text:
        print(f"warning: reload marker not found in {src}; skipping list_changed patch", file=sys.stderr)
        return

    notification_block = """\

            // Notify all connected MCP clients that the tool list has changed.
            // Hermes receives this via agentgateway and auto-refreshes without /reload-mcp.
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
    _npm_build(proxy_dir)


def _npm_build(proxy_dir: Path) -> None:
    print("rebuilding mcp-proxy-server...")
    result = subprocess.run(["npm", "run", "build"], cwd=str(proxy_dir), capture_output=True, text=True)
    if result.returncode != 0:
        raise SystemExit(
            f"npm run build failed — list_changed patch will not be active:\n{result.stderr}"
        )
    print("rebuild complete")


# ---------------------------------------------------------------------------
# supervisord interaction
# ---------------------------------------------------------------------------


def supervisorctl(*args: str, runtime_dir: Path) -> subprocess.CompletedProcess[str]:
    conf = runtime_paths(runtime_dir)["supervisord_conf"]
    return subprocess.run(
        ["supervisorctl", "-c", str(conf), *args],
        capture_output=True, text=True, check=False,
    )


def get_supervisor_states(runtime_dir: Path) -> dict[str, str]:
    """Return {program_name: SUPERVISOR_STATE} for all programs, or {} if not running."""
    if not runtime_paths(runtime_dir)["supervisord_sock"].exists():
        return {}
    result = supervisorctl("status", runtime_dir=runtime_dir)
    states: dict[str, str] = {}
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            states[parts[0]] = parts[1]
    return states


def is_supervisor_running(runtime_dir: Path) -> bool:
    states = get_supervisor_states(runtime_dir)
    return bool(states)


# ---------------------------------------------------------------------------
# Hot reload
# ---------------------------------------------------------------------------


def reload_proxy(runtime_dir: Path, config: dict[str, Any]) -> None:
    """Hot-reload mcp-proxy-server via session-cookie admin API.

    Flow: POST /admin/login (get cookie) -> POST /admin/server/reload.
    After reload, the patched proxy sends list_changed to all MCP sessions
    so Hermes auto-refreshes without a manual /reload-mcp.
    """
    proxy = proxy_settings(config)
    base = f"http://{proxy['listen_host']}:{proxy['proxy_port']}"
    password = get_or_create_secret(runtime_paths(runtime_dir)["admin_password"])

    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

    login_data = urllib.parse.urlencode({"username": "admin", "password": password}).encode()
    try:
        opener.open(f"{base}/admin/login", login_data, timeout=5)
    except Exception as e:
        print(f"  proxy reload: login failed ({e}) — is the stack running?")
        return

    # Confirm a session cookie was set; wrong password returns HTTP 200 with no cookie.
    if not any(c.name == "connect.sid" for c in jar):
        print("  proxy reload: login did not return a session cookie — check admin password")
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
        print(f"  proxy reload failed: {e}")


# ---------------------------------------------------------------------------
# TUI helpers
# ---------------------------------------------------------------------------


def tail_log_iter(log_file: Path, n_lines: int = 50) -> Iterator[str]:
    """Yield lines from log_file like `tail -n N -f`, non-blocking.

    Reads the last N lines immediately, then yields new lines as they arrive.
    Uses a daemon thread that polls every 100ms. Thread stops when generator
    is garbage-collected. Suitable for a TUI log pane.
    """
    queue: Queue[str] = Queue()

    def _read_last_n_lines(path: Path, n: int) -> list[str]:
        if not path.exists():
            return []
        with path.open("rb") as f:
            f.seek(0, 2)
            size = f.tell()
            if size == 0:
                return []
            block_size = 8192
            lines: list[bytes] = []
            remaining = size
            while remaining > 0 and len(lines) <= n:
                read_size = min(block_size, remaining)
                remaining -= read_size
                f.seek(remaining)
                chunk = f.read(read_size)
                lines = chunk.splitlines() + lines
            return [line.decode("utf-8", errors="replace") for line in lines[-n:]]

    def _tail_worker() -> None:
        with log_file.open("r", encoding="utf-8", errors="replace") as f:
            f.seek(0, 2)
            while True:
                data = f.read()
                if data:
                    for line in data.splitlines():
                        queue.put(line)
                time.sleep(0.1)

    for line in _read_last_n_lines(log_file, n_lines):
        yield line

    thread = Thread(target=_tail_worker, daemon=True)
    thread.start()

    while True:
        try:
            yield queue.get(timeout=0.1)
        except Empty:
            pass


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------


def write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def mcp_remote_hash(
    server_url: str,
    authorize_resource: str | None = None,
    headers: dict[str, str] | None = None,
) -> str:
    """Match mcp-remote getServerUrlHash(): md5(parts.join('|'))."""
    parts = [server_url]
    if authorize_resource:
        parts.append(authorize_resource)
    if headers:
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


def default_tunnel_listen() -> str:
    host_only_ip = os.environ.get("HOST_ONLY_IP", DEFAULT_HOST_ONLY_IP)
    return f"{host_only_ip}:{DEFAULT_HOST_MCP_TUNNEL_PORT}"


# ---------------------------------------------------------------------------
# Rich display helpers
# ---------------------------------------------------------------------------


def _require_rich() -> tuple[Any, Any]:
    try:
        from rich.console import Console
        from rich.table import Table

        return Console(), Table
    except ImportError:
        raise SystemExit("rich is required: pip install -r host/mcp/requirements-host.txt")


def _auth_label(server: Server, auth_dir: Path) -> str:
    if server.mode != "remote-oauth":
        return "n/a"
    state, _ = auth_state(server, auth_dir)
    return state


def _style_auth(label: str) -> str:
    if label == "authenticated":
        return f"[green]{label}[/green]"
    if label == "n/a":
        return f"[dim]{label}[/dim]"
    return f"[yellow]{label}[/yellow]"


def _style_proc(state: str) -> str:
    if state == "RUNNING":
        return f"[green]{state}[/green]"
    if state in ("STARTING", "BACKOFF"):
        return f"[yellow]{state}[/yellow]"
    if state in ("STOPPED", "EXITED", "FATAL", "UNKNOWN"):
        return f"[red]{state}[/red]"
    return f"[dim]{state or '—'}[/dim]"


# ---------------------------------------------------------------------------
# Action functions (TUI callable)
# ---------------------------------------------------------------------------


def list_servers(
    config_path: Path = DEFAULT_CONFIG,
    runtime_dir: Path = DEFAULT_RUNTIME_DIR,
    auth_dir: Path = DEFAULT_AUTH_DIR,
) -> int:
    """Show all configured MCP servers and their declared state (no stack required)."""
    console, Table = _require_rich()
    config = load_config(config_path)
    state = load_state(runtime_paths(runtime_dir)["state"])
    servers = iter_servers(config, state)

    table = Table(title="Host MCP Servers", show_header=True, header_style="bold magenta")
    table.add_column("Server", style="bold")
    table.add_column("Mode")
    table.add_column("Auth")
    table.add_column("Enabled")
    for server in servers:
        table.add_row(
            server.key,
            server.mode,
            _style_auth(_auth_label(server, auth_dir)),
            "[green]yes[/green]" if server.enabled else "[dim]no[/dim]",
        )
    console.print(table)
    return 0


def status(
    config_path: Path = DEFAULT_CONFIG,
    runtime_dir: Path = DEFAULT_RUNTIME_DIR,
    auth_dir: Path = DEFAULT_AUTH_DIR,
) -> int:
    """Show server auth state and infrastructure process state as rich tables."""
    console, Table = _require_rich()
    config = load_config(config_path)
    state = load_state(runtime_paths(runtime_dir)["state"])
    servers = iter_servers(config, state)
    sup_states = get_supervisor_states(runtime_dir)

    srv_table = Table(title="Host MCP Servers", show_header=True, header_style="bold magenta")
    srv_table.add_column("Server", style="bold")
    srv_table.add_column("Mode")
    srv_table.add_column("Auth")
    srv_table.add_column("Enabled")
    for server in servers:
        srv_table.add_row(
            server.key,
            server.mode,
            _style_auth(_auth_label(server, auth_dir)),
            "[green]yes[/green]" if server.enabled else "[dim]no[/dim]",
        )
    console.print(srv_table)

    inf_table = Table(title="Infrastructure", show_header=True, header_style="bold cyan")
    inf_table.add_column("Process", style="bold")
    inf_table.add_column("State")
    not_running = not sup_states
    for prog in ("proxy", "caddy", "ghostunnel"):
        inf_table.add_row(prog, _style_proc(sup_states.get(prog, "STOPPED" if not_running else "")))
    console.print(inf_table)
    return 0


def set_server_enabled(
    server_key: str,
    enabled: bool,
    config_path: Path = DEFAULT_CONFIG,
    runtime_dir: Path = DEFAULT_RUNTIME_DIR,
    proxy_dir: Path = DEFAULT_PROXY_DIR,
) -> int:
    """Enable or disable a server and hot-reload the proxy if running."""
    config = load_config(config_path)
    paths = runtime_paths(runtime_dir)
    all_keys = {s.key for s in iter_servers(config)}
    if server_key not in all_keys:
        raise SystemExit(f"unknown server: {server_key!r}. Known: {sorted(all_keys)}")

    state = load_state(paths["state"])
    state[server_key] = enabled
    save_state(paths["state"], state)

    servers = iter_servers(config, state)
    paths["proxy_config_dir"].mkdir(parents=True, exist_ok=True)
    write_json(paths["proxy_config_dir"] / "mcp_server.json", make_proxy_config(servers))
    copy_proxy_config(runtime_dir, proxy_dir)

    verb = "enabled" if enabled else "disabled"
    print(f"{verb} {server_key!r}")

    if is_supervisor_running(runtime_dir):
        reload_proxy(runtime_dir, config)
    else:
        print("  stack not running — change takes effect on next start")
    return 0


def reload_config(
    config_path: Path = DEFAULT_CONFIG,
    runtime_dir: Path = DEFAULT_RUNTIME_DIR,
    proxy_dir: Path = DEFAULT_PROXY_DIR,
) -> int:
    """Re-render proxy config from current state and hot-reload the proxy.

    Use after git pull updates servers.json to pick up new server declarations.
    """
    config = load_config(config_path)
    paths = runtime_paths(runtime_dir)
    state = load_state(paths["state"])
    servers = iter_servers(config, state)
    paths["proxy_config_dir"].mkdir(parents=True, exist_ok=True)
    write_json(paths["proxy_config_dir"] / "mcp_server.json", make_proxy_config(servers))
    copy_proxy_config(runtime_dir, proxy_dir)
    print("proxy config re-rendered")

    if is_supervisor_running(runtime_dir):
        reload_proxy(runtime_dir, config)
    else:
        print("stack not running — change takes effect on next start")
    return 0


def start_stack(
    config_path: Path = DEFAULT_CONFIG,
    runtime_dir: Path = DEFAULT_RUNTIME_DIR,
    proxy_dir: Path = DEFAULT_PROXY_DIR,
    ghostshell: Path | None = None,
    listen: str | None = None,
    allow_cn: str | None = None,
) -> int:
    """Start proxy, Caddy, and ghostunnel via supervisord."""
    config = load_config(config_path)
    paths = runtime_paths(runtime_dir)
    state = load_state(paths["state"])
    servers = iter_servers(config, state)
    active = [s for s in servers if s.enabled]

    if not active:
        print("no enabled MCP servers — not starting")
        return 0

    if not (proxy_dir / "build" / "sse.js").exists():
        raise SystemExit(f"mcp-proxy-server build not found: {proxy_dir / 'build' / 'sse.js'}")

    if is_supervisor_running(runtime_dir):
        print("supervisord is already running. Use 'reload' to update config or 'stop' first.")
        return 1

    proxy = proxy_settings(config)
    ensure_proxy_binds_loopback(proxy_dir, str(proxy["listen_host"]))
    ensure_list_changed_notification(proxy_dir)

    render_proxy_config(config, servers, runtime_dir)
    copy_proxy_config(runtime_dir, proxy_dir)

    admin_password = get_or_create_secret(paths["admin_password"])
    session_secret = get_or_create_secret(paths["session_secret"], lambda: secrets.token_hex(32))
    proxy_env = make_proxy_env(config, admin_password, session_secret)

    effective_ghostshell = ghostshell or DEFAULT_GHOSTSHELL
    effective_listen = listen or default_tunnel_listen()
    tunnel_env: dict[str, str] = {
        "TARGET": f"{proxy['listen_host']}:{int(proxy['filtered_port'])}",
        "LISTEN": effective_listen,
        "ALLOW_CN": allow_cn or DEFAULT_AGENT_CLIENT_CN,
    }

    conf_text = build_supervisord_conf(paths, proxy_dir, proxy_env, effective_ghostshell, tunnel_env)
    paths["supervisord_conf"].write_text(conf_text, encoding="utf-8")
    paths["supervisord_conf"].chmod(0o600)  # contains plaintext secrets

    # Remove stale socket so supervisord doesn't refuse to start.
    sock = paths["supervisord_sock"]
    if sock.exists():
        sock.unlink()

    try:
        subprocess.run(["supervisord", "-c", str(paths["supervisord_conf"])], check=True)
    except subprocess.CalledProcessError as exc:
        raise SystemExit(
            f"supervisord failed to start (exit {exc.returncode}); check {paths['logs']}/supervisord.log"
        ) from None

    # Wait up to 10s for all three programs to reach RUNNING.
    deadline = time.time() + 10
    while time.time() < deadline:
        sup_states = get_supervisor_states(runtime_dir)
        if all(sup_states.get(p) == "RUNNING" for p in ("proxy", "caddy", "ghostunnel")):
            break
        time.sleep(0.5)

    sup_states = get_supervisor_states(runtime_dir)
    print("enabled servers: " + ", ".join(s.key for s in active))
    failed = [p for p in ("proxy", "caddy", "ghostunnel") if sup_states.get(p) != "RUNNING"]
    for prog in ("proxy", "caddy", "ghostunnel"):
        print(f"  {prog}: {sup_states.get(prog, 'unknown')}")
    print(f"filtered MCP:  http://{proxy['listen_host']}:{int(proxy['filtered_port'])}/mcp")
    print(f"ghostunnel:    {effective_listen}")
    if failed:
        print(f"\nwarning: {failed} did not reach RUNNING; check logs under {paths['logs']}", file=sys.stderr)
        return 1
    return 0


def stop_stack(runtime_dir: Path = DEFAULT_RUNTIME_DIR) -> int:
    """Shut down supervisord and all managed processes, waiting until they exit."""
    if not is_supervisor_running(runtime_dir):
        print("supervisord is not running")
        return 0
    result = supervisorctl("shutdown", runtime_dir=runtime_dir)
    print(result.stdout.strip() or "supervisord shutdown initiated")

    # Wait up to 15s for the supervisor socket to disappear (processes fully exited)
    sock = runtime_paths(runtime_dir)["supervisord_sock"]
    deadline = time.time() + 15
    while time.time() < deadline:
        if not sock.exists():
            break
        time.sleep(0.5)
    else:
        print("warning: supervisord did not exit within 15s", file=sys.stderr)
        return 1

    return 0


def tail_log(
    process_name: str,
    runtime_dir: Path = DEFAULT_RUNTIME_DIR,
    n_lines: int = 50,
) -> int:
    """Tail logs for a supervised process (or supervisord itself). Blocking CLI version."""
    paths = runtime_paths(runtime_dir)
    log_map = {
        "proxy": paths["logs"] / "proxy.log",
        "caddy": paths["logs"] / "caddy.log",
        "ghostunnel": paths["logs"] / "ghostunnel.log",
        "supervisord": paths["logs"] / "supervisord.log",
    }
    log_file = log_map[process_name]
    if not log_file.exists():
        print(f"no log file yet for {process_name!r}: {log_file}", file=sys.stderr)
        return 1
    try:
        subprocess.run(["tail", f"-n{n_lines}", "-f", str(log_file)])
    except KeyboardInterrupt:
        pass
    return 0


def auth_status(
    server_key: str | None = None,
    config_path: Path = DEFAULT_CONFIG,
    runtime_dir: Path = DEFAULT_RUNTIME_DIR,
    auth_dir: Path = DEFAULT_AUTH_DIR,
) -> int:
    """Show mcp-remote OAuth cache state per server."""
    config = load_config(config_path)
    state = load_state(runtime_paths(runtime_dir)["state"])
    all_servers = {s.key: s for s in iter_servers(config, state)}
    selected = [all_servers[server_key]] if server_key else list(all_servers.values())
    for server in selected:
        if server.mode != "remote-oauth":
            print(f"{server.key}: {server.mode}")
            continue
        st, files = auth_state(server, auth_dir)
        print(f"{server.key}: {st}")
        if server.url:
            print(f"  url: {server.url}")
            print(f"  mcp-remote hash: {mcp_remote_hash(server.url)}")
        for path in files:
            print(f"  {path}")
    return 0


def auth_reset(
    server_key: str,
    config_path: Path = DEFAULT_CONFIG,
    runtime_dir: Path = DEFAULT_RUNTIME_DIR,
    auth_dir: Path = DEFAULT_AUTH_DIR,
    yes: bool = False,
    force: bool = False,
) -> int:
    """Delete OAuth cache for a server (stop stack first)."""
    config = load_config(config_path)
    state = load_state(runtime_paths(runtime_dir)["state"])
    all_servers = {s.key: s for s in iter_servers(config, state)}
    if server_key not in all_servers:
        raise SystemExit(f"unknown server: {server_key!r}")
    server = all_servers[server_key]
    if server.mode != "remote-oauth" or not server.url:
        raise SystemExit(f"{server.key!r} is not a remote-oauth server")

    # Guard: refuse if supervisord is running (mcp-remote may be active).
    if is_supervisor_running(runtime_dir) and not force:
        print(
            "Refusing to delete OAuth cache while the stack is running.\n"
            "Stop it first ('stop'), or pass --force.",
            file=sys.stderr,
        )
        return 2

    files = auth_files(mcp_remote_hash(server.url), auth_dir)
    if not files:
        print(f"no auth files found for {server.key!r}")
        return 0
    if not yes:
        print(f"would delete {len(files)} auth file(s) for {server.key!r}:")
        for path in files:
            print(f"  {path}")
        print("rerun with --yes to delete")
        return 1
    for path in files:
        path.unlink(missing_ok=True)
        print(f"deleted {path}")
    return 0


def doctor(
    config_path: Path = DEFAULT_CONFIG,
    runtime_dir: Path = DEFAULT_RUNTIME_DIR,
    auth_dir: Path = DEFAULT_AUTH_DIR,
) -> int:
    """Check host prerequisites and auth state."""
    config = load_config(config_path)
    proxy = proxy_settings(config)
    print("host MCP doctor")

    binaries = ["node", "npx", "caddy", "ghostunnel", "op", "supervisord", "supervisorctl"]
    for binary in binaries:
        found = shutil.which(binary)
        print(f"  {binary}: {found or 'MISSING'}")

    print(f"proxy port:    {proxy['proxy_port']}")
    print(f"filtered port: {proxy['filtered_port']}")
    print(f"auth dir:      {auth_dir}")
    print()
    return auth_status(
        server_key=None,
        config_path=config_path,
        runtime_dir=runtime_dir,
        auth_dir=auth_dir,
    )


# ---------------------------------------------------------------------------
# CLI command wrappers (thin adapters from argparse.Namespace to action fns)
# ---------------------------------------------------------------------------


def cmd_list(args: argparse.Namespace) -> int:
    return list_servers(args.config, args.runtime_dir, args.auth_dir)


def cmd_status(args: argparse.Namespace) -> int:
    return status(args.config, args.runtime_dir, args.auth_dir)


def cmd_enable(args: argparse.Namespace) -> int:
    return set_server_enabled(args.server, True, args.config, args.runtime_dir, args.proxy_dir)


def cmd_disable(args: argparse.Namespace) -> int:
    return set_server_enabled(args.server, False, args.config, args.runtime_dir, args.proxy_dir)


def cmd_reload(args: argparse.Namespace) -> int:
    return reload_config(args.config, args.runtime_dir, args.proxy_dir)


def cmd_start(args: argparse.Namespace) -> int:
    return start_stack(args.config, args.runtime_dir, args.proxy_dir, args.ghostshell, args.listen, args.allow_cn)


def cmd_stop(args: argparse.Namespace) -> int:
    return stop_stack(args.runtime_dir)


def cmd_logs(args: argparse.Namespace) -> int:
    return tail_log(args.process, args.runtime_dir, args.lines)


def cmd_auth_status(args: argparse.Namespace) -> int:
    return auth_status(getattr(args, "server", None), args.config, args.runtime_dir, args.auth_dir)


def cmd_auth_reset(args: argparse.Namespace) -> int:
    return auth_reset(args.server, args.config, args.runtime_dir, args.auth_dir, args.yes, args.force)


def cmd_doctor(args: argparse.Namespace) -> int:
    return doctor(args.config, args.runtime_dir, args.auth_dir)


def cmd_tui(args: argparse.Namespace) -> int:
    """Launch the interactive TUI."""
    import importlib.util

    tui_path = Path(__file__).parent / "tui.py"
    spec = importlib.util.spec_from_file_location("tui", tui_path)
    tui_mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(tui_mod)  # type: ignore[union-attr]
    HostMCPApp = tui_mod.HostMCPApp

    app = HostMCPApp(
        config_path=args.config,
        runtime_dir=args.runtime_dir,
        proxy_dir=args.proxy_dir,
        auth_dir=args.auth_dir,
    )
    app.run()
    return 0


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


_HELP = """\
vicegerent-mcp — host-side MCP control plane

Manages N stdio/OAuth MCP servers via mcp-proxy-server + supervisord,
with hot-reload on enable/disable and a rich TUI dashboard.

Commands:
  list                   show all configured MCP servers and their state
  status                 show server auth state + infrastructure process state
  enable KEY             enable a server and hot-reload the proxy
  disable KEY            disable a server and hot-reload the proxy
  reload                 re-render proxy config from current state and hot-reload
  start                  start proxy, Caddy, and ghostunnel via supervisord
  stop                   shut down all managed processes
  logs PROC              tail logs  (proxy | caddy | ghostunnel | supervisord)
  auth-status [KEY]      show mcp-remote OAuth cache state (all servers or one)
  auth-reset KEY         delete OAuth cache for a server (stop stack first)
  doctor                 check host prerequisites and auth state
  tui                    launch interactive TUI dashboard

Global options:
  --config PATH          server config file
                         (default: host/mcp/servers.json in repo)
  --auth-dir PATH        mcp-remote OAuth cache directory
                         (default: ~/.mcp-auth)
  --runtime-dir PATH     supervisord/runtime state directory
                         (default: ~/.vicegerent/mcp)

Run './vicegerent-mcp COMMAND --help' for per-command options.
"""


class _SuppressSubparsers(argparse.RawDescriptionHelpFormatter):
    """Formatter that hides the auto-generated subcommand list.

    We hand-write the command table in _HELP; the argparse duplicate is noise.
    """

    def _format_action(self, action: argparse.Action) -> str:
        if action.nargs == argparse.PARSER:
            return ""
        return super()._format_action(action)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=_HELP,
        formatter_class=_SuppressSubparsers,
        add_help=True,
    )
    # Global args available to all subcommands.
    parser.add_argument(
        "--config", type=Path, default=DEFAULT_CONFIG, metavar="PATH",
        help="server config file (default: host/mcp/servers.json in repo)",
    )
    parser.add_argument(
        "--auth-dir", type=Path, default=DEFAULT_AUTH_DIR, metavar="PATH",
        help="mcp-remote OAuth cache directory (default: ~/.mcp-auth)",
    )
    parser.add_argument(
        "--runtime-dir", type=Path, default=DEFAULT_RUNTIME_DIR, metavar="PATH",
        help="supervisord/runtime state directory (default: ~/.vicegerent/mcp)",
    )
    sub = parser.add_subparsers(dest="command", required=False)

    # list — no stack required
    sub.add_parser("list", help="show all configured MCP servers and their state").set_defaults(func=cmd_list)

    # status — rich table with process state
    sub.add_parser("status", help="show server auth state and infrastructure process state").set_defaults(func=cmd_status)

    # enable / disable
    for verb, fn, help_str in [
        ("enable", cmd_enable, "enable a server and hot-reload the proxy"),
        ("disable", cmd_disable, "disable a server and hot-reload the proxy"),
    ]:
        p = sub.add_parser(verb, help=help_str)
        p.add_argument("server", metavar="KEY")
        p.add_argument("--proxy-dir", type=Path, default=DEFAULT_PROXY_DIR)
        p.set_defaults(func=fn)

    # reload
    rl = sub.add_parser("reload", help="re-render proxy config from current state and hot-reload")
    rl.add_argument("--proxy-dir", type=Path, default=DEFAULT_PROXY_DIR)
    rl.set_defaults(func=cmd_reload)

    # start
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

    # stop
    sub.add_parser("stop", help="shut down supervisord and all managed processes").set_defaults(func=cmd_stop)

    # logs
    logs = sub.add_parser("logs", help="tail logs for a supervised process (Ctrl-C to exit)")
    logs.add_argument(
        "process",
        choices=["proxy", "caddy", "ghostunnel", "supervisord"],
        help="which process log to tail",
    )
    logs.add_argument("-n", "--lines", type=int, default=50, metavar="N", help="initial lines to show (default: 50)")
    logs.set_defaults(func=cmd_logs)

    # auth-status
    ast = sub.add_parser("auth-status", help="show mcp-remote OAuth cache state per server")
    ast.add_argument("server", nargs="?", metavar="KEY")
    ast.set_defaults(func=cmd_auth_status)

    # auth-reset
    reset = sub.add_parser("auth-reset", help="delete OAuth cache for a server (stop stack first)")
    reset.add_argument("server", metavar="KEY")
    reset.add_argument("--yes", action="store_true", help="confirm deletion")
    reset.add_argument("--force", action="store_true", help="delete even if the stack is running")
    reset.set_defaults(func=cmd_auth_reset)

    # doctor
    sub.add_parser("doctor", help="check host prerequisites and auth state").set_defaults(func=cmd_doctor)

    # tui
    tui_p = sub.add_parser("tui", help="launch interactive TUI dashboard")
    tui_p.add_argument("--proxy-dir", type=Path, default=DEFAULT_PROXY_DIR)
    tui_p.set_defaults(func=cmd_tui)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 0
    if args.command == "auth-status" and getattr(args, "server", None):
        servers = {s.key: s for s in iter_servers(load_config(args.config))}
        if args.server not in servers:
            raise SystemExit(f"unknown server: {args.server!r}")
    return args.func(args)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__ = [
    # Data reading (pure)
    "load_config",
    "load_state",
    "iter_servers",
    "proxy_settings",
    "get_supervisor_states",
    "is_supervisor_running",
    "auth_state",
    # Actions (TUI callable)
    "list_servers",
    "status",
    "set_server_enabled",
    "reload_config",
    "start_stack",
    "stop_stack",
    "auth_status",
    "auth_reset",
    "doctor",
    # TUI helpers
    "tail_log_iter",
    "runtime_paths",
    "_auth_label",
    "_style_auth",
    "_style_proc",
    # Constants
    "DEFAULT_CONFIG",
    "DEFAULT_RUNTIME_DIR",
    "DEFAULT_PROXY_DIR",
    "DEFAULT_AUTH_DIR",
    "DEFAULT_GHOSTSHELL",
    "DEFAULT_HOST_ONLY_IP",
    "DEFAULT_HOST_MCP_TUNNEL_PORT",
]


if __name__ == "__main__":
    raise SystemExit(main())
