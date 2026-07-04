#!/usr/bin/env python3
"""Probe running vicegerent MCP backends and emit their tool/argument catalog.

Backends are discovered from `thv vmcp init --group <group> --output -` (the raw
per-workload endpoints, before ToolHive's vMCP collapses them behind
find_tool/call_tool), so each server's full tools/list catalog is enumerated
directly with its input-schema argument names.

Two output formats:
  csv   flat rows (server,tool,tool_description,argument,type,required) — grep/sheets
  yaml  nested server -> tool -> argument tree — the committed available-tools.yaml
        reference (regenerate with: scripts/probe-mcp-tools.py --format yaml
        -o host/mcp/available-tools.yaml). Useful for spotting a missing tool and
        for seeing which arguments a cerbos policy can key on.

Usage:
  scripts/probe-mcp-tools.py                             # all backends -> CSV on stdout
  scripts/probe-mcp-tools.py gitlab                      # one backend by name
  scripts/probe-mcp-tools.py gitlab linear               # several backends
  scripts/probe-mcp-tools.py --format yaml -o tools.yaml # regenerate the YAML reference
  scripts/probe-mcp-tools.py --list                      # just list backends+URLs

Env / flags:
  --group NAME    ToolHive group to init (default: vicegerent)
  --format FMT    csv (default) or yaml
  -o, --output    output path (default: stdout)
  --list          print discovered backends and exit

CSV columns: server,tool,tool_description,argument,type,required
A tool with no arguments still emits one row (empty argument).
"""
import argparse
import csv
import json
import re
import subprocess
import sys
import urllib.request

INIT = {
    "jsonrpc": "2.0", "id": 1, "method": "initialize",
    "params": {
        "protocolVersion": "2024-11-05", "capabilities": {},
        "clientInfo": {"name": "probe-mcp-tools", "version": "1"},
    },
}
INITIALIZED = {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}
LIST = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}

BACKEND_RE = re.compile(
    r"-\s+name:\s*(?P<name>\S+)\s*\n"
    r"\s*url:\s*(?P<url>\S+)\s*\n"
    r"\s*transport:\s*(?P<transport>\S+)"
)


def discover_backends(group):
    """Return [(name, url), ...] from `thv vmcp init`."""
    out = subprocess.run(
        ["thv", "vmcp", "init", "--group", group, "--output", "-"],
        capture_output=True, text=True,
    )
    if out.returncode != 0:
        sys.exit(f"thv vmcp init failed:\n{out.stderr.strip()}")
    return [(m["name"], m["url"]) for m in
            (m.groupdict() for m in BACKEND_RE.finditer(out.stdout))]


def _parse_body(raw):
    """MCP streamable-http replies are SSE; pull the JSON out of data: lines."""
    data = [ln[5:].strip() for ln in raw.splitlines() if ln.startswith("data:")]
    return json.loads(data[0] if data else raw)


def _post(url, payload, session=None):
    """POST one JSON-RPC message. Returns (parsed_body_or_None, response_headers)."""
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if session:
        headers["Mcp-Session-Id"] = session
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), method="POST", headers=headers)
    with urllib.request.urlopen(req, timeout=20) as r:
        body = r.read().decode()
        hdrs = dict(r.headers)
    return (_parse_body(body) if body.strip() else None), hdrs


def list_tools(url):
    """MCP handshake against one backend; return the tools/list array."""
    _, hdrs = _post(url, INIT)
    session = hdrs.get("Mcp-Session-Id") or hdrs.get("mcp-session-id")
    try:
        _post(url, INITIALIZED, session)  # best-effort; some servers require it
    except Exception:
        pass
    body, _ = _post(url, LIST, session)
    return (body or {}).get("result", {}).get("tools", [])


def tool_rows(server, tools):
    """Flatten tools -> CSV rows (server, tool, tool_desc, arg, type, required)."""
    for t in tools:
        name = t.get("name", "")
        desc = " ".join((t.get("description") or "").split())
        schema = t.get("inputSchema") or {}
        props = schema.get("properties") or {}
        required = set(schema.get("required") or [])
        if not props:
            yield [server, name, desc, "", "", ""]
            continue
        for arg, spec in props.items():
            atype = spec.get("type", "")
            if not atype and "anyOf" in spec:
                atype = "|".join(o.get("type", "?") for o in spec["anyOf"])
            yield [server, name, desc, arg, atype, "yes" if arg in required else "no"]


def tool_entry(t):
    """Shape one tool's schema into the nested arguments mapping used by YAML."""
    schema = t.get("inputSchema") or {}
    props = schema.get("properties") or {}
    required = set(schema.get("required") or [])
    arguments = {}
    for arg, spec in props.items():
        atype = spec.get("type", "")
        if not atype and "anyOf" in spec:
            atype = "|".join(o.get("type", "?") for o in spec["anyOf"])
        entry = {"type": atype, "required": arg in required}
        adesc = " ".join((spec.get("description") or "").split())
        if adesc:
            entry["description"] = adesc
        if "enum" in spec:
            entry["enum"] = spec["enum"]
        arguments[arg] = entry
    return {
        "description": " ".join((t.get("description") or "").split()),
        "arguments": arguments,
    }


_BARE_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*$")


def _yaml_key(k):
    return k if _BARE_KEY.match(k) else json.dumps(k)


def _yaml_scalar(v):
    # JSON is a subset of YAML 1.2, so json.dumps yields a valid, safely-escaped scalar.
    if isinstance(v, bool):
        return "true" if v else "false"
    if v is None:
        return "null"
    if isinstance(v, (int, float, list)):
        return json.dumps(v)
    return json.dumps(str(v))


def emit_yaml(obj, indent=0):
    """Deterministic YAML for dict/list/scalar trees (no PyYAML dependency)."""
    pad = "  " * indent
    if isinstance(obj, dict):
        if not obj:
            return " {}\n"
        out = "\n" if indent else ""
        for k, v in obj.items():
            key = f"{pad}{_yaml_key(k)}:"
            if isinstance(v, dict) and v:
                out += key + emit_yaml(v, indent + 1)
            elif isinstance(v, dict):
                out += key + " {}\n"
            else:
                out += f"{key} {_yaml_scalar(v)}\n"
        return out
    return f" {_yaml_scalar(obj)}\n"


def write_yaml(out, group, results):
    """results: list of (server, tools-list). Emit the available-tools.yaml tree."""
    total_tools = sum(len(t) for _, t in results)
    out.write("# available-tools.yaml — catalog of every tool exposed by the running\n")
    out.write("# vicegerent MCP backends, with each tool's arguments (type, required,\n")
    out.write("# description, enum). Reference for spotting missing tools and for writing\n")
    out.write("# cerbos argument-level policies. GENERATED — do not edit by hand.\n")
    out.write("# Regenerate: scripts/probe-mcp-tools.py --format yaml -o host/mcp/available-tools.yaml\n")
    out.write(f"group: {group}\n")
    out.write(f"tool_count: {total_tools}\n")
    tree = {}
    for server, tools in results:
        tree[server] = {
            "tool_count": len(tools),
            "tools": {t.get("name", ""): tool_entry(t) for t in tools},
        }
    out.write("servers:" + emit_yaml(tree, 1))


def main():
    ap = argparse.ArgumentParser(description="Probe vicegerent MCP backends.")
    ap.add_argument("servers", nargs="*", help="backend name(s) to probe (default: all)")
    ap.add_argument("--group", default="vicegerent", help="ToolHive group (default: vicegerent)")
    ap.add_argument("--format", choices=("csv", "yaml"), default="csv", help="output format (default: csv)")
    ap.add_argument("-o", "--output", help="output path (default: stdout)")
    ap.add_argument("--list", action="store_true", dest="list_only",
                    help="list discovered backends and exit")
    args = ap.parse_args()

    backends = discover_backends(args.group)
    if not backends:
        sys.exit(f"No backends found in group '{args.group}'. Is the ToolHive stack running?")

    if args.servers:
        want = set(args.servers)
        known = {n for n, _ in backends}
        missing = want - known
        if missing:
            sys.exit(f"Unknown backend(s): {', '.join(sorted(missing))}. "
                     f"Available: {', '.join(sorted(known))}")
        backends = [(n, u) for n, u in backends if n in want]

    if args.list_only:
        for n, u in backends:
            print(f"{n}\t{u}")
        return

    results = []
    for name, url in backends:
        try:
            tools = list_tools(url)
        except Exception as e:
            print(f"# {name}: probe failed ({e})", file=sys.stderr)
            continue
        if not tools:
            print(f"# {name}: 0 tools returned", file=sys.stderr)
            continue
        print(f"# {name}: {len(tools)} tools", file=sys.stderr)
        results.append((name, tools))

    out = open(args.output, "w", newline="") if args.output else sys.stdout
    try:
        if args.format == "yaml":
            write_yaml(out, args.group, results)
        else:
            w = csv.writer(out)
            w.writerow(["server", "tool", "tool_description", "argument", "type", "required"])
            for name, tools in results:
                for row in tool_rows(name, tools):
                    w.writerow(row)
    finally:
        if out is not sys.stdout:
            out.close()


if __name__ == "__main__":
    main()
