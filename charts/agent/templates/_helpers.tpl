{{- define "vicegerent-agent.name" -}}
{{- .Values.name | default .Release.Name -}}
{{- end -}}

{{- /* Shared coding-agent instruction: web_search/WebSearch/WebFetch are disabled
      in codex and claude-code because they're server-side and bypass the sealed
      egress proxy. Single source of truth for codex's developer_instructions and
      claude-code's seeded CLAUDE.md — keep them in sync by editing only here. */ -}}
{{- define "vicegerent-agent.webSearchInstructions" -}}
WebSearch/web_search and WebFetch are disabled — both are server-side tools that bypass the sealed egress proxy. For web search, curl $SEARXNG_URL/search?q=<query>&format=json instead.
{{- end -}}

{{- define "vicegerent-agent.environment" -}}
# Environment
You run inside a sealed agent sandbox: a non-root container on a
locked-down Kubernetes cluster, managed by GitOps (Flux). The platform is
defined in the `vicegerent-agents` repo
(gitlab.hahomelabs.com/jchristensen/vicegerent-agents) — that repo is where
your own capabilities, models, tools, and limits are configured.

## Limitations to expect
- **Egress is sealed.** Most direct outbound TCP is dropped — no raw HTTP/HTTPS,
  no package managers, no direct API calls. Approved channels only:
  - **`web_search`** — internet lookups via the in-cluster SearXNG proxy.
  - **MCP servers** — all external integrations (GitLab, Kubernetes, Notion, web scraping, etc.).
  - **agentgateway** — all model API calls; don't call providers directly.
  - **`git` over SSH (port 22)** — the only approved direct TCP outside the cluster.
  If none cover your need, tell the user what to add.
- **No cluster credentials by default.** Your service-account token is not
  mounted; you cannot read Secrets or mutate the cluster unless a specific
  capability was granted to you in the repo.
- **Tools are an allowlist.** MCP servers and their callable tools are
  explicitly enumerated. If a tool you need isn't present, it wasn't wired
  up — not a transient error.
- **The filesystem is mostly ephemeral.** Only the mounted data and
  workspace volumes persist; everything else resets when the pod restarts.
  Clone repos and keep git worktrees under `/workspace` — it's the
  persistent volume for git repos and survives pod restarts; anywhere
  else is wiped.

## When you hit a wall
If a task is blocked by the sandbox itself — a missing tool, a sealed
endpoint you legitimately need, absent credentials, or a denied action —
**say so plainly and tell the user what access would unblock you.** Name the
specific capability (e.g. "I need the `foo` MCP tool added" or "the gateway
needs a route to bar.example.com"). The user can change the repo to grant
it. Don't silently fail, fabricate a result, or burn turns retrying
something the environment structurally prevents. Surfacing the gap is the
correct, expected move — the human is your path to expanding what you can do.

# Coding agents
Use `claude-code` or `codex` for medium/large tasks and all code reviews — don't inline large coding work.
Pick the model that fits the task: heavier reasoning for complex/design work, lighter/faster for quick fixes and alternatives.

# Memory
- Mnemosyne is the only memory store. Use it for all facts, preferences, and insights
- **Repo knowledge**: also add a terse bullet to `AGENTS.md` in your next PR.
{{- if .Values.obsidian.vaultPath }}

# Obsidian vault
- `OBSIDIAN_VAULT_PATH` is set to `{{ .Values.obsidian.vaultPath }}` — a git-synced Obsidian vault.
  It is the durable, version-controlled knowledge base: prefer it over ad-hoc notes for anything
  meant to survive indefinitely and be reviewed/edited by a human.
- Treat the vault as an [OKF](https://github.com/GoogleCloudPlatform/knowledge-catalog/blob/main/okf/SPEC.md)
  bundle unless the user's existing vault says otherwise: one concept per markdown file, YAML
  frontmatter (`type` required), `index.md` per directory for progressive disclosure, bundle-relative
  links (`[text](/topic/concept.md)`), a `log.md` changelog.
- Use the `obsidian` skill for reads/writes/search. Don't `git commit`/`git push` after every single
  edit — batch them. Commit and push at the end of a work session, or at least once a day if the
  session runs long, using the already-configured SSH key. The vault directory persists across pod
  restarts either way; committing regularly is about off-site durability and human visibility, not
  preventing data loss.
- Keep skills (`$HERMES_HOME/skills/`) and the vault as separate systems — don't move or symlink
  skills into the vault. Skills carry their own curator lifecycle (usage telemetry, staleness,
  archiving) that assumes a skill-shaped directory, not an OKF bundle; cross-reference a skill by
  name from a vault concept instead of merging the two.
- Mnemosyne stays the fast-recall layer: store short pointers to vault concept paths there, not
  copies of vault content, so the two stores don't drift out of sync.
{{- end }}

# Expectations
- Use a fresh, updated git worktree for every session, regardless of the repo.
- Be thorough in your debugging. Find a smoking gun before suggesting a fix.
- Never guess or assume, always back up statements with data.
- You are designed to be AUTONOMOUS. Run issues to completion or until you get stuck.
- When MCP servers misbehave, stop execution and tell the user.
{{- end -}}

{{- /* Locked provider connectivity every agent must have; wins on merge conflicts. */ -}}
{{- define "vicegerent-agent.mandatoryConfig" -}}
providers:
  anthropic:
    name: Agentgateway-Anthropic
    api: http://agentgateway-proxy.agentgateway-system.svc.cluster.local/anthropic
    key_env: ANTHROPIC_API_KEY
    transport: anthropic_messages
  openai:
    name: Agentgateway-OpenAI
    api: http://agentgateway-proxy.agentgateway-system.svc.cluster.local/openai
    key_env: OPENAI_API_KEY
    transport: responses
{{- end -}}

{{- /* Platform-wide operational defaults; overridable per-agent via values.config. */ -}}
{{- define "vicegerent-agent.defaultConfig" -}}
model:
  default: claude-sonnet-5
  context_length: 1000000
compression:
  threshold: 0.50
prompt_caching:
  cache_ttl: 1h
mcp_servers:
  agentburn:
    command: /opt/hermes/.venv/bin/agentburn
    args: [mcp]
    env:
      HERMES_HOME: /opt/data
  vmcp:
    url: http://agentgateway-proxy.agentgateway-system.svc.cluster.local/mcp/vmcp
    timeout: 30
    connect_timeout: 5
agent:
  max_turns: 150
  gateway_timeout: 900
  reasoning_effort: medium
  disabled_toolsets:
    - computer_use
    - tts
    - browser
    - image_gen
platform_toolsets:
  slack:
    - hermes-slack
    - kanban
    - moa
kanban:
  dispatch_in_gateway: false
delegation:
  provider: anthropic
  model: claude-sonnet-5
  orchestrator_enabled: true
  max_spawn_depth: 2
auxiliary:
  vision:
    provider: anthropic
    model: claude-haiku-4-5
  title_generation:
    provider: anthropic
    model: claude-haiku-4-5
  approval:
    provider: anthropic
    model: claude-haiku-4-5
  compression:
    provider: anthropic
    model: claude-haiku-4-5
    context_length: 1000000
  web_extract:
    provider: anthropic
    model: claude-haiku-4-5
  triage_specifier:
    provider: anthropic
    model: claude-haiku-4-5
  kanban_decomposer:
    provider: anthropic
    model: claude-haiku-4-5
  curator:
    provider: anthropic
    model: claude-haiku-4-5
  monitor:
    provider: anthropic
    model: claude-haiku-4-5
tool_loop_guardrails:
  hard_stop_enabled: true
  hard_stop_after:
    exact_failure: 8
display:
  skin: slate
  streaming: true
  show_cost: true
  timestamps: true
  tool_progress: verbose
  platforms:
    slack:
      tool_progress: false
approvals:
  mode: smart
command_allowlist: []
checkpoints:
  enabled: true
clarify:
  timeout: 300
timezone: America/Denver
terminal:
  cwd: /workspace
  persistent_shell: true
web:
  search_backend: searxng
memory:
  provider: mnemosyne
  memory_enabled: false
  user_profile_enabled: false
  mnemosyne:
    auto_sleep: true
context:
  engine: lcm
lsp:
  enabled: true
  install_strategy: manual
slack:
  require_mention: true
  strict_mention: true
plugins:
  enabled:
    - disk-cleanup
    - rtk-rewrite
    - security-guidance
  disabled:
    - google_meet
    - spotify
    - teams_pipeline
    - raft-platform
    - web/exa
    - web/parallel
    - web/brave_free
    - web/tavily
    - web/firecrawl
    - web/ddgs
    - web/xai
    - image_gen/fal
    - image_gen/krea
    - image_gen/openai
    - image_gen/openai-codex
    - image_gen/xai
    - video_gen/fal
    - video_gen/xai
    - browser/browser_use
    - browser/browserbase
    - browser/firecrawl
{{- end -}}

{{- define "vicegerent-agent.renderedConfig" -}}
{{- $agentConfig := .Values.config | fromYaml -}}
{{- if not (kindIs "map" $agentConfig) -}}
{{- fail (printf "vicegerent-agent: .Values.config did not parse to a YAML map (got %s) — check for invalid YAML or a bare scalar/list in values.config" (kindOf $agentConfig)) -}}
{{- end -}}
{{- if hasKey $agentConfig "Error" -}}
{{- fail (printf "vicegerent-agent: .Values.config failed to parse as YAML: %v" (get $agentConfig "Error")) -}}
{{- end -}}
{{- $default := include "vicegerent-agent.defaultConfig" . | fromYaml -}}
{{- $agentConfig = mergeOverwrite $default $agentConfig -}}
{{- $mandatory := include "vicegerent-agent.mandatoryConfig" . | fromYaml -}}
{{- $merged := mergeOverwrite $agentConfig $mandatory -}}
{{- /* model.* is a connection independent of providers.*; re-derive it too so it can't diverge. */ -}}
{{- $activeProviderName := "anthropic" -}}
{{- if (kindIs "map" $merged.model) -}}
{{- if $merged.model.provider -}}
{{- $activeProviderName = $merged.model.provider -}}
{{- end -}}
{{- end -}}
{{- if not (hasKey $merged.providers $activeProviderName) -}}
{{- fail (printf "vicegerent-agent: model.provider %q has no matching entry in providers — must be one of the mandatory providers (anthropic, openai) or an agent-defined entry under providers.*" $activeProviderName) -}}
{{- end -}}
{{- $activeProvider := index $merged.providers $activeProviderName -}}
{{- $lockedModel := dict "provider" $activeProviderName "base_url" $activeProvider.api "key_env" $activeProvider.key_env "api_mode" $activeProvider.transport -}}
{{- $merged = mergeOverwrite $merged (dict "model" $lockedModel) -}}
{{- $merged | toYaml -}}
{{- end -}}
