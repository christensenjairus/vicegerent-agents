# hermes-agent (vicegerent derived image)

A thin derivation of the upstream [`nousresearch/hermes-agent`](https://hub.docker.com/r/nousresearch/hermes-agent)
image, published to `harbor.hahomelabs.com/vicegerent/hermes-agent`. The sandbox
is egress-locked and cannot reach Docker Hub, npm, or PyPI at runtime
(`HERMES_DISABLE_LAZY_INSTALLS=1` is baked into the upstream image), so anything
the agent needs must be present in the image. This is the base every bake builds
on.

## Why a derived image

The stock image ships the Hermes runtime but **not** the pieces this platform
relies on. Verified against the upstream arm64 image (`v2026.6.19`):

| Needed | In stock image? |
| --- | --- |
| mnemosyne plugin + MiniCPM embedding gguf | no |
| hermes-lcm context engine | yes — baked from the pinned GitHub release |
| LSP servers (pyright, yaml-language-server, terraform-ls, bash-language-server) | no |
| `ddgs` Python package (web search backend) | no — only the plugin glue is present |

Each of those is baked in its own follow-up (see "Bakes" below). This directory
starts with only the pinned base + build tooling so the artifact and its
provenance are reviewable on their own.

## Build & push

Built on a machine with internet (your laptop), then pushed to Harbor. The
egress-locked cluster only ever pulls.

```sh
docker login harbor.hahomelabs.com
make image PLATFORM=linux/arm64      # minikube on Apple Silicon
make push
# or: make release PLATFORM=linux/arm64 TAG=v2026.6.19
```

`make help` lists targets. `TAG` defaults to the upstream version; bump it when
the bakes change what the image contains.

## Base pin

`FROM` is pinned by **tag + digest**. The tag keeps the reference
Renovate-trackable (an upstream bump opens an MR); the digest makes the build
reproducible. The `apps/vicegerent/agents/hermes/sandbox.yaml` `Sandbox` is
repointed at this Harbor image, tracked by the `custom.regex` Renovate manager.

## Bakes (follow-up cards, layered on top)

Tracked separately on the `vicegerent-build` board, each adding its own layer:

- mnemosyne + MiniCPM `MiniCPM5-1B-Q4_K_M.gguf` (bake outside `/opt/data`; the
  data PVC shadows `/opt/data` at runtime, so first-boot seeding is an
  init-container concern, not a Dockerfile one).
- hermes-lcm context engine — extracted from its pinned GitHub release into
  Hermes' bundled `plugins/context_engine/lcm/` (resolved from the installed
  package, not a hardcoded path). Loaded by the dedicated context-engine
  discovery, not the general `~/.hermes/plugins` system, so `hermes plugins
  install` does not place it; selected via `context.engine: lcm` in the agent
  config. Pure stdlib, no PyPI deps.
- LSP servers via `npm -g` (node + npm are in the base).
- `ddgs` via `uv pip install` into `/opt/hermes/.venv`.
- `yq` + `jq` + `pygount`; `rtk-hermes` plugin.
