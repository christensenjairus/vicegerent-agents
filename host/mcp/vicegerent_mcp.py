#!/usr/bin/env python3
"""Host-side stack controller for vicegerent.

Owns the full local ToolHive stack that backs the cluster's MCP access:

  ToolHive workloads   12 MCP backends (kubernetes, gitlab, github, tavily,
                       firecrawl, notion, linear, jira, grafana, alertmanager,
                       pagerduty, elastic) run by `thv run` into
                       the group `vicegerent`.
                       Managed by ToolHive's own daemon (Docker containers),
                       NOT by supervisord — they persist across stack restarts
                       so OAuth tokens are not re-prompted.
  vMCP                 `thv vmcp serve` aggregates the group behind one
                       loopback endpoint on 127.0.0.1:4483, prefixing every
                       backend's tools with `{workload}_`.
  ghostunnel           terminates mTLS from the cluster and forwards to vMCP.
  rclone-s3            `rclone serve s3` on 127.0.0.1:9899 backing the cluster's
                       Velero BackupStorageLocation from <repo>/velero-backups;
                       reached from pods via host.docker.internal.
  caffeinate           opt-in: holds a macOS "stay awake" assertion while the
                       stack is up (enable per-start with --caffeinate, or --always).

vMCP, ghostunnel, and rclone-s3 (plus caffeinate when enabled) run under supervisord
with autorestart. The workloads are brought up by `start` (idempotent) before it starts.

Tool authorization lives in the cluster (agentgateway allowlist + Cerbos); the
vMCP config here exposes ALL backend tools and adds no filter/authz.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import getpass
import hashlib
import json
import os
import base64
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Generator, Iterator


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_RUNTIME_DIR = Path.home() / ".vicegerent" / "mcp"
DEFAULT_GHOSTSHELL = REPO_ROOT / "scripts" / "ghostunnel" / "ghostshell.sh"
DEFAULT_SERVERS_CONFIG = Path(__file__).resolve().parent / "toolhive-servers.json"

DEFAULT_GROUP = "vicegerent"
DEFAULT_VMCP_HOST = "127.0.0.1"
DEFAULT_VMCP_PORT = 4483
# Loopback only — Kind reaches it via host.docker.internal (Docker Desktop proxies
# to the host's localhost). Binding 0.0.0.0 would expose the tunnel to the LAN.
DEFAULT_LISTEN = "127.0.0.1:8453"
DEFAULT_AGENT_CLIENT_CN = "agent-client"

# rclone serve s3 backend for Velero backups (loopback only; port clear of vmcp/ghostunnel/dashboard).
DEFAULT_RCLONESHELL = REPO_ROOT / "scripts" / "rclone" / "rclone-s3.sh"
DEFAULT_RCLONE_ADDR = "127.0.0.1:9899"
DEFAULT_RCLONE_S3_DIR = Path.home() / ".vicegerent" / "rclone-s3"
DEFAULT_RCLONE_SERVE_DIR = REPO_ROOT / "velero-backups"
RCLONE_BUCKET = "vicegerent"
# Mirrors the host auth-key; recovers it on a fresh laptop (see ensure_rclone_material).
VELERO_SECRET_NS = "velero"  # pragma: allowlist secret
VELERO_SECRET = "velero-credentials"  # pragma: allowlist secret

# Host ghostunnel mTLS material. Source of truth is the laptop; a copy of the
# server cert/key + CA cert is mirrored to a kind Secret by setup-secrets-platform.sh
# so a host that's missing them can recover before ghostunnel starts.
DEFAULT_GHOSTUNNEL_DIR = Path.home() / ".vicegerent" / "ghostunnel"
GHOSTUNNEL_KUBE_CONTEXT = os.environ.get("KUBE_CONTEXT", "kind-vicegerent")
GHOSTUNNEL_SECRET_NS = "agentgateway-system"  # pragma: allowlist secret
GHOSTUNNEL_SECRET = "ghostunnel-server"  # pragma: allowlist secret
# host filename -> kind Secret data key
GHOSTUNNEL_FILES = {"server.crt": "server.crt", "server.key": "server.key", "ca.cert": "ca.crt"}

THV = os.environ.get("THV", "thv")

# Kubeconfig mount path inside the containerized kubernetes MCP server.
KUBECONFIG_CONTAINER_PATH = "/kubeconfig/config"

# AWS: botocore derives the SSO token-cache path (~/.aws/sso/cache) from HOME
# with no env override, so any container that needs the operator's ~/.aws
# (the aws-api-mcp-server backend, and kubernetes-mcp-server for exec-plugin
# auth against a real EKS cluster) gets it mounted at /app/.aws with HOME
# pinned to /app (see apply:aws_config).
AWS_HOME_CONTAINER_PATH = "/app"
AWS_DIR_CONTAINER_PATH = "/app/.aws"

# Core supervised programs (always run): vMCP, ghostunnel, rclone-s3.
SUPERVISED_PROGRAMS = ("vmcp", "ghostunnel", "rclone-s3")
# caffeinate (macOS stay-awake) is opt-in per `start`; shown in status/logs regardless.
ALL_PROGRAMS = ("caffeinate", *SUPERVISED_PROGRAMS)


# ---------------------------------------------------------------------------
# Runtime paths + config
# ---------------------------------------------------------------------------


def runtime_paths(runtime_dir: Path) -> dict[str, Path]:
    return {
        "runtime": runtime_dir,
        "logs": runtime_dir / "logs",
        "supervisord_conf": runtime_dir / "supervisord.conf",
        "supervisord_sock": runtime_dir / "supervisor.sock",
        "supervisord_pid": runtime_dir / "supervisord.pid",
        "vmcp_config": runtime_dir / "vmcp-config.json",
        "vmcp_init": runtime_dir / "vmcp-init.yaml",
        "servers_state": runtime_dir / "servers-state.json",
    }


def load_servers_config(path: Path = DEFAULT_SERVERS_CONFIG) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SystemExit(f"servers config not found: {path}")
    except json.JSONDecodeError as exc:
        raise SystemExit(f"invalid servers config {path}: {exc}")


def group_name(config: dict[str, Any]) -> str:
    return os.environ.get("THV_GROUP") or config.get("group") or DEFAULT_GROUP


def vmcp_port(config: dict[str, Any]) -> int:
    return int(os.environ.get("VMCP_PORT") or config.get("vmcp_port") or DEFAULT_VMCP_PORT)


def load_server_state(runtime_dir: Path) -> dict[str, bool]:
    """Runtime enable/disable overrides written by `configure`.

    A server absent from this map falls back to its config default. This keeps
    the tracked toolhive-servers.json declarative (all off by default) while the
    user's opt-in choices live in disposable runtime state.
    """
    return {k: bool(v) for k, v in (_read_state(runtime_dir).get("enabled") or {}).items()}


def _read_state(runtime_dir: Path) -> dict[str, Any]:
    path = runtime_paths(runtime_dir)["servers_state"]
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _write_state(runtime_dir: Path, data: dict[str, Any]) -> None:
    path = runtime_paths(runtime_dir)["servers_state"]
    path.parent.mkdir(parents=True, exist_ok=True)
    # Write-then-rename so a reader (or a crash mid-write) never sees a torn file.
    tmp = path.with_suffix(f"{path.suffix}.tmp{os.getpid()}")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


@contextlib.contextmanager
def _locked_state(runtime_dir: Path) -> Generator[dict[str, Any], None, None]:
    """Read-modify-write servers-state.json as one atomic, cross-process critical
    section: yields the current state for in-place mutation, writes it back on
    clean exit.

    `run_workloads` fires one thread per enabled server, each of which saves its
    own fingerprint into this same file, and `enable`/`disable`/`configure` write
    to it too. Locking only the write wouldn't help -- a writer's *read* can still
    be stale from before it acquired the lock, so it'd write back a snapshot that's
    missing whatever another writer committed in between, silently reverting it.
    Locking the read+mutate+write together means each writer's read is always of
    the latest committed state.
    """
    path = runtime_paths(runtime_dir)["servers_state"]
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(f"{path.name}.lock")
    with open(lock_path, "w") as lockfile:
        fcntl.flock(lockfile.fileno(), fcntl.LOCK_EX)
        try:
            data = _read_state(runtime_dir)
            yield data
            _write_state(runtime_dir, data)
        finally:
            fcntl.flock(lockfile.fileno(), fcntl.LOCK_UN)


def save_server_state(runtime_dir: Path, enabled: dict[str, bool]) -> None:
    with _locked_state(runtime_dir) as data:
        data["enabled"] = enabled


def caffeinate_always(runtime_dir: Path) -> bool:
    """Persisted preference: keep macOS awake whenever the stack starts."""
    return bool(_read_state(runtime_dir).get("always_caffeinate", False))


def set_caffeinate_always(runtime_dir: Path, value: bool) -> None:
    with _locked_state(runtime_dir) as data:
        data["always_caffeinate"] = bool(value)


def load_server_params(runtime_dir: Path) -> dict[str, dict[str, str]]:
    """Per-server non-secret parameter values set by `configure` (e.g. GitLab URL,
    kubeconfig path). Shape: {server_name: {param_name: value}}."""
    raw = _read_state(runtime_dir).get("params") or {}
    return {k: {pk: str(pv) for pk, pv in v.items()} for k, v in raw.items() if isinstance(v, dict)}


def save_server_params(runtime_dir: Path, params: dict[str, dict[str, str]]) -> None:
    with _locked_state(runtime_dir) as data:
        data["params"] = params


def server_param(runtime_dir: Path, server_name: str, param_name: str, default: str = "") -> str:
    return load_server_params(runtime_dir).get(server_name, {}).get(param_name, default)


def param_secret_name(server_name: str, param_name: str) -> str:
    """`thv` secret name for a param marked `"secret": true` (e.g. gitlab_api_url).

    Params normally live in servers-state.json, disposable runtime state. A param
    that's a pain to re-enter (a URL you'd otherwise have to look up again) can opt
    into living in the `thv` secrets provider instead -- the same durable store
    already used for API keys, so it survives a wiped/corrupted runtime dir.
    """
    return f"{server_name}_{param_name}"


def read_secret_value(secret_name: str) -> str:
    """Fetch a secret's plaintext value via `thv secret get`; "" if unset/unavailable."""
    result = thv("secret", "get", secret_name)
    return result.stdout.strip() if result.returncode == 0 else ""


def _param_owner(server: dict[str, Any]) -> str:
    """The server whose configured param VALUES and enabled-state this server
    uses. Normally itself; for a hidden companion (`companion_of`) it's the
    parent, so the companion inherits the parent's config (e.g. aws-profiles
    reuses aws's aws_config_dir) and is never configured on its own."""
    return server.get("companion_of") or server["name"]


def is_server_enabled(server: dict[str, Any], state: dict[str, bool]) -> bool:
    """Effective enabled state: a runtime override wins over the config default.

    A companion (`companion_of`) mirrors its parent's state via `_param_owner`,
    so enabling/disabling the parent flips both as one unit — the companion is
    never enabled independently. Keep a companion's own `enabled` default equal
    to its parent's so the pre-configure default matches too.
    """
    owner = _param_owner(server)
    if owner in state:
        return state[owner]
    return bool(server.get("enabled", False))


def enabled_servers(
    config: dict[str, Any], runtime_dir: Path = DEFAULT_RUNTIME_DIR
) -> list[dict[str, Any]]:
    state = load_server_state(runtime_dir)
    return [s for s in config.get("servers", []) if is_server_enabled(s, state)]


def vmcp_target(config: dict[str, Any]) -> str:
    host = os.environ.get("VMCP_HOST", DEFAULT_VMCP_HOST)
    return f"{host}:{vmcp_port(config)}"


def default_listen() -> str:
    return os.environ.get("LISTEN", DEFAULT_LISTEN)


def _thv_path() -> str:
    return shutil.which(THV) or THV


def _addr_reachable(addr: str, timeout: float = 0.3) -> bool:
    """True if something is already accepting connections on host:port.

    Used to detect a vmcp/ghostunnel/rclone-s3 instance left running outside
    supervisord's control (e.g. orphaned by a killed supervisord) so `start`
    can leave it in place instead of racing it for the port.
    """
    host, _, port_s = addr.rpartition(":")
    try:
        port = int(port_s)
    except ValueError:
        return False
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        return s.connect_ex((host, port)) == 0


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _terminate_pids(pids: list[int], timeout: float = 5.0) -> None:
    """SIGTERM a set of pids, then SIGKILL whichever are still alive after timeout."""
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.time() + timeout
    while time.time() < deadline and any(_pid_alive(p) for p in pids):
        time.sleep(0.2)
    for pid in pids:
        if _pid_alive(pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def _kill_addr_listeners(addr: str, timeout: float = 5.0) -> list[int]:
    """Kill whatever is listening on host:port (SIGTERM, then SIGKILL); returns
    the pids killed.

    `stop` uses this to clear vmcp/ghostunnel/rclone-s3 processes supervisord
    wasn't tracking (orphaned by e.g. a killed supervisord) so the next `start`
    always gets a fresh, fully supervisord-managed instance rather than one it
    has to leave alone because the port's already taken.
    """
    _, _, port_s = addr.rpartition(":")
    result = subprocess.run(
        ["lsof", "-t", "-nP", f"-iTCP:{port_s}", "-sTCP:LISTEN"],
        capture_output=True, text=True,
    )
    pids = sorted({int(p) for p in result.stdout.split() if p.strip()})
    if pids:
        _terminate_pids(pids, timeout)
    return pids


def _kill_stray_supervisord(paths: dict[str, Path], timeout: float = 10.0) -> list[int]:
    """Kill any supervisord process still pointing at our conf file.

    `supervisorctl shutdown` only ever reaches whichever supervisord currently
    owns the socket *path* -- but `start` unconditionally unlinks and recreates
    that path, so a supervisord that a prior `stop` failed to reach becomes
    permanently unreachable that way while it keeps running, autorestart=true,
    forever resurrecting vmcp/ghostunnel/rclone-s3 the instant something else
    kills them. Find strays by command line instead of by socket.
    """
    conf = str(paths["supervisord_conf"])
    result = subprocess.run(
        ["pgrep", "-f", f"supervisord -c {conf}"],
        capture_output=True, text=True,
    )
    pids = sorted({int(p) for p in result.stdout.split() if p.strip()})
    if pids:
        _terminate_pids(pids, timeout)
    return pids


# ---------------------------------------------------------------------------
# ToolHive workloads
# ---------------------------------------------------------------------------


def thv(*args: str, check: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [_thv_path(), *args], capture_output=True, text=True, check=check,
    )


def list_workloads(group: str) -> dict[str, str]:
    """Return {workload_name: status} for all workloads in the group."""
    result = thv("list", "--all", "--group", group, "--format", "json")
    if result.returncode != 0 or not result.stdout.strip():
        return {}
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return {}
    return {w["name"]: w.get("status", "unknown") for w in data if "name" in w}


def list_all_workload_names() -> set[str]:
    """All ToolHive workload names across every group (names are globally unique,
    so `thv run <name>` collides even with a workload in another group)."""
    result = thv("list", "--all", "--format", "json")
    if result.returncode != 0 or not result.stdout.strip():
        return set()
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return set()
    return {w["name"] for w in data if "name" in w}


def _resolve_param_value(server: dict[str, Any], param_name: str, runtime_dir: Path) -> str:
    """Resolve one configured param's current value (secret or runtime state),
    same lookup `build_thv_run_argv` does for `apply: server_arg`/`kubeconfig`.
    """
    owner = _param_owner(server)
    for param in server.get("params", []):
        if param["name"] != param_name:
            continue
        if param.get("secret"):
            return read_secret_value(param_secret_name(owner, param_name))
        return server_param(runtime_dir, owner, param_name, param.get("default", ""))
    return ""


def build_permission_profile(server: dict[str, Any], runtime_dir: Path) -> dict[str, Any] | None:
    """Build a ToolHive network permission-profile dict for one server, or None
    if the server is declared `network.exempt` (network-mode carve-out, e.g.
    kubernetes' raw docker-network access — out of scope for permission-profile
    allowlisting entirely).

    Isolation is ToolHive's default since v0.30.1 (no `--isolate-network` flag
    needed); passing `--permission-profile <path>` with a `network.outbound`
    block scopes what that isolation actually allows out. Static hosts come
    straight from config. A dynamic hostname is resolved at run time from the
    already-`configure`d value and never hardcoded here, since it's per-operator:
    `host_from_param` reads it out of a `params[]` entry (gitlab's api_url,
    alertmanager('_gov')'s url); `host_from_secret` reads it out of a top-level
    `secrets[]` entry instead (jira_url, grafana('_gov')_url) via `thv secret get`
    directly, since those aren't in `params` at all. Either way, raise a clear
    error if the value isn't set yet — same pattern as the existing kubeconfig
    "run `vicegerent mcp configure`" error.
    """
    name = server["name"]
    net = server.get("network")
    if net is None:
        raise SystemExit(
            f"{name}: no 'network' config in toolhive-servers.json — every server "
            "must declare network.allow_hosts/host_from_param/host_from_secret, "
            "network.none=true, or network.exempt=true (see host/mcp/README.md)"
        )
    if net.get("exempt"):
        return None

    if net.get("none"):
        # Server makes no outbound calls at all (e.g. aws-profiles reads the
        # mounted ~/.aws/config and serves stdio through ToolHive's bridge).
        # Lock egress to deny-all rather than inherit ToolHive's default.
        return {
            "network": {
                "outbound": {"insecure_allow_all": False, "allow_host": [], "allow_port": []}
            }
        }

    allow_hosts = list(net.get("allow_hosts", []))
    host_from_param = net.get("host_from_param")
    if host_from_param:
        value = _resolve_param_value(server, host_from_param, runtime_dir)
        if not value:
            raise SystemExit(
                f"{name}: network.host_from_param {host_from_param!r} has no value yet "
                "— run `vicegerent mcp configure` to set it"
            )
        host = urllib.parse.urlparse(value).hostname
        if not host:
            raise SystemExit(
                f"{name}: could not parse a hostname out of configured "
                f"{host_from_param}={value!r}"
            )
        allow_hosts.append(host)

    host_from_secret = net.get("host_from_secret")
    if host_from_secret:
        value = read_secret_value(host_from_secret)
        if not value:
            raise SystemExit(
                f"{name}: network.host_from_secret {host_from_secret!r} has no value yet "
                "— run `vicegerent mcp configure` (or `thv secret set` it) first"
            )
        host = urllib.parse.urlparse(value).hostname
        if not host:
            raise SystemExit(
                f"{name}: could not parse a hostname out of configured "
                f"{host_from_secret}={value!r}"
            )
        allow_hosts.append(host)

    if not allow_hosts:
        raise SystemExit(
            f"{name}: network config resolved to an empty allow_hosts list — "
            "refusing to run with a permission profile that blocks all egress "
            "unless that's actually intended (set network.none=true for a "
            "deliberate no-egress server)"
        )

    return {
        "network": {
            "outbound": {
                "insecure_allow_all": False,
                "allow_host": allow_hosts,
                "allow_port": list(net.get("allow_ports", [443])),
            }
        }
    }


def write_permission_profile(
    server: dict[str, Any], runtime_dir: Path
) -> Path | None:
    """Build and persist the permission-profile JSON for one server; return its
    path, or None if the server is `network.exempt` (no profile — it opts out of
    permission-profile allowlisting entirely, e.g. kubernetes' docker-network mode).
    """
    profile = build_permission_profile(server, runtime_dir)
    if profile is None:
        return None
    path = runtime_dir / f"permission-profile-{server['name']}.json"
    path.write_text(json.dumps(profile, indent=2) + "\n", encoding="utf-8")
    return path


def build_thv_run_argv(
    server: dict[str, Any],
    group: str,
    runtime_dir: Path,
) -> list[str]:
    """Assemble the `thv run` argv for one server from the config entry.

    The workload name is pinned with --name so it becomes the exact vMCP tool
    prefix the Cerbos policy expects.
    """
    name = server["name"]
    stype = server["type"]
    if stype == "npx":
        positional = f"npx://{server['package']}"
    elif stype in ("remote", "registry"):
        # A registry name (notion/linear) is a static positional; a bare remote
        # URL that's per-operator (elastic's Kibana host) comes from a configured
        # param instead — ToolHive accepts either as the `thv run` positional.
        positional = server.get("registry")
        if not positional and server.get("registry_from_param"):
            positional = _resolve_param_value(
                server, server["registry_from_param"], runtime_dir
            )
        if not positional:
            raise SystemExit(
                f"server {name!r}: remote type needs 'registry' or a resolvable "
                "'registry_from_param' — run `vicegerent mcp configure` to set it"
            )
    else:
        raise SystemExit(f"server {name!r}: unknown type {stype!r}")

    argv = [_thv_path(), "run", positional, "--name", name, "--group", group]

    # npx-wrapped MCP servers speak stdio. ToolHive otherwise defaults them to
    # streamable-http (injects MCP_PORT/MCP_TRANSPORT and runs the container with
    # stdin CLOSED); the server ignores those, starts on stdio, hits EOF, exits 0,
    # and Docker crashloops it. Tell ToolHive the transport so it attaches stdin
    # and bridges stdio -> streamable-http. Overridable via a "transport" config field.
    transport = server.get("transport", "stdio" if stype == "npx" else "")
    if transport:
        argv += ["--transport", transport]

    argv += list(server.get("run_flags", []))

    # Network egress lockdown: isolation is ToolHive's default since
    # v0.30.1, so an unrestricted workload here would be one that already
    # opted out via run_flags (kubernetes: --isolate-network=false, needs raw
    # docker-network TCP to the kind API server and can't go through the
    # egress proxy — see README "Kubernetes networking"). Every other server
    # gets an explicit --permission-profile: a narrow static/dynamic allowlist,
    # or (network.none) a deny-all-egress profile.
    profile_path = write_permission_profile(server, runtime_dir)
    if profile_path is not None:
        argv += ["--permission-profile", str(profile_path)]

    # server_args from config are non-negotiable (e.g. kubernetes' --read-only);
    # configured params only ADD to them.
    server_args = list(server.get("server_args", []))

    # Apply configured params (values from `configure`, stored in runtime state --
    # or, for a param marked "secret": true, in the `thv` secrets provider instead).
    param_owner = _param_owner(server)  # companion inherits the parent's values
    for param in server.get("params", []):
        pname = param["name"]
        if param.get("secret"):
            value = read_secret_value(param_secret_name(param_owner, pname))
        else:
            value = server_param(runtime_dir, param_owner, pname, param.get("default", ""))
        apply = param.get("apply")
        if apply == "server_arg":
            if value:
                server_args.append(param["template"].replace("{value}", value))
        elif apply == "kubeconfig":
            # A user-supplied kubeconfig path wins; otherwise fall back to the
            # kind cluster's --internal kubeconfig (containerized npx can't reach a
            # host-loopback API, so it needs the in-docker-network address).
            if value:
                kubeconfig = Path(value).expanduser()
                if not kubeconfig.is_file():
                    raise SystemExit(f"{name}: kubeconfig not found: {kubeconfig}")
            elif server.get("kind_cluster"):
                kubeconfig = write_internal_kubeconfig(server["kind_cluster"], runtime_dir)
            else:
                raise SystemExit(f"{name}: no kubeconfig set — run `vicegerent mcp configure`")
            argv += ["-v", f"{kubeconfig}:{KUBECONFIG_CONTAINER_PATH}:ro"]
            argv += ["-e", f"KUBECONFIG={KUBECONFIG_CONTAINER_PATH}"]
            server_args += ["--kubeconfig", KUBECONFIG_CONTAINER_PATH]
        elif apply == "aws_config":
            # Mount the host ~/.aws (a user-supplied path wins; else ~/.aws)
            # read-only into the container and pin HOME so profiles + the SSO
            # token cache resolve. Read-only is deliberate: the agent must never
            # mutate the operator's AWS config/creds, and SSO token refresh is a
            # host-side concern (`aws sso login`), mirroring how every other
            # backend's credentials are maintained outside the sandbox.
            aws_dir = Path(value).expanduser() if value else Path.home() / ".aws"
            if not aws_dir.is_dir():
                raise SystemExit(
                    f"{name}: AWS config dir not found: {aws_dir} — run `aws configure` "
                    "/ `aws sso login` first, or set the path via `vicegerent mcp configure`"
                )
            argv += ["-v", f"{aws_dir}:{AWS_DIR_CONTAINER_PATH}:ro"]
            # aws-api-mcp-server writes its own state/log under $HOME/.aws/aws-api-mcp,
            # which the read-only creds mount above would block. Overlay a writable
            # dir at exactly that subpath (in the runtime dir) so the server's state
            # is writable while the operator's real creds stay read-only.
            aws_workdir = runtime_dir / "aws-workdir"
            aws_workdir.mkdir(parents=True, exist_ok=True)
            argv += ["-v", f"{aws_workdir}:{AWS_DIR_CONTAINER_PATH}/aws-api-mcp"]
            # The AWS CLI needs to write refreshed STS credentials to ~/.aws/cli/cache
            # for profiles that assume a role -- without a writable overlay there, a
            # cache miss/expiry hard-fails with "Read-only file system" (a cache hit
            # succeeds silently, which is why this is easy to miss testing by hand).
            # Shared across every apply:aws_config consumer, same as aws-workdir above.
            aws_cli_cache = runtime_dir / "aws-cli-cache"
            aws_cli_cache.mkdir(parents=True, exist_ok=True)
            argv += ["-v", f"{aws_cli_cache}:{AWS_DIR_CONTAINER_PATH}/cli/cache"]
            argv += ["-e", f"HOME={AWS_HOME_CONTAINER_PATH}"]
        elif apply == "remote_header":
            # Inject a static auth header into a `remote` server by NAME reference
            # so the credential stays in the `thv` secret store — never argv, never
            # read into this process. ToolHive resolves it and does NO OAuth flow
            # (that only runs under --remote-auth, which we never pass), so header
            # auth works directly against endpoints whose OAuth-discovery paths
            # would otherwise redirect an SDK client into an HTML login shell.
            argv += [
                "--remote-forward-headers-secret",
                f"{param['header']}={param_secret_name(name, pname)}",
            ]
        elif apply is None:
            # Value is consumed elsewhere (registry_from_param / network
            # host_from_param), not injected as an arg here.
            pass
        else:
            raise SystemExit(f"{name}: param {pname!r} has unknown apply {apply!r}")

    for key, val in server.get("env", {}).items():
        argv += ["-e", f"{key}={val}"]
    for sec in server.get("secrets", []):
        argv += ["--secret", f"{sec['name']},target={sec['target']}"]

    # Some CLIs (mcp-remote) treat their first bare arg as positional (the URL)
    # rather than doing real flag-aware parsing, so a flag placed ahead of it
    # gets swallowed as the URL itself. server_args_after lets config put
    # trailing flags (e.g. --transport http-only) AFTER the params loop above.
    server_args += list(server.get("server_args_after", []))

    if server_args:
        argv += ["--", *server_args]
    return argv


def write_internal_kubeconfig(cluster: str, runtime_dir: Path) -> Path:
    """Write kind's --internal kubeconfig for the cluster and return its path.

    Uses the in-docker-network API address (https://<cluster>-control-plane:6443)
    so the containerized MCP server can reach it over the kind docker network.
    """
    dest = runtime_dir / f"kubeconfig-{cluster}.yaml"
    result = subprocess.run(
        ["kind", "get", "kubeconfig", "--name", cluster, "--internal"],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        raise SystemExit(
            f"failed to get internal kubeconfig for kind cluster {cluster!r}: "
            f"{result.stderr.strip()}"
        )
    dest.write_text(result.stdout, encoding="utf-8")
    dest.chmod(0o644)  # readable by the container's (possibly non-root) user
    return dest


def _ca_data(text: str) -> str:
    m = re.search(r"certificate-authority-data:\s*(\S+)", text)
    return m.group(1) if m else ""


def kind_kubeconfig_stale(server: dict[str, Any], runtime_dir: Path) -> bool:
    """A kind_cluster workload mounts an internal kubeconfig captured at `thv run`
    time. If the cluster is recreated its CA rotates, leaving the mount stale — the
    MCP server then fails API calls with 'certificate signed by unknown authority'.
    Detect this by comparing the mounted CA to the current one so start can recreate.
    """
    cluster = server.get("kind_cluster")
    if not cluster:
        return False
    dest = runtime_dir / f"kubeconfig-{cluster}.yaml"
    if not dest.is_file():
        return True
    result = subprocess.run(
        ["kind", "get", "kubeconfig", "--name", cluster, "--internal"],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        return False  # can't tell (cluster down?) — don't force a needless recreate
    return _ca_data(result.stdout) != _ca_data(dest.read_text(encoding="utf-8"))


def _path_content_digest(path: Path) -> str:
    """sha256 over a file's bytes, or a directory's sorted (relpath, bytes)
    manifest; "" if absent/unreadable. Folds the CONTENT of a server's mounted
    host config into its spec fingerprint so an on-disk change forces a recreate
    on the next `start`. A live bind mount alone isn't enough — some MCP servers
    read/cache their config once at startup, so a fresh `aws sso login` token, a
    new profile in ~/.aws/config, or an edited kubeconfig wouldn't take effect
    until the container is recreated."""
    h = hashlib.sha256()
    try:
        if path.is_dir():
            for p in sorted(path.rglob("*")):
                if p.is_file():
                    h.update(str(p.relative_to(path)).encode("utf-8") + b"\0")
                    try:
                        h.update(p.read_bytes())
                    except OSError:
                        h.update(b"<unreadable>")
                    h.update(b"\0")
        elif path.is_file():
            h.update(path.read_bytes())
        else:
            return ""
    except OSError:
        return ""
    return h.hexdigest()


def _mounted_config_digest(server: dict[str, Any], runtime_dir: Path) -> str:
    """Digest of the host config a server MOUNTS (apply:aws_config /
    apply:kubeconfig). The kind-cluster internal kubeconfig's CA rotation is
    handled separately by kind_kubeconfig_stale; here we cover the ~/.aws
    directory (config + SSO token cache + credentials) and a user-supplied
    kubeconfig path. Companions resolve the same path as their parent
    (_resolve_param_value uses the owner), so both recreate together."""
    parts: list[str] = []
    for param in server.get("params", []):
        apply = param.get("apply")
        if apply == "aws_config":
            val = _resolve_param_value(server, param["name"], runtime_dir)
            aws_dir = Path(val).expanduser() if val else Path.home() / ".aws"
            parts.append("aws_config:" + _path_content_digest(aws_dir))
        elif apply == "kubeconfig":
            val = _resolve_param_value(server, param["name"], runtime_dir)
            if val:  # kind_cluster (no explicit path) is covered by kind_kubeconfig_stale
                parts.append("kubeconfig:" + _path_content_digest(Path(val).expanduser()))
    if not parts:
        return ""
    return hashlib.sha256("|".join(sorted(parts)).encode("utf-8")).hexdigest()


def server_spec_fingerprint(server: dict[str, Any], runtime_dir: Path) -> str:
    """Hash the parts of a server's config entry that determine its running
    container: type/package-or-registry/transport/run_flags/server_args/
    server_args_after/env/
    secret TARGETS (never secret values — those live in `thv secret`, not this
    repo) and configured PARAM NAMES (not their values, which come from runtime
    state and may reasonably change without forcing a rebuild here; a changed
    kubeconfig path is covered by `kind_kubeconfig_stale`, and the CONTENT of a
    server's mounted host config — the aws `~/.aws` dir, a user kubeconfig — is
    folded in via `mounted_config` so an `aws sso login`, a new profile, or an
    edited kubeconfig recreates the workload on the next start).

    Also hashes the *content* of the generated network permission profile —
    unlike ordinary params, a change here (a new GitLab/Alertmanager/
    Jira/Grafana hostname, or an edited allow_hosts list) must force a recreate,
    since the profile is baked into the container at `thv run` time and a plain
    `thv restart` would silently keep enforcing the OLD allowlist forever.

    Used to detect drift between what's currently running and what
    toolhive-servers.json now declares, so `start` can recreate a workload whose
    spec changed instead of blindly `thv restart`-ing stale container args (see
    `_apply_workload`: restart reuses the args baked in at the container's
    original `thv run`, so an edited env/flag/package silently never takes
    effect until something forces a recreate).
    """
    fingerprint_input = {
        "type": server.get("type"),
        "package": server.get("package"),
        "registry": server.get("registry"),
        "transport": server.get("transport"),
        "run_flags": list(server.get("run_flags", [])),
        "server_args": list(server.get("server_args", [])),
        "server_args_after": list(server.get("server_args_after", [])),
        "env": dict(sorted(server.get("env", {}).items())),
        "secret_targets": sorted(
            f"{sec['name']}->{sec['target']}" for sec in server.get("secrets", [])
        ),
        "param_names": sorted(p["name"] for p in server.get("params", [])),
        "permission_profile": build_permission_profile(server, runtime_dir),
        # Content of the mounted host config (aws ~/.aws, a user kubeconfig): a
        # change (aws sso login, a new profile, an edited kubeconfig) drifts this
        # and recreates the workload on the next start.
        "mounted_config": _mounted_config_digest(server, runtime_dir),
    }
    blob = json.dumps(fingerprint_input, sort_keys=True).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def load_server_fingerprints(runtime_dir: Path) -> dict[str, str]:
    """Last-applied spec fingerprint per workload, written after each `run`/`recreate`."""
    raw = _read_state(runtime_dir).get("fingerprints") or {}
    return {k: str(v) for k, v in raw.items() if isinstance(v, str)}


def save_server_fingerprint(runtime_dir: Path, name: str, fingerprint: str) -> None:
    with _locked_state(runtime_dir) as data:
        fingerprints = data.get("fingerprints") or {}
        fingerprints[name] = fingerprint
        data["fingerprints"] = fingerprints


def server_spec_changed(server: dict[str, Any], runtime_dir: Path) -> bool:
    """True if the server's declared spec differs from what was last applied.

    A workload with no recorded fingerprint (first run under this feature, or a
    workload created before it existed) is NOT treated as changed — there is
    nothing to compare against, and forcing a needless recreate on upgrade would
    re-trigger OAuth for every remote server. It gets a fingerprint recorded the
    first time it's applied, so drift is detected from then on.
    """
    name = server["name"]
    recorded = load_server_fingerprints(runtime_dir).get(name)
    if recorded is None:
        return False
    return recorded != server_spec_fingerprint(server, runtime_dir)


def ensure_group(group: str) -> None:
    thv("group", "create", group)  # idempotent; errors if it already exists


def _apply_workload(
    server: dict[str, Any],
    group: str,
    runtime_dir: Path,
    in_group: dict[str, str],
    all_names: set[str],
    dry_run: bool,
    start_lock: threading.Lock,
) -> list[tuple[bool, str]]:
    """Bring one workload to the desired state; return [(is_warning, message), …].

    Per-workload state (fingerprint/stale checks, `thv rm`) is safe to run
    concurrently since each server only touches its own workload. The actual
    `thv run`/`thv restart` call is serialized via `start_lock`: ToolHive's
    network-isolation ingress-proxy port allocator is a shared, global resource,
    and issuing two of those calls at once can race — both pick the same free
    host port, one bind wins and the other's ingress container is left
    permanently stuck in `Created` (seen with grafana/grafana_gov colliding on
    port 8001). This does mean `thv run`/`thv restart` calls (and any image pull
    they trigger) now run one at a time rather than overlapping — correctness
    over the pull-overlap speedup, since a lost race leaves a workload down
    until someone notices and manually restarts it.
    """
    name = server["name"]
    state = in_group.get(name)
    # A kind_cluster workload with a stale kubeconfig (cluster CA rotated) must be
    # recreated so it remounts a fresh internal kubeconfig — restart won't remount.
    stale = kind_kubeconfig_stale(server, runtime_dir)
    # A workload whose declared spec (package/env/flags/secret targets/...) has
    # drifted from what's actually running must also be recreated — `thv restart`
    # reuses the container's original `thv run` args, so it would silently keep
    # running the OLD spec forever otherwise (e.g. an added `env` var never
    # actually reaches the container).
    spec_changed = server_spec_changed(server, runtime_dir)
    if state == "running" and not stale and not spec_changed:
        return [(False, f"  workload {name}: already running")]
    if (stale or spec_changed) and name in all_names:
        action = "recreate"
    elif name in in_group:
        action = "restart"
    elif name in all_names:
        action = "recreate"  # exists in another group; must be rebuilt in ours
    else:
        action = "run"
    if dry_run:
        return [(False, f"  would {action} workload {name}")]
    msgs: list[tuple[bool, str]] = []
    if action == "restart":
        msgs.append((False, f"  restarting workload {name} …"))
        with start_lock:
            result = thv("restart", name)
        if result.returncode != 0:
            msgs.append((True, f"  warning: `thv restart {name}` failed: {result.stderr.strip()}"))
        else:
            save_server_fingerprint(runtime_dir, name, server_spec_fingerprint(server, runtime_dir))
        return msgs
    if action == "recreate":
        reasons = []
        if stale:
            reasons.append("kubeconfig changed")
        if spec_changed:
            reasons.append("spec changed")
        if not reasons:
            reasons.append(f"exists outside group '{group}'")
        msgs.append((False, f"  recreating workload {name} ({', '.join(reasons)}) …"))
        thv("rm", name)  # names are global; OAuth tokens persist via the secrets provider
    msgs.append((False, f"  starting workload {name} …"))
    # capture_output so concurrent workloads don't interleave on the terminal; the
    # browser-based OAuth flow for remote servers is handled by the detached proxy
    # (logs to thv's own file), so nothing interactive is lost by not streaming here.
    with start_lock:
        result = subprocess.run(
            build_thv_run_argv(server, group, runtime_dir), capture_output=True, text=True
        )
    if result.returncode != 0:
        msgs.append((True, f"  warning: `thv run {name}` exited {result.returncode}: {result.stderr.strip()}"))
    else:
        save_server_fingerprint(runtime_dir, name, server_spec_fingerprint(server, runtime_dir))
    return msgs


def run_workloads(
    config: dict[str, Any],
    runtime_dir: Path,
    dry_run: bool = False,
) -> int:
    """Ensure the group exists and every enabled workload is up (idempotent).

    Workloads persist across `stop`, so on a later `start` they already exist:
    - already running in our group, spec unchanged -> leave it
    - already running in our group, spec CHANGED since it was created -> recreate
      (see `server_spec_changed`; `thv restart` would keep the stale container)
    - exists in our group but stopped, spec unchanged -> restart (don't `thv run`,
      which errors "already exists")
    - exists OUTSIDE our group (orphan / name collision) -> remove and recreate in-group
    - absent -> `thv run`

    Enabled workloads are brought up concurrently for their per-workload prep
    (fingerprint/stale checks), but the actual `thv run`/`thv restart` call is
    serialized via `start_lock` — see `_apply_workload` for why (a shared
    ToolHive port allocator, not a per-workload lock, guards the container's
    network-isolation ingress proxy).
    """
    group = group_name(config)
    ensure_group(group)
    in_group = list_workloads(group)
    all_names = list_all_workload_names()

    targets = enabled_servers(config, runtime_dir)
    if not targets:
        return 0
    start_lock = threading.Lock()
    with ThreadPoolExecutor(max_workers=len(targets)) as pool:
        results = pool.map(
            lambda s: _apply_workload(s, group, runtime_dir, in_group, all_names, dry_run, start_lock),
            targets,
        )
        # pool.map preserves input order, so messages print in server order.
        for msgs in results:
            for is_warning, msg in msgs:
                print(msg, file=sys.stderr if is_warning else sys.stdout)
    return 0


def wait_for_workloads_running(
    config: dict[str, Any], runtime_dir: Path, timeout: float = 120.0
) -> None:
    """Block until every enabled workload reports `running`, or timeout.

    `thv vmcp init` only captures backends that are healthy at that instant, so
    generating the config before slow npx workloads finish starting would silently
    drop them. Warn (don't fail) on any that never come up — they'll just be absent.

    A workload stuck in `error` gets automatically `thv restart`'d (up to
    MAX_ERROR_RETRIES times). `run_workloads` serializes the `thv run`/`thv
    restart` calls themselves to shrink the ingress-proxy port-allocation race
    that used to cause this, but `thv run`/`thv restart` fork a detached
    background process and return before that process finishes creating
    containers — so our lock can't fully close the window, and a retry here can
    itself occasionally lose the same race. Retrying more than once, rather than
    leaving it down until someone notices and restarts it by hand, is the actual
    fix for the user-visible failure.
    """
    MAX_ERROR_RETRIES = 3
    group = group_name(config)
    want = [s["name"] for s in enabled_servers(config, runtime_dir)]
    if not want:
        return
    deadline = time.time() + timeout
    pending = list(want)
    retries: dict[str, int] = {}
    while pending and time.time() < deadline:
        states = list_workloads(group)
        pending = [n for n in want if states.get(n) != "running"]
        if not pending:
            print(f"  all {len(want)} workloads running")
            return
        for name in pending:
            if states.get(name) == "error" and retries.get(name, 0) < MAX_ERROR_RETRIES:
                retries[name] = retries.get(name, 0) + 1
                print(f"  workload {name} is in error state — restarting "
                      f"(attempt {retries[name]}/{MAX_ERROR_RETRIES}) …")
                thv("restart", name)
        time.sleep(2)
    print(f"  warning: workloads not running after {int(timeout)}s: {pending} "
          "— they will be omitted from the vMCP until healthy", file=sys.stderr)


# ---------------------------------------------------------------------------
# vMCP config generation
# ---------------------------------------------------------------------------


def _parse_init_backends(text: str) -> list[dict[str, str]]:
    """Extract backend blocks from `thv vmcp init` YAML (stdlib-only, no pyyaml).

    Mirrors the demo's flat-YAML regex approach: blocks are indented `- name:`
    entries carrying `url:` and `transport:`.
    """
    backends: list[dict[str, str]] = []
    cur: dict[str, str] | None = None
    for line in text.splitlines():
        m = re.match(r"\s*-\s*name:\s*(\S+)", line)
        if m:
            cur = {"name": m.group(1).strip("\"'"), "url": "", "transport": "streamable-http"}
            backends.append(cur)
            continue
        if cur is not None:
            mu = re.match(r"\s+url:\s*(\S+)", line)
            mt = re.match(r"\s+transport:\s*(\S+)", line)
            if mu:
                cur["url"] = mu.group(1).strip("\"'")
            if mt:
                cur["transport"] = mt.group(1).strip("\"'")
    return [b for b in backends if b["url"]]


def _init_scalar(text: str, key: str) -> str | None:
    m = re.search(rf"^{key}:\s*(\S+)", text, re.M)
    return m.group(1).strip("\"'") if m else None


def generate_vmcp_config(
    config: dict[str, Any],
    runtime_dir: Path,
    validate: bool = True,
) -> Path:
    """Run `thv vmcp init`, post-process, write JSON (valid YAML), and validate.

    Tool scoping uses the native vMCP `aggregation.tools` primitive: any server
    with a `tools` allowlist in the config emits a `{workload, filter}` entry, so
    the vMCP exposes only those tools (raw, unprefixed names). Servers without a
    `tools` field expose everything. Backends whose URL is a legacy `/sse`
    endpoint are fixed to transport: sse (init mislabels them streamable-http).
    """
    group = group_name(config)
    paths = runtime_paths(runtime_dir)
    init_path = paths["vmcp_init"]
    out_path = paths["vmcp_config"]

    result = thv("vmcp", "init", "--group", group, "--output", str(init_path))
    if result.returncode != 0:
        raise SystemExit(f"`thv vmcp init` failed: {result.stderr.strip()}")

    text = init_path.read_text(encoding="utf-8")
    backends = _parse_init_backends(text)
    for b in backends:
        if "/sse" in b["url"]:
            b["transport"] = "sse"

    present = {b["name"] for b in backends}
    tool_filters = [
        {"workload": s["name"], "filter": s["tools"]}
        for s in config.get("servers", [])
        if s.get("tools") and s["name"] in present
    ]
    aggregation = {
        "conflictResolution": "prefix",
        "conflictResolutionConfig": {"prefixFormat": "{workload}_"},
    }
    if tool_filters:
        aggregation["tools"] = tool_filters

    cfg = {
        "name": _init_scalar(text, "name") or f"{group}-vmcp",
        "groupRef": _init_scalar(text, "groupRef") or group,
        "incomingAuth": {"type": "anonymous"},
        "outgoingAuth": {"source": "inline"},
        "aggregation": aggregation,
        "backends": backends,
    }
    out_path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")

    for b in backends:
        print(f"  backend {b['name']:14} transport={b['transport']}")
    for tf in tool_filters:
        print(f"  tool-filter {tf['workload']:11} {len(tf['filter'])} tools")

    if validate:
        vr = thv("vmcp", "validate", "--config", str(out_path))
        if vr.returncode != 0:
            raise SystemExit(f"vMCP config failed validation:\n{vr.stdout}\n{vr.stderr}")
    return out_path


# ---------------------------------------------------------------------------
# supervisord config
# ---------------------------------------------------------------------------


def _supervisord_env_str(env: dict[str, str]) -> str:
    """Format a dict as a supervisord environment= value (KEY="val",...).

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
    ghostshell: Path,
    tunnel_env: dict[str, str],
    vmcp_command: str,
    vmcp_env: dict[str, str],
    rcloneshell: Path,
    rclone_env: dict[str, str],
    caffeinate: bool = False,
    preexisting: frozenset[str] = frozenset(),
) -> str:
    """preexisting: SUPERVISED_PROGRAMS names already running outside supervisord
    (e.g. orphaned by a killed supervisord) -- give them autostart=false so
    supervisord doesn't spawn a competing instance and fail on the bound port."""
    sock = paths["supervisord_sock"]
    pidfile = paths["supervisord_pid"]
    logs = paths["logs"]

    def autostart(name: str) -> str:
        return "false" if name in preexisting else "true"

    caffeinate_block = f"""\
[program:caffeinate]
command=caffeinate -i
autostart=true
autorestart=true
startsecs=2
stopwaitsecs=4
redirect_stderr=true
stdout_logfile={logs}/caffeinate.log
stdout_logfile_maxbytes=1MB
stdout_logfile_backups=1

""" if caffeinate else ""
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

{caffeinate_block}[program:vmcp]
command={vmcp_command}
directory={REPO_ROOT}
environment={_supervisord_env_str(vmcp_env)}
autostart={autostart("vmcp")}
autorestart=true
startsecs=3
stopwaitsecs=10
redirect_stderr=true
stdout_logfile={logs}/vmcp.log
stdout_logfile_maxbytes=5MB
stdout_logfile_backups=2

[program:ghostunnel]
command={ghostshell}
directory={REPO_ROOT}
environment={_supervisord_env_str(tunnel_env)}
autostart={autostart("ghostunnel")}
autorestart=true
startsecs=2
stopwaitsecs=8
redirect_stderr=true
stdout_logfile={logs}/ghostunnel.log
stdout_logfile_maxbytes=5MB
stdout_logfile_backups=2

[program:rclone-s3]
command={rcloneshell}
directory={REPO_ROOT}
environment={_supervisord_env_str(rclone_env)}
autostart={autostart("rclone-s3")}
autorestart=true
startsecs=2
stopwaitsecs=8
redirect_stderr=true
stdout_logfile={logs}/rclone-s3.log
stdout_logfile_maxbytes=5MB
stdout_logfile_backups=2
"""


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
    if result.returncode not in (0, 3):
        # 0: all RUNNING, 3: reachable but some programs not RUNNING/FATAL etc.
        # Anything else (e.g. 4 "refused connection") means the socket is stale.
        return {}
    states: dict[str, str] = {}
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            states[parts[0]] = parts[1]
    return states


def is_supervisor_running(runtime_dir: Path) -> bool:
    return bool(get_supervisor_states(runtime_dir))


# ---------------------------------------------------------------------------
# Log helpers
# ---------------------------------------------------------------------------


def tail_log_iter(log_file: Path, n_lines: int = 50) -> Iterator[str]:
    """Yield the last n_lines of a log file, then follow it (like `tail -f`).

    Used by the TUI's background log panes. Blocks between reads; the caller is
    expected to run it in a thread.
    """
    with log_file.open("r", encoding="utf-8", errors="replace") as fh:
        # Prime with the tail.
        lines = fh.readlines()
        for line in lines[-n_lines:]:
            yield line.rstrip("\n")
        # Follow.
        while True:
            line = fh.readline()
            if line:
                yield line.rstrip("\n")
            else:
                time.sleep(0.4)


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


def _style_proc(state: str) -> str:
    if state == "RUNNING":
        return f"[green]{state}[/green]"
    if state == "EXTERNAL":
        return f"[cyan]{state}[/cyan]"
    if state in ("STARTING", "BACKOFF"):
        return f"[yellow]{state}[/yellow]"
    if state in ("STOPPED", "EXITED", "FATAL", "UNKNOWN"):
        return f"[red]{state}[/red]"
    return f"[dim]{state or '—'}[/dim]"


def _style_workload(state: str) -> str:
    if state == "running":
        return f"[green]{state}[/green]"
    if state in ("starting", "auth_retrying", "authenticating"):
        return f"[yellow]{state}[/yellow]"
    if state in ("stopped", "error", "unauthenticated"):
        return f"[red]{state}[/red]"
    return f"[dim]{state or 'not created'}[/dim]"


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------


def status(
    runtime_dir: Path = DEFAULT_RUNTIME_DIR,
    servers_config: Path = DEFAULT_SERVERS_CONFIG,
) -> int:
    """Rich table of workload + supervised-process state."""
    console, Table = _require_rich()
    config = load_servers_config(servers_config)
    group = group_name(config)

    workloads = list_workloads(group)
    state = load_server_state(runtime_dir)
    wl_table = Table(title=f"ToolHive workloads (group: {group})", show_header=True, header_style="bold cyan")
    wl_table.add_column("Workload", style="bold")
    wl_table.add_column("Status")
    for server in config.get("servers", []):
        name = server["name"]
        if not is_server_enabled(server, state):
            wl_table.add_row(name, "[dim]disabled[/dim]")
            continue
        wl_table.add_row(name, _style_workload(workloads.get(name, "")))
    console.print(wl_table)

    sup_states = get_supervisor_states(runtime_dir)
    not_running = not sup_states
    # A program supervisord isn't managing (never started, or left autostart=false
    # because `start` found it already running externally) shows as STOPPED here --
    # probe its port so an inherited, actually-up process doesn't read as down.
    probe_addrs = {
        "vmcp": vmcp_target(config),
        "ghostunnel": default_listen(),
        "rclone-s3": os.environ.get("RCLONE_ADDR", DEFAULT_RCLONE_ADDR),
    }
    proc_table = Table(title="Host stack", show_header=True, header_style="bold cyan")
    proc_table.add_column("Process", style="bold")
    proc_table.add_column("State")
    for prog in ALL_PROGRAMS:
        state = sup_states.get(prog, "STOPPED" if not_running else "")
        if state in ("", "STOPPED") and prog in probe_addrs and _addr_reachable(probe_addrs[prog]):
            state = "EXTERNAL"
        proc_table.add_row(prog, _style_proc(state))
    console.print(proc_table)
    return 0


def ensure_ghostunnel_material() -> None:
    """If the host ghostunnel material is missing, recover it from the kind Secret.

    ghostunnel (server side) needs server.crt/server.key/ca.cert. Those are written
    to ~/.vicegerent/ghostunnel by setup-secrets-platform.sh, which also mirrors them
    to the kind Secret `ghostunnel-server`. On a host that's missing them, pull them
    back from the cluster before ghostunnel starts. (The CA *key* is never mirrored —
    it's only needed to re-issue certs, so run setup-secrets to fully rebuild.)
    """
    hd = Path(os.environ.get("GHOSTUNNEL_HOST_DIR", str(DEFAULT_GHOSTUNNEL_DIR)))
    missing = [f for f in GHOSTUNNEL_FILES if not (hd / f).is_file() or (hd / f).stat().st_size == 0]
    if not missing:
        return
    current_ctx = subprocess.run(
        ["kubectl", "config", "current-context"], capture_output=True, text=True,
    ).stdout.strip()
    if current_ctx != GHOSTUNNEL_KUBE_CONTEXT:
        print(
            f"ghostunnel material missing {missing}, but current kubectl context is "
            f"'{current_ctx or '<none>'}', expected '{GHOSTUNNEL_KUBE_CONTEXT}'. "
            f"Run: kubectl config use-context {GHOSTUNNEL_KUBE_CONTEXT}",
            file=sys.stderr,
        )
        return

    print(f"ghostunnel material missing {missing}; recovering from kind Secret {GHOSTUNNEL_SECRET} …")
    hd.mkdir(parents=True, exist_ok=True)
    hd.chmod(0o700)
    for fname in missing:
        key = GHOSTUNNEL_FILES[fname].replace(".", r"\.")
        result = subprocess.run(
            ["kubectl", "--context", GHOSTUNNEL_KUBE_CONTEXT, "-n", GHOSTUNNEL_SECRET_NS,
             "get", "secret", GHOSTUNNEL_SECRET, "-o", f"jsonpath={{.data.{key}}}"],
            capture_output=True, text=True,
        )
        if result.returncode != 0 or not result.stdout.strip():
            print(
                f"  could not recover {fname} from kind ({result.stderr.strip() or 'secret/key absent'}).\n"
                "  Run `./vicegerent secrets setup platform` to (re)generate the ghostunnel material.",
                file=sys.stderr,
            )
            return  # leave it missing; ghostshell.sh will fail with a clear message
        (hd / fname).write_bytes(base64.b64decode(result.stdout))
        (hd / fname).chmod(0o600)
        print(f"  restored {fname} from kind")


def ensure_rclone_material() -> None:
    """If the host rclone S3 auth-key is missing, recover it from the velero
    credential Secret (mirrors ensure_ghostunnel_material).

    The Secret's `cloud` key is an AWS credentials file; the auth-key file is the
    `access,secret` pair `rclone serve s3 --auth-key` expects. Both are seeded by
    setup-secrets-platform.sh, which also applies the Secret — so a laptop missing
    the file can rebuild it from the cluster before rclone starts.
    """
    d = Path(os.environ.get("RCLONE_S3_HOST_DIR", str(DEFAULT_RCLONE_S3_DIR)))
    authkey = d / "auth-key"
    if authkey.is_file() and authkey.stat().st_size > 0:
        return
    current_ctx = subprocess.run(
        ["kubectl", "config", "current-context"], capture_output=True, text=True,
    ).stdout.strip()
    if current_ctx != GHOSTUNNEL_KUBE_CONTEXT:
        print(
            f"rclone auth-key missing, but current kubectl context is "
            f"'{current_ctx or '<none>'}', expected '{GHOSTUNNEL_KUBE_CONTEXT}'. "
            f"Run: ./vicegerent secrets setup platform",
            file=sys.stderr,
        )
        return
    print(f"rclone auth-key missing; recovering from kind Secret {VELERO_SECRET} …")
    result = subprocess.run(
        ["kubectl", "--context", GHOSTUNNEL_KUBE_CONTEXT, "-n", VELERO_SECRET_NS,
         "get", "secret", VELERO_SECRET, "-o", "jsonpath={.data.cloud}"],
        capture_output=True, text=True,
    )
    if result.returncode != 0 or not result.stdout.strip():
        print(
            f"  could not recover the auth-key from kind ({result.stderr.strip() or 'secret/key absent'}).\n"
            "  Run `./vicegerent secrets setup platform` to (re)generate the Velero credentials.",
            file=sys.stderr,
        )
        return
    cloud = base64.b64decode(result.stdout).decode("utf-8", "replace")
    access = secret = ""
    for line in cloud.splitlines():
        if line.startswith("aws_access_key_id="):
            access = line.split("=", 1)[1].strip()
        elif line.startswith("aws_secret_access_key="):
            secret = line.split("=", 1)[1].strip()
    if not access or not secret:
        print(f"  {VELERO_SECRET} Secret is malformed (missing key id/secret).", file=sys.stderr)
        return
    d.mkdir(parents=True, exist_ok=True)
    d.chmod(0o700)
    authkey.write_text(f"{access},{secret}\n", encoding="utf-8")
    authkey.chmod(0o600)
    print("  restored rclone auth-key from kind")


def start_stack(
    runtime_dir: Path = DEFAULT_RUNTIME_DIR,
    servers_config: Path = DEFAULT_SERVERS_CONFIG,
    ghostshell: Path | None = None,
    listen: str | None = None,
    allow_cn: str | None = None,
    skip_workloads: bool = False,
    caffeinate: bool | None = None,
    always_caffeinate: bool = False,
) -> int:
    """Full bring-up: thv workloads -> vMCP config -> supervisord (vMCP/ghostunnel, opt-in caffeinate)."""
    paths = runtime_paths(runtime_dir)
    config = load_servers_config(servers_config)

    if is_supervisor_running(runtime_dir):
        print("supervisord is already running. Use 'stop' first.")
        return 1

    # caffeinate is opt-in: explicit --caffeinate/--no-caffeinate wins, else the
    # persisted "always" preference (default off). --always saves the choice.
    use_caffeinate = caffeinate if caffeinate is not None else caffeinate_always(runtime_dir)
    if always_caffeinate:
        set_caffeinate_always(runtime_dir, use_caffeinate)

    paths["logs"].mkdir(parents=True, exist_ok=True)

    ensure_ghostunnel_material()

    if not skip_workloads:
        print("Ensuring ToolHive workloads …")
        run_workloads(config, runtime_dir)
        # `thv vmcp init` only captures backends that are HEALTHY right now, so wait
        # for the (often slow, npx-download) workloads to come up first — otherwise
        # they're silently omitted from the vMCP config and never aggregated.
        wait_for_workloads_running(config, runtime_dir)

    print("Generating vMCP config …")
    vmcp_cfg = generate_vmcp_config(config, runtime_dir)

    port = vmcp_port(config)
    thv_bin = _thv_path()
    # Tier 1 FTS5 keyword optimizer: collapses every backend's tools down to
    # find_tool/call_tool, cutting the tokens spent on tool definitions as more
    # servers are enabled. Requires mcp-cerbos-shim to unwrap call_tool (it does —
    # see server.go callToolMeta) or Cerbos-guarded tools would silently bypass
    # authorization. Set VMCP_OPTIMIZER=0 to fall back to exposing all tools raw.
    optimizer_flag = "" if os.environ.get("VMCP_OPTIMIZER", "1") == "0" else " --optimizer"
    vmcp_command = f'{thv_bin} vmcp serve --config {vmcp_cfg} --port {port}{optimizer_flag}'
    # Ensure thv's dir (and Homebrew) are on PATH for the supervised process.
    path_env = os.pathsep.join(
        dict.fromkeys([str(Path(thv_bin).parent), "/opt/homebrew/bin", os.environ.get("PATH", "")])
    )
    vmcp_env = {"PATH": path_env, "HOME": str(Path.home())}

    effective_ghostshell = ghostshell or DEFAULT_GHOSTSHELL
    effective_listen = listen or default_listen()
    target = vmcp_target(config)
    tunnel_env: dict[str, str] = {
        "TARGET": target,
        "LISTEN": effective_listen,
        "ALLOW_CN": allow_cn or DEFAULT_AGENT_CLIENT_CN,
    }

    ensure_rclone_material()
    rclone_addr = os.environ.get("RCLONE_ADDR", DEFAULT_RCLONE_ADDR)
    rclone_serve_dir = os.environ.get("RCLONE_SERVE_DIR", str(DEFAULT_RCLONE_SERVE_DIR))
    rclone_env: dict[str, str] = {
        "RCLONE_S3_HOST_DIR": os.environ.get("RCLONE_S3_HOST_DIR", str(DEFAULT_RCLONE_S3_DIR)),
        "ADDR": rclone_addr,
        "SERVE_DIR": rclone_serve_dir,
        "BUCKET": RCLONE_BUCKET,
        "PATH": path_env,
        "HOME": str(Path.home()),
    }

    # A prior supervisord could have died without stopping its children, leaving
    # vmcp/ghostunnel/rclone-s3 orphaned but still bound to their ports. Starting a
    # fresh instance for one of those would just lose the port race and go FATAL, so
    # leave any already-reachable one alone instead (autostart=false in the conf).
    probe_addrs = {"vmcp": target, "ghostunnel": effective_listen, "rclone-s3": rclone_addr}
    preexisting = frozenset(name for name, addr in probe_addrs.items() if _addr_reachable(addr))
    if preexisting:
        print(f"Already running outside supervisord, leaving in place: {', '.join(sorted(preexisting))}")

    conf_text = build_supervisord_conf(
        paths, effective_ghostshell, tunnel_env, vmcp_command, vmcp_env,
        DEFAULT_RCLONESHELL, rclone_env, use_caffeinate, preexisting,
    )
    paths["supervisord_conf"].write_text(conf_text, encoding="utf-8")

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

    expected = (("caffeinate", *SUPERVISED_PROGRAMS) if use_caffeinate else SUPERVISED_PROGRAMS)
    # preexisting programs are autostart=false -- supervisord never touches them,
    # so only wait on the ones it's actually meant to bring up.
    managed = [p for p in expected if p not in preexisting]
    # Wait up to 15s for all managed programs to reach RUNNING.
    deadline = time.time() + 15
    while time.time() < deadline:
        sup_states = get_supervisor_states(runtime_dir)
        if all(sup_states.get(p) == "RUNNING" for p in managed):
            break
        time.sleep(0.5)

    sup_states = get_supervisor_states(runtime_dir)
    for prog in expected:
        state = "RUNNING (external, inherited)" if prog in preexisting else sup_states.get(prog, "unknown")
        print(f"  {prog}: {state}")
    print(f"vMCP:          127.0.0.1:{port}  (ToolHive, group '{group_name(config)}')")
    print(f"ghostunnel:    {effective_listen} -> {target}")
    print(f"rclone-s3:     {rclone_addr} -> {rclone_serve_dir} (bucket '{RCLONE_BUCKET}')")
    print(f"caffeinate:    {'on' if use_caffeinate else 'off'}")
    failed = [p for p in managed if sup_states.get(p) != "RUNNING"]
    if failed:
        print(f"\nwarning: {failed} did not reach RUNNING; check logs under {paths['logs']}", file=sys.stderr)
        return 1
    return 0


def stop_stack(
    runtime_dir: Path = DEFAULT_RUNTIME_DIR,
    servers_config: Path = DEFAULT_SERVERS_CONFIG,
    stop_workloads: bool = True,
) -> int:
    """Shut down the supervised stack (vMCP/ghostunnel + caffeinate if enabled) and,
    by default, the ToolHive workloads too.

    Workloads are `thv stop`'d (stopped, not removed), so their persisted OAuth
    sessions survive and the next `start` won't re-prompt. Pass stop_workloads=False
    (`--keep-workloads`) to leave them running.
    """
    config = load_servers_config(servers_config)
    if stop_workloads:
        group = group_name(config)
        running = [name for name, st in list_workloads(group).items() if st == "running"]
        if running:
            print(f"  stopping {len(running)} workloads: {', '.join(running)} …")
            # Concurrent: per-workload `thv` locks make parallel stops safe.
            with ThreadPoolExecutor(max_workers=len(running)) as pool:
                list(pool.map(lambda n: thv("stop", n), running))

    paths = runtime_paths(runtime_dir)
    rc = 0
    if is_supervisor_running(runtime_dir):
        result = supervisorctl("shutdown", runtime_dir=runtime_dir)
        print(result.stdout.strip() or "supervisord shutdown initiated")

        # Wait up to 15s for the supervisor socket to disappear (processes fully exited).
        sock = paths["supervisord_sock"]
        deadline = time.time() + 15
        while time.time() < deadline:
            if not sock.exists():
                break
            time.sleep(0.5)
        else:
            print("warning: supervisord did not exit within 15s", file=sys.stderr)
            rc = 1
    else:
        print("supervisord is not running")

    # `supervisorctl shutdown` above only ever reaches whichever supervisord
    # currently owns the socket path -- a stray instance a *prior* stop failed to
    # reach (e.g. orphaned when some earlier `start` recreated that path out from
    # under it) keeps running with autorestart=true, immune to the check above.
    # Find and kill it by command line instead, or it'll just resurrect whatever
    # the port sweep below kills.
    stray = _kill_stray_supervisord(paths)
    if stray:
        print(f"  killed stray supervisord ({', '.join(str(p) for p in stray)})")

    # supervisorctl shutdown only stops what supervisord is currently tracking --
    # anything still listening on these ports afterward is an orphan (see start's
    # autostart=false inherit path). Kill it so the next `start` always gets a
    # fresh, fully supervisord-managed instance instead of inheriting one again.
    port_addrs = {
        "vmcp": vmcp_target(config),
        "ghostunnel": default_listen(),
        "rclone-s3": os.environ.get("RCLONE_ADDR", DEFAULT_RCLONE_ADDR),
    }
    for name, addr in port_addrs.items():
        killed = _kill_addr_listeners(addr)
        if killed:
            print(f"  killed orphaned {name} ({', '.join(str(p) for p in killed)})")

    return rc


_LOG_NAMES = ("ghostunnel", "vmcp", "rclone-s3", "supervisord", "caffeinate")


def tail_log(
    process_name: str,
    runtime_dir: Path = DEFAULT_RUNTIME_DIR,
    n_lines: int = 50,
) -> int:
    """Tail logs for a supervised process (or supervisord itself)."""
    paths = runtime_paths(runtime_dir)
    log_file = paths["logs"] / f"{process_name}.log"
    if not log_file.exists():
        print(f"no log file yet for {process_name!r}: {log_file}", file=sys.stderr)
        return 1
    try:
        subprocess.run(["tail", f"-n{n_lines}", "-f", str(log_file)])
    except KeyboardInterrupt:
        pass
    return 0


def doctor(
    servers_config: Path = DEFAULT_SERVERS_CONFIG,
) -> int:
    """Check host prerequisites for the ToolHive + vMCP + ghostunnel stack."""
    config = load_servers_config(servers_config)
    group = group_name(config)
    ok = True

    print("binaries:")
    for binary in ("thv", "ghostunnel", "rclone", "supervisord", "supervisorctl", "caffeinate", "kind"):
        found = shutil.which(binary)
        print(f"  {binary}: {found or 'MISSING'}")
        if not found and binary != "kind":
            ok = False

    print("thv secrets provider:")
    prov = thv("secret", "list")
    if prov.returncode == 0:
        print("  configured (thv secret list OK)")
    else:
        print("  NOT configured — run `thv secret setup` (choose 'encrypted')")
        ok = False

    print("required thv secrets:")
    needed = sorted({sec["name"] for s in config.get("servers", []) for sec in s.get("secrets", [])}
                     | {param_secret_name(s["name"], p["name"])
                        for s in config.get("servers", []) for p in s.get("params", []) if p.get("secret")})
    for name in needed:
        present = thv("secret", "get", name).returncode == 0
        print(f"  {name}: {'present' if present else 'MISSING (thv secret set ' + name + ')'}")
        if not present:
            ok = False

    print("kind cluster:")
    clusters = {s.get("kind_cluster") for s in config.get("servers", []) if s.get("kind_cluster")}
    for cluster in sorted(c for c in clusters if c):
        reachable = subprocess.run(
            ["kind", "get", "kubeconfig", "--name", cluster, "--internal"],
            capture_output=True, text=True,
        ).returncode == 0
        print(f"  {cluster}: {'reachable' if reachable else 'NOT reachable (kind create cluster / vicegerent cluster setup)'}")
        if not reachable:
            ok = False

    print(f"group:         {group}")
    print(f"vMCP target:   {vmcp_target(config)}")
    print(f"ghostunnel:    {default_listen()}")
    print(f"rclone-s3:     {DEFAULT_RCLONE_ADDR} -> {DEFAULT_RCLONE_SERVE_DIR} (bucket '{RCLONE_BUCKET}')")
    return 0 if ok else 1


def run_tui(
    runtime_dir: Path = DEFAULT_RUNTIME_DIR,
    servers_config: Path = DEFAULT_SERVERS_CONFIG,
) -> int:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    try:
        from tui import HostMCPApp
    except ImportError as exc:
        raise SystemExit(f"textual is required for the TUI: {exc}\n  pip install -r host/mcp/requirements-host.txt")
    HostMCPApp(runtime_dir=runtime_dir, servers_config=servers_config).run()
    return 0


# ---------------------------------------------------------------------------
# CLI command wrappers
# ---------------------------------------------------------------------------


def _prompt_yn(prompt: str, default: bool) -> bool:
    suffix = " [Y/n] " if default else " [y/N] "
    try:
        ans = input(prompt + suffix).strip().lower()
    except EOFError:
        return default
    if not ans:
        return default
    return ans in ("y", "yes")


def _server_auth_line(server: dict[str, Any]) -> str:
    if server.get("type") == "remote":
        return "auth: OAuth — a browser opens on first `start` to authorize (token then persists)."
    secrets = server.get("secrets", [])
    if secrets:
        return f"auth: API key via `thv` secret ({', '.join(s['name'] for s in secrets)})."
    if server.get("kind_cluster"):
        return f"auth: uses the kind '{server['kind_cluster']}' cluster kubeconfig (no secret)."
    return "auth: none."


def configure(
    runtime_dir: Path = DEFAULT_RUNTIME_DIR,
    servers_config: Path = DEFAULT_SERVERS_CONFIG,
) -> int:
    """Interactively walk each MCP server: enable + set up secrets, or skip.

    Skipping (or answering no) disables the server so ToolHive never runs it.
    Choices persist in the runtime servers-state file; secrets go to `thv`.
    """
    config = load_servers_config(servers_config)
    group = group_name(config)
    servers = config.get("servers", [])
    state = load_server_state(runtime_dir)
    params_all = load_server_params(runtime_dir)

    print(f"\nConfigure ToolHive MCP servers (group: {group}).")
    print("For each server: enable and set it up, or skip it (ToolHive won't run it).")
    have_provider = thv("secret", "list").returncode == 0
    if not have_provider:
        print(
            "\n! No `thv` secrets provider configured — servers that need an API key\n"
            "  can't be set up yet."
        )
        if _prompt_yn("  Set one up now (choose 'encrypted')?", default=True):
            subprocess.run([_thv_path(), "secret", "setup"])
            have_provider = thv("secret", "list").returncode == 0
        if not have_provider:
            print(
                "  ! Still no provider — enabling servers anyway, but set their keys later\n"
                "    (re-run `vicegerent mcp configure` after `thv secret setup`).\n"
            )

    running = list_workloads(group)
    for server in servers:
        name = server["name"]
        # Hidden companions (companion_of) are enabled/configured with their
        # parent as one unit and never shown on their own — skip entirely.
        if server.get("companion_of"):
            continue
        secrets = server.get("secrets", [])
        currently = is_server_enabled(server, state)
        print(f"\n── {name} ──  (currently {'enabled' if currently else 'disabled'})")
        if server.get("description"):
            print(f"   {server['description']}")
        print(f"   {_server_auth_line(server)}")

        if not _prompt_yn(f"   Enable {name}?", default=currently):
            state[name] = False
            if running.get(name):
                print(f"   stopping running workload {name} …")
                thv("stop", name)
            print(f"   {name}: disabled.")
            continue

        # Parameters (GitLab URL, kubeconfig path, …). Most live in runtime state;
        # one marked "secret": true lives in the `thv` secrets provider instead
        # (param_secret_name) so it survives a wiped/corrupted runtime dir. Either
        # way the value is typed and shown here in the clear -- these aren't
        # sensitive, `thv secret` is just durable storage, so we do our own visible
        # prompt and pipe it into `thv secret set` rather than let it hide the input.
        #
        # A param can additionally set "sensitive": true (only meaningful alongside
        # "secret": true) for genuinely confidential values that must still be
        # templated into argv via apply:server_arg -- e.g. an API key baked into a
        # --header flag, which can't go through the top-level secrets[]/--secret
        # mechanism below since that only injects container env vars, not CLI args.
        # Such a param skips the visible echo-prompt entirely and instead follows
        # the exact same hidden-input model as the secrets[] loop further down:
        # `thv secret set <name>` is invoked directly with no captured input/output,
        # so thv's own hidden terminal prompt handles it and the value never passes
        # through Python (never printed, never echoed back on a later run). The one
        # exception is a param with "value_prefix": the value is read hidden via
        # getpass and that fixed prefix prepended before storing (idempotently), so
        # the operator pastes just the bare secret and can't forget a required
        # scheme (e.g. the "ApiKey " that elastic's Authorization header must carry).
        for param in server.get("params", []):
            pname = param["name"]
            prompt = param.get("prompt", pname)
            use_secret = bool(param.get("secret"))
            sensitive = bool(param.get("sensitive"))
            if use_secret and not have_provider:
                print(f"   ! {prompt} needs a secrets provider — set it after `thv secret setup`.")
                continue
            sname = param_secret_name(name, pname) if use_secret else None

            if sname and sensitive:
                prefix = param.get("value_prefix", "")
                exists = thv("secret", "get", sname).returncode == 0
                if exists and not _prompt_yn(f"   secret '{sname}' is already set — replace it?", default=False):
                    print(f"   keeping existing '{sname}'.")
                elif prefix:
                    # value_prefix is a fixed scheme the stored secret MUST carry
                    # (e.g. "ApiKey " for an Authorization header injected verbatim
                    # by --remote-forward-headers-secret). Read the value hidden via
                    # getpass so it still never echoes, prepend the prefix
                    # idempotently, and pipe it in — the operator pastes just the
                    # bare secret and can't forget the scheme. The value passes
                    # through Python only in memory here (never printed), the one
                    # deviation from the no-capture path below, required to prepend.
                    entered = getpass.getpass(f"   {prompt} (input hidden): ").strip()
                    if entered:
                        value = entered if entered.startswith(prefix) else prefix + entered
                        rc = subprocess.run(
                            [_thv_path(), "secret", "set", sname],
                            input=value, text=True, capture_output=True,
                        ).returncode
                        if rc != 0:
                            print(f"   warning: saving {pname} failed (rc={rc}); {name} may not work.")
                else:
                    print(f"   setting '{sname}' (input hidden):")
                    rc = subprocess.run([_thv_path(), "secret", "set", sname]).returncode
                    if rc != 0:
                        print(f"   warning: saving {pname} failed (rc={rc}); {name} may not work.")
                if param.get("required") and thv("secret", "get", sname).returncode != 0:
                    print(f"   ! {pname} is required — {name} won't work until it's set.")
                continue

            current = (
                read_secret_value(sname) if sname
                else params_all.get(name, {}).get(pname) or str(param.get("default") or "")
            )
            shown = current if current else "(none)"
            try:
                entered = input(f"   {prompt} [{shown}]: ").strip()
            except EOFError:
                entered = ""
            value = entered if entered else current
            if sname:
                if value != current:
                    rc = subprocess.run(
                        [_thv_path(), "secret", "set", sname],
                        input=value, text=True, capture_output=True,
                    ).returncode
                    if rc != 0:
                        print(f"   warning: saving {pname} failed (rc={rc}); {name} may not work.")
            else:
                params_all.setdefault(name, {})[pname] = value
            if param.get("required") and not value:
                print(f"   ! {pname} is required — {name} won't work until it's set.")

        if secrets and not have_provider:
            print(f"   ! {name} needs a secrets provider — enabling anyway, but set the key later.")
        for sec in secrets if have_provider else []:
            sname = sec["name"]
            exists = thv("secret", "get", sname).returncode == 0
            if exists and not _prompt_yn(f"   secret '{sname}' is already set — replace it?", default=False):
                print(f"   keeping existing '{sname}'.")
                continue
            print(f"   setting '{sname}' (input hidden):")
            rc = subprocess.run([_thv_path(), "secret", "set", sname]).returncode
            if rc != 0:
                print(f"   warning: `thv secret set {sname}` failed (rc={rc}); {name} may not work.")
        state[name] = True
        print(f"   {name}: enabled.")

    save_server_state(runtime_dir, state)
    save_server_params(runtime_dir, params_all)
    # Companions are hidden — they follow their parent and aren't listed.
    visible = [s for s in servers if not s.get("companion_of")]
    on = [s["name"] for s in visible if is_server_enabled(s, state)]
    off = [s["name"] for s in visible if not is_server_enabled(s, state)]
    print("\nSaved. enabled: " + (", ".join(on) or "(none)"))
    print("       disabled: " + (", ".join(off) or "(none)"))
    print("Run `vicegerent start` to bring the enabled servers up.")
    return 0


def set_enabled(
    name: str,
    enabled: bool,
    runtime_dir: Path = DEFAULT_RUNTIME_DIR,
    servers_config: Path = DEFAULT_SERVERS_CONFIG,
) -> int:
    """Non-interactive enable/disable of a single server (persists to state)."""
    config = load_servers_config(servers_config)
    by_name = {s["name"]: s for s in config.get("servers", [])}
    if name not in by_name:
        raise SystemExit(f"unknown server: {name!r}. Known: {sorted(by_name)}")
    parent = by_name[name].get("companion_of")
    if parent:
        raise SystemExit(
            f"{name!r} is a hidden companion of {parent!r} and follows it "
            f"automatically — enable/disable {parent!r} instead."
        )
    state = load_server_state(runtime_dir)
    state[name] = enabled
    save_server_state(runtime_dir, state)
    if not enabled and list_workloads(group_name(config)).get(name):
        thv("stop", name)
    print(f"{name}: {'enabled' if enabled else 'disabled'}")
    return 0


def cmd_configure(args: argparse.Namespace) -> int:
    return configure(args.runtime_dir, args.servers_config)


def cmd_enable(args: argparse.Namespace) -> int:
    return set_enabled(args.server, True, args.runtime_dir, args.servers_config)


def cmd_disable(args: argparse.Namespace) -> int:
    return set_enabled(args.server, False, args.runtime_dir, args.servers_config)


def cmd_status(args: argparse.Namespace) -> int:
    return status(args.runtime_dir, args.servers_config)


def cmd_start(args: argparse.Namespace) -> int:
    return start_stack(
        args.runtime_dir, args.servers_config, args.ghostshell,
        args.listen, args.allow_cn, args.skip_workloads,
        args.caffeinate, args.always_caffeinate,
    )


def cmd_stop(args: argparse.Namespace) -> int:
    return stop_stack(args.runtime_dir, args.servers_config, not args.keep_workloads)


def cmd_logs(args: argparse.Namespace) -> int:
    return tail_log(args.process, args.runtime_dir, args.lines)


def cmd_doctor(args: argparse.Namespace) -> int:
    return doctor(args.servers_config)


def cmd_tui(args: argparse.Namespace) -> int:
    return run_tui(args.runtime_dir, args.servers_config)


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


_HELP = """\
vicegerent-mcp — host-side ToolHive stack controller

Owns the local ToolHive stack behind the cluster's MCP access:
  ToolHive workloads (group 'vicegerent') -> vMCP aggregator on :4483
  -> ghostunnel (mTLS from the cluster), optionally kept awake by caffeinate.
Also runs rclone-s3 on :9899, the S3 backend for the cluster's Velero backups.
vMCP, ghostunnel, and rclone-s3 run under supervisord; the workloads run under
ToolHive's own daemon and persist across stack restarts.

Commands:
  configure              interactively enable/skip each MCP server + set secrets
  enable KEY             enable a server (persists; brought up on next start)
  disable KEY            disable a server (stops it; ToolHive won't run it)
  start [--caffeinate]   bring up enabled workloads + vMCP + ghostunnel (idempotent);
                         --caffeinate keeps macOS awake, --always to make it the default
  stop                   stop the supervised stack + ToolHive workloads (--keep-workloads to leave them)
  status                 workload + supervised-process state (rich table)
  logs PROC              tail logs  (ghostunnel | vmcp | rclone-s3 | supervisord | caffeinate)
  doctor                 check binaries, thv secrets provider + secrets, kind
  tui                    interactive dashboard (textual)

MCP servers are OFF by default; run `configure` (or `enable KEY`) to opt in.

Global options:
  --runtime-dir PATH     supervisord/runtime state directory
                         (default: ~/.vicegerent/mcp)
  --servers-config PATH  ToolHive servers config
                         (default: host/mcp/toolhive-servers.json)

Environment:
  THV_GROUP              ToolHive group name (default: vicegerent)
  VMCP_HOST / VMCP_PORT  vMCP loopback target (default 127.0.0.1:4483)
  LISTEN                 ghostunnel listen address (default 127.0.0.1:8453)
  RCLONE_ADDR            rclone serve s3 listen address (default 127.0.0.1:9899)

Run './vicegerent-mcp COMMAND --help' for per-command options.
"""


class _SuppressSubparsers(argparse.RawDescriptionHelpFormatter):
    """Hide the auto-generated subcommand list; the command table is in _HELP."""

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
    parser.add_argument(
        "--runtime-dir", type=Path, default=DEFAULT_RUNTIME_DIR, metavar="PATH",
        help="supervisord/runtime state directory (default: ~/.vicegerent/mcp)",
    )
    parser.add_argument(
        "--servers-config", type=Path, default=DEFAULT_SERVERS_CONFIG, metavar="PATH",
        help="ToolHive servers config (default: host/mcp/toolhive-servers.json)",
    )
    sub = parser.add_subparsers(dest="command", required=False)

    sub.add_parser("configure", help="interactively enable/skip each MCP server + set secrets").set_defaults(func=cmd_configure)

    for verb, fn, helptext in (
        ("enable", cmd_enable, "enable a server (persists; started on next start)"),
        ("disable", cmd_disable, "disable a server (stops it; ToolHive won't run it)"),
    ):
        p = sub.add_parser(verb, help=helptext)
        p.add_argument("server", metavar="KEY", help="server name from toolhive-servers.json")
        p.set_defaults(func=fn)

    start = sub.add_parser("start", help="bring up workloads + vMCP + ghostunnel")
    start.add_argument("--ghostshell", type=Path, default=None)
    start.add_argument(
        "--listen", default=None,
        help=f"ghostunnel listen address (default: $LISTEN or {DEFAULT_LISTEN})",
    )
    start.add_argument("--allow-cn", default=None, help="ghostunnel client certificate CN")
    start.add_argument(
        "--skip-workloads", action="store_true",
        help="don't run `thv run`; assume workloads are already up",
    )
    start.add_argument(
        "--caffeinate", dest="caffeinate", action="store_true", default=None,
        help="keep macOS awake while the stack runs (opt-in; default off)",
    )
    start.add_argument(
        "--no-caffeinate", dest="caffeinate", action="store_false",
        help="don't keep macOS awake, overriding a saved 'always' preference",
    )
    start.add_argument(
        "--always", dest="always_caffeinate", action="store_true",
        help="persist the caffeinate choice as the default for future starts",
    )
    start.set_defaults(func=cmd_start)

    stop = sub.add_parser("stop", help="stop the supervised stack + ToolHive workloads")
    stop.add_argument(
        "--keep-workloads", action="store_true",
        help="leave the ToolHive workloads running (default: `thv stop` them; auth survives)",
    )
    stop.set_defaults(func=cmd_stop)

    sub.add_parser("status", help="show workload + supervised-process state").set_defaults(func=cmd_status)

    logs = sub.add_parser("logs", help="tail logs for a supervised process (Ctrl-C to exit)")
    logs.add_argument("process", choices=list(_LOG_NAMES), help="which process log to tail")
    logs.add_argument("-n", "--lines", type=int, default=50, metavar="N", help="initial lines to show (default: 50)")
    logs.set_defaults(func=cmd_logs)

    sub.add_parser("doctor", help="check host prerequisites").set_defaults(func=cmd_doctor)

    sub.add_parser("tui", help="interactive dashboard (textual)").set_defaults(func=cmd_tui)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 0
    return args.func(args)


__all__ = [
    "runtime_paths",
    "load_servers_config",
    "group_name",
    "vmcp_port",
    "enabled_servers",
    "vmcp_target",
    "default_listen",
    "list_workloads",
    "build_thv_run_argv",
    "build_permission_profile",
    "write_permission_profile",
    "server_spec_fingerprint",
    "load_server_fingerprints",
    "save_server_fingerprint",
    "server_spec_changed",
    "run_workloads",
    "generate_vmcp_config",
    "get_supervisor_states",
    "is_supervisor_running",
    "tail_log_iter",
    "status",
    "start_stack",
    "stop_stack",
    "tail_log",
    "doctor",
    "run_tui",
    "DEFAULT_RUNTIME_DIR",
    "DEFAULT_SERVERS_CONFIG",
    "DEFAULT_GHOSTSHELL",
    "DEFAULT_LISTEN",
    "SUPERVISED_PROGRAMS",
]


if __name__ == "__main__":
    raise SystemExit(main())
