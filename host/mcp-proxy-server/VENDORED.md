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

This directory is vendored as plain committed files (not a git submodule), so
updating means copying upstream source in at the target tag:

```bash
# Clone upstream elsewhere at the target tag, then copy its source in:
git clone --branch vX.Y.Z https://github.com/ptbsare/mcp-proxy-server /tmp/mps
rsync -a --delete /tmp/mps/src/ host/mcp-proxy-server/src/
# repeat for public/, package.json, package-lock.json, tsconfig.json as needed
# Remove HA-addon files if re-added upstream (see list above).
npm --prefix host/mcp-proxy-server ci
npm --prefix host/mcp-proxy-server run build   # patches re-apply on next `start`
# Or just run: ./vicegerent mcp setup --rebuild
```

Update the version line at the top of this file after upgrading.
