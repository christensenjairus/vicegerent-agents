"""Minimal MCP server exposing the AWS profiles available on the mounted
~/.aws/config, so an agent using the `aws` (aws-api-mcp-server) backend can
discover which profiles it may pass to call_aws's `--profile`. That backend has
no way to enumerate profiles (its validator rejects `aws configure`), so this
companion fills the gap. Read-only, no network, no secrets — just profile names.
"""

import configparser
import os

from fastmcp import FastMCP

mcp = FastMCP("aws_profiles")


def _config_path() -> str:
    return os.environ.get("AWS_CONFIG_FILE") or os.path.join(
        os.path.expanduser("~"), ".aws", "config"
    )


def _profiles() -> list[str]:
    """Profile names from ~/.aws/config. Sections are `[default]`,
    `[profile <name>]`, and `[sso-session <name>]`; only default/profile
    sections are real credentials profiles usable with --profile."""
    parser = configparser.RawConfigParser()
    try:
        parser.read(_config_path())
    except (OSError, configparser.Error):
        return []
    names: set[str] = set()
    for section in parser.sections():
        if section == "default":
            names.add("default")
        elif section.startswith("profile "):
            names.add(section[len("profile "):].strip())
    return sorted(names)


@mcp.tool(name="list")
def list_profiles() -> list[str]:
    """List the AWS profiles available to the `aws` backend's call_aws tool.

    Read from the operator's mounted ~/.aws/config. Pass one of these to
    call_aws inside the CLI command as `--profile <name>` (e.g.
    call_aws(cli_command="aws sts get-caller-identity --profile <name>")).
    Returns profile names only — no credentials.
    """
    return _profiles()


if __name__ == "__main__":
    mcp.run()  # stdio transport; ToolHive bridges it to streamable-http
