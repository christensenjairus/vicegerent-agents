# Vendored: mcp-proxy-server

Upstream: https://github.com/ptbsare/mcp-proxy-server
Version: v0.4.1 (commit `9fe05ed478cd1b80ca75b0b625e0e259dd9d1a2a`)
License: MIT

## What was removed from upstream

Home Assistant addon files not needed here:
- `rootfs/`, `build.yaml`, `config.yaml`, `nginx.conf`, `icon.png`, `logo.png`, `Dockerfile`, `README_ZH.md`

## Patches applied at runtime

Two idempotent patches are applied by `vicegerent-mcp start` (see `host/mcp/vicegerent_mcp.py`):

1. **Loopback bind** — constrains the HTTP listener to `127.0.0.1` only (security)
2. **`notifications/tools/list_changed`** — emits MCP list-changed notifications after
   admin reload so agentgateway and Hermes pick up tool changes live

These patches modify `src/sse.ts` and trigger a rebuild if the build is stale.

## Updating to a new upstream version

```bash
# From repo root
git -C host/mcp-proxy-server fetch --tags
git -C host/mcp-proxy-server checkout vX.Y.Z
# Remove HA-addon files if re-added upstream
# Then rebuild
npm --prefix host/mcp-proxy-server ci
npm --prefix host/mcp-proxy-server run build
# Or just run setup-host-mcp --rebuild
```

Update the version line at the top of this file after upgrading.
