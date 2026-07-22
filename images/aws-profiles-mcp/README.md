# aws-profiles-mcp

A tiny MCP server that exposes one tool, `list_profiles`, returning the AWS profile names from the mounted read-only `~/.aws/config`. It's a companion to the `aws` backend (awslabs `aws-api-mcp-server`): that server executes AWS CLI commands but cannot enumerate profiles (its validator rejects `aws configure`), so an agent has no way to discover which `--profile <name>` values are valid. This fills that gap.

Read-only, no network (`network.none` in `toolhive-servers.json`), no secrets — it returns profile *names* only, never credentials. It reuses the same `apply: aws_config` read-only `~/.aws` mount as the `aws` backend.

Naming: the vMCP prefixes each backend's tools with its workload name, so this surfaces as `aws-profiles_list_profiles` alongside the `aws` backend's `aws_call_aws` / `aws_suggest_aws_commands`. A literal shared `aws_` prefix would require both to be one ToolHive workload (names are unique per group); the tool description ties it to `call_aws` so the agent treats them as one AWS toolset.

## Build & push

Built on a machine with internet (your laptop), pushed to Harbor; the egress-locked cluster only ever pulls.

```sh
docker login harbor.hahomelabs.com
make image PLATFORM=linux/arm64      # Kind on Apple Silicon
make push
# or: make release PLATFORM=linux/arm64 TAG=v0.1.0
```

`FASTMCP_VERSION` (Dockerfile ARG) is Renovate-tracked (pypi); the image ref in `toolhive-servers.json` is Renovate-tracked (docker).
