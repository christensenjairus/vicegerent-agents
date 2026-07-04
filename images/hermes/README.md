# hermes-agent (vicegerent derived image)

A thin derivation of the upstream [`nousresearch/hermes-agent`](https://hub.docker.com/r/nousresearch/hermes-agent)
image, published to `harbor.hahomelabs.com/vicegerent/hermes-agent`. The sandbox
is egress-locked and cannot reach Docker Hub, npm, or PyPI at runtime
(`HERMES_DISABLE_LAZY_INSTALLS=1` is baked into the upstream image), so anything
the agent needs must be present in the image. This is the base every bake builds
on.

## Why a derived image

The stock image ships the Hermes runtime but **not** the pieces this platform
relies on. Verified against the upstream arm64 image (`v2026.7.1`):

| Needed | In stock image? |
| --- | --- |
| mnemosyne plugin + MiniCPM embedding gguf | no |
| hermes-lcm context engine | no |
| LSP servers (pyright, yaml-language-server, terraform-ls, bash-language-server) | no |
| `ddgs` Python package (web search backend) | no — only the plugin glue is present |
| netdebug tools (ss, dig, nc) for egress diagnostics | no |

## Build & push

Built on a machine with internet (your laptop), then pushed to Harbor. The
egress-locked cluster only ever pulls.

```sh
docker login harbor.hahomelabs.com
make image PLATFORM=linux/arm64      # Kind on Apple Silicon
make push
# or: make release PLATFORM=linux/arm64 TAG=v2026.7.1
```

`make help` lists targets. `TAG` defaults to the upstream version; bump it when
the bakes change what the image contains.

## Base pin

`FROM` is pinned by **tag + digest**. The tag keeps the reference
Renovate-trackable (an upstream bump opens an MR); the digest makes the build
reproducible. The `apps/personal/agents/hermes/sandbox.yaml` `Sandbox` is
repointed at this Harbor image, tracked by the `custom.regex` Renovate manager.

## Bakes

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
- netdebug tools (`iproute2`/`ss`, `dnsutils`/`dig`, `netcat-openbsd`/`nc`) via
  apt, for diagnosing egress / CiliumNetworkPolicy hangs from inside the sandbox.
  A default-deny policy DROPS a blocked outbound connect, so it sits in SYN-SENT
  until timeout — `ss -tanp state syn-sent` names the stuck dest + PID with no
  added capabilities. Deliberately excludes `strace`/`tcpdump`: those need
  `CAP_SYS_PTRACE`/`CAP_NET_RAW`, which the locked-down securityContext strips,
  so they would bake fine but fail to attach at runtime.
- `yq` + `jq` + `pygount`; `rtk-hermes` plugin.

## Patches

Upstream Hermes is also customized at build time by numbered Python scripts in
`patches/`, each `COPY`d in, run against `/opt/hermes/.venv`, then deleted. They edit
installed package files in place and self-verify where feasible; remove one once the
fix lands upstream. (Numbering is sparse — 0002/0003/0010 were dropped.)

- `0001-toolsearch-context-length.py` — resolve Tool Search context length offline so
  it never dials `openrouter.ai` at startup behind the egress lockdown.
- `0004-agentburn.py` — `HERMES_HOME` support for the agentburn adapter and missing
  Anthropic/OpenAI model prices.
- `0006-slack-command-name.py` — make the catch-all Slack slash command configurable
  via `HERMES_SLACK_COMMAND_NAME` (default `/hermes`).
- `0007-slack-bypass-egress-proxy.py` — patch `slack_sdk`'s env proxy loader to return
  `None` so Slack bypasses the GET-only egress MITM proxy (`slack_sdk` ignores
  `NO_PROXY`, which would otherwise force every Slack call through the proxy and fail).
- `0008-approval-tirith-only-mode.py` — add `approvals.pattern_silence` to smart-mode
  command approval so operator-configured false-positive patterns skip the aux-LLM
  pre-screen (tirith findings and uncancellable patterns are never silenced).
- `0009-mcp-circuit-breaker-business-errors.py` — stop the MCP circuit breaker from
  tripping on business errors (`isError: true` relayed as a JSON `"error"` key); only
  real transport/auth exceptions should count toward the 3-strike "server unreachable"
  block. Remove once upstream lands hermes-agent #47918/#47955 (issues #47851/#11113).
- `0011-web-extract-capability-check.py` — gate `web_extract` on an
  extract-capable backend (firecrawl/tavily/exa/parallel) rather than the
  shared "any web backend configured" check. We run SearXNG-only
  (`web.search_backend: searxng`); SearXNG passes the shared check but can't
  extract, so `web_extract` was registering and erroring at call time
  instead of simply not existing. Remove once upstream gates `web_extract`
  on `supports_extract()` itself (loosely tracked with hermes-agent #19198).
- `0012-execute-code-pattern-silence.py` — let `approvals.pattern_silence`
  also silence `execute_code`'s separate whole-script approval gate
  (`check_execute_code_guard`), which previously ignored the silence list
  entirely (only `check_all_command_guards`, the regex-flagged `terminal()`
  path, consulted it). Arbitrary script execution is this sandbox's intended
  capability — the isolation boundary is the pod/network layer (no Docker
  socket, egress-locked, non-root), not this gate. Silenced via
  `execute_code` in `approval-policy.yaml`'s `pattern_silence` list. Remove
  once upstream lets a single silence list cover both approval paths.
