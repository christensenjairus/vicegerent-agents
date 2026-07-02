# Design rationale

Why this exists, what it's compared against, and the tradeoffs — split out of the top-level README so that stays a quick front door. See the [README](../README.md) for what the platform is and how to stand one up.

## What it provides

Every agent runs as its own `agent-sandbox` pod, non-root and pod-hardened, with its own generated credentials (SSH key, dashboard auth, agentgateway bearer token) that are never shared with another agent. From there:

- The sandbox has no direct internet or cluster access. Cilium egress-locks it to a scrubbing egress proxy, git-over-SSH, Slack, and DNS.
- Every model call and every MCP tool call goes through agentgateway.
- Every shell command passes through a layered approval pipeline (hardline block → operator silence list → tirith static scan → LLM-assessed smart approval) before it executes.
- MCP tool calls also pass a Cerbos guardrail that blocks reading Kubernetes Secrets regardless of which tool asks, so a confidentiality boundary survives even if a tool is otherwise permitted.

Because the containment is structural, agents can run genuinely autonomously — unattended, on schedules, reacting to events — without a human in the loop for every action. The boundary is what makes that autonomy safe to grant. The whole platform (agents, models, gateway routes, secrets policy, approval rules) is Flux-reconciled from this git repo, so standing it up, changing it, or reproducing it on another machine is `git clone` + `flux bootstrap`, not a pile of manual laptop setup steps.

## Why not just run Claude Code / Codex directly on a laptop?

This isn't a replacement for that — running an agent directly on your laptop with a human watching every action is a perfectly good mode, and for a lot of interactive work it's the right one. The problem shows up specifically when you want the agent to run unattended: a CLI agent running as your own user has your full filesystem, your full network, and every credential in your shell environment or keychain, gated only by per-action safety prompts. Those prompts work when a human is actually reading them, but in practice people learn the agent's patterns and start reflexively approving — at which point the prompt is theater, not a control, and a prompt-injected page or a wrong destructive command has the same reach you do. Claude Code on a laptop is great precisely because a human is there; this project exists for the case where one isn't — durable, unattended, full autonomy — and needs the containment to come from the platform instead of from someone paying attention to every prompt. Nothing about the laptop setup is declarative or reviewable either — config lives in whatever state your laptop happens to be in, drifts silently, and isn't reproducible for a second machine or a second person.

## Why not a plain container, or a runtime like NVIDIA OpenShell?

A raw container is an improvement over bare-metal, but usually still runs as root or with a mounted Docker socket (host-root-equivalent), has unrestricted egress, and mounts one set of credentials that every process inside can read. [NVIDIA OpenShell](https://github.com/NVIDIA/OpenShell) is a closer analogue: per-sandbox isolation, declarative YAML network/filesystem/process policy, and credential injection via named providers. But it's explicitly alpha and single-tenant ("single-player mode": one developer, one environment, one gateway), and its policy engine draws one coarse line per sandbox (filesystem/network/process/inference) instead of combining a kernel-level egress lock with argument-level MCP guardrails and a graded command-approval pipeline. That coarser model pushes a real tradeoff onto the operator: without a graded approval pipeline, a sandbox either needs a human reviewing every action, or a policy loose enough to let the agent act on its own. There's no middle layer that lets an agent run unattended while still catching a specific dangerous call. Neither a plain container nor OpenShell gives you Flux-reconciled, git-auditable state across many independently-credentialed agents sharing one cluster.

## Drawbacks of this architecture

The flexibility that makes this useful is also complexity to maintain:
- More moving parts than a laptop CLI — Kind, Cilium, Flux, agentgateway, Cerbos, the host ToolHive stack, and the approval pipeline all need to be understood together when something doesn't behave as expected.
- Adding a new integration takes a few more steps than `pip install` — a ToolHive server entry, its host-side secret or OAuth flow, and possibly a Cerbos rule.
- The host-side MCP bridge depends on the developer's machine being on; it's a deliberate tradeoff (see below) rather than a fully in-cluster design.
- Onboarding a new agent (secrets, sandbox config, gateway routes) takes longer than opening a terminal and running a CLI tool.
- It's infrastructure with normal upkeep — cert rotation, image bumps, keeping Flux reconciled — like any platform, not an install-and-forget script.
- The isolation and review overhead pays off most for agents that run unattended or touch things worth protecting; for quick one-off exploration it's more setup than you need.

## Why not run every tool in Kubernetes?

Every MCP server runs on the developer's machine on purpose (see [`host/mcp`](../host/mcp)) — OAuth-backed tools and anything tied to laptop-local session state (browser OAuth flows, a local kubeconfig, AWS SSO) don't have a clean cluster-side equivalent. Standing up a service identity for these in-cluster means either provisioning bot accounts/service tokens per integration or building an OAuth flow a headless pod can complete on its own, and most organizations haven't standardized bot identities for every tool an agent might want to use. Rather than block on that, this platform runs those tools under the developer's own already-authenticated session — as ToolHive (`thv`) workloads aggregated behind a single Virtual MCP Server (vMCP) — and tunnels that one endpoint into the cluster over mTLS (ghostunnel), so the agent acts through the developer's existing identity instead of waiting for a separate one to be provisioned.

That's a deliberate design choice, not just a stopgap: it makes the agent a genuine extension of the developer — reviewing a Notion doc or hitting the Kubernetes API as *them*, with their actual permissions — rather than a separate identity whose access has to be independently reasoned about, requested, and kept in sync with the developer's own. The tradeoff is real and stated above: the host bridge only works while the developer's machine is up. Once an organization has bot tokens and service identities sorted out for a given tool, it both can and should move that tool fully into the cluster — at that point the developer laptop dependency for that integration disappears entirely, and the whole system stops needing any one person's machine at all. Taken to its conclusion, this platform can run entirely in the cloud, serving many different people's sandboxes identically, with no host-side bridge in the picture.
