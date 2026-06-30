"""
Patch: make Slack always bypass the egress MITM proxy.

In the vicegerent sandbox ALL outbound traffic is pointed at the GET-only
scrubbing egress proxy via HTTPS_PROXY. Slack must NOT go through it — the proxy
scrubs ``xox*`` tokens, blocks POST, and blocks the Socket Mode WebSocket, so any
Slack call routed through it fails (and previously surfaced as a TLS error because
the proxy presents a MITM cert). The network policy already allows slack.com
directly; the only thing forcing Slack through the proxy is slack_sdk.

slack_sdk's ``AsyncBaseClient.__init__`` auto-loads ``HTTPS_PROXY`` whenever its
``proxy`` arg is ``None`` or empty, via ``load_http_proxy_from_env()`` — which
never consults ``NO_PROXY``. So even though the Hermes Slack adapter resolves the
bypass and clears ``app.client.proxy = None``, slack_bolt rebuilds a fresh
per-request context client as ``AsyncWebClient(token=..., proxy=app.client.proxy)``
= ``AsyncWebClient(proxy=None)`` — and that ``None`` re-triggers the env lookup, so
the auth middleware's ``auth.test()`` goes back through the proxy and hangs.

Fix: make ``load_http_proxy_from_env`` return ``None`` so an unset proxy means
"direct". Explicit per-client proxies (``AsyncWebClient(proxy="http://...")``, or
the adapter's ``_apply_slack_proxy`` with a real URL) set ``client.proxy`` to a
non-empty value and never reach this loader, so a deliberately-configured
SLACK_PROXY still works.

Remove this patch if slack_sdk ever honors NO_PROXY in load_http_proxy_from_env.
"""

import importlib.util
import os
from pathlib import Path


def _find_module_path(module_name: str) -> Path:
    spec = importlib.util.find_spec(module_name)
    if spec is None or not spec.origin:
        raise FileNotFoundError(f"Cannot locate module: {module_name}")
    return Path(spec.origin)


loader_path = _find_module_path("slack_sdk.proxy_env_variable_loader")
print(f"Patching {loader_path}")


def _patch(path: Path, old: str, new: str, description: str) -> None:
    src = path.read_text()
    count = src.count(old)
    if count == 0:
        raise RuntimeError(
            f"Patch marker not found in {path}\n"
            f"  description : {description}\n"
            f"  looking for : {old!r}"
        )
    if count > 1:
        raise RuntimeError(
            f"Patch marker is ambiguous in {path} ({count} matches)\n"
            f"  description : {description}\n"
            f"  looking for : {old!r}"
        )
    path.write_text(src.replace(old, new, 1))
    print(f"  ok  {description}")


_patch(
    loader_path,
    old=(
        "def load_http_proxy_from_env(logger: logging.Logger = _default_logger) -> Optional[str]:\n"
        "    proxy_url = (\n"
    ),
    new=(
        "def load_http_proxy_from_env(logger: logging.Logger = _default_logger) -> Optional[str]:\n"
        "    # vicegerent: Slack must bypass the GET-only MITM egress proxy (it scrubs\n"
        "    # xox* tokens and blocks POST/WebSocket). Auto-loading HTTPS_PROXY here\n"
        "    # ignores NO_PROXY and forces every Slack client back through the proxy.\n"
        "    # Return None so an unset proxy means direct; explicit per-client proxies\n"
        "    # (proxy=\"http://...\") set client.proxy directly and never reach this loader.\n"
        "    return None\n"
        "    proxy_url = (\n"
    ),
    description="proxy_env_variable_loader.py: disable env proxy auto-detection",
)


# ---------------------------------------------------------------------------
# Smoke-test: the loader returns None even with HTTPS_PROXY set, and a Slack
# client built with proxy=None no longer picks the proxy back up.
# ---------------------------------------------------------------------------

print("Smoke-testing patched module...")

# 1. The loader function itself returns None even with HTTPS_PROXY set. Load the
#    patched file in isolation (this process already imported slack_sdk while
#    locating the module, which binds the pre-patch loader by name).
os.environ["HTTPS_PROXY"] = "http://smoke-test-proxy:8080"
spec = importlib.util.spec_from_file_location("_patched_slack_proxy_loader", loader_path)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
assert mod.load_http_proxy_from_env() is None, "loader should return None after patch"
os.environ.pop("HTTPS_PROXY", None)
print("  ok  load_http_proxy_from_env() -> None with HTTPS_PROXY set")

# 2. Real client behavior, in a FRESH interpreter — only a new process re-imports
#    the patched file (binding the patched loader by name), which matches how the
#    container runs the bot. Verifying in-process here would give a false result.
import subprocess
import sys

check = (
    "import os; os.environ['HTTPS_PROXY'] = 'http://smoke-test-proxy:8080';"
    "from slack_sdk.web.async_client import AsyncWebClient;"
    "c = AsyncWebClient(token='xoxb-smoke', proxy=None);"
    "assert c.proxy is None, 'proxy not bypassed: %r' % (c.proxy,);"
    "e = AsyncWebClient(token='xoxb-smoke', proxy='http://explicit:8080');"
    "assert e.proxy == 'http://explicit:8080', 'explicit proxy lost: %r' % (e.proxy,);"
    "print('subprocess-ok')"
)
result = subprocess.run([sys.executable, "-c", check], capture_output=True, text=True)
if result.returncode != 0 or "subprocess-ok" not in result.stdout:
    raise RuntimeError(
        "Slack proxy bypass not effective at runtime:\n" + result.stdout + result.stderr
    )
print("  ok  AsyncWebClient(proxy=None) bypasses proxy; explicit proxy still honored")

print("Patch 0007 applied and verified.")
