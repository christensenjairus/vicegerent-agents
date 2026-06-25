#!/usr/bin/env python3
"""Validate rendered agentgateway custom resources against the pinned
agentgateway CRD openAPIV3Schema.

Why this exists: kubeconform runs with -ignore-missing-schemas, so
AgentgatewayPolicy/AgentgatewayBackend resources are otherwise NOT validated.
A malformed `backend.mcp.guardrails` block silently fails to load, leaving only
the tool-name allowlist active and secret reads ALLOWED. This gate fails closed:
if a target resource's (group, version, kind) has no matching CRD schema, that
is an ERROR (not a skip), so a structural drift can't slip through unvalidated.

Limits (documented, not silent): JSON-schema cannot evaluate the CRD's
x-kubernetes-validations CEL rules, and the upstream schema does not set
additionalProperties:false, so a misplaced-but-type-valid extra key is not
caught here — the apiserver's CEL is the backstop. This catches the high-value
cases: bad enums (e.g. lowercase phase), wrong types, and missing required
fields such as remote.backendRef.

Usage: validate-agentgateway-crds.py <crd-glob> <rendered.yaml> [<rendered.yaml> ...]
"""
import sys
import glob
import yaml

try:
    import jsonschema
except ImportError:
    print("ERROR - python 'jsonschema' not installed", file=sys.stderr)
    sys.exit(2)


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__, file=sys.stderr)
        return 2
    crd_glob = sys.argv[1]
    targets = sys.argv[2:]

    schemas = {}
    for f in glob.glob(crd_glob):
        with open(f) as fh:
            for doc in yaml.safe_load_all(fh):
                if not doc or doc.get("kind") != "CustomResourceDefinition":
                    continue
                group = doc["spec"]["group"]
                kind = doc["spec"]["names"]["kind"]
                for v in doc["spec"]["versions"]:
                    schemas[(group, v["name"], kind)] = v["schema"]["openAPIV3Schema"]

    if not schemas:
        print(f"ERROR - no CRD schemas loaded from {crd_glob}", file=sys.stderr)
        return 3
    print(f"INFO - loaded {len(schemas)} CRD version schema(s)")

    errors = 0
    checked = 0
    for tf in targets:
        with open(tf) as fh:
            for doc in yaml.safe_load_all(fh):
                if not doc:
                    continue
                av = doc.get("apiVersion", "")
                kind = doc.get("kind", "")
                if "/" not in av:
                    continue
                group, version = av.split("/", 1)
                key = (group, version, kind)
                if key not in schemas:
                    continue  # not an agentgateway CRD; other tooling validates it
                checked += 1
                try:
                    jsonschema.validate(doc, schemas[key])
                    print(f"PASS - {tf}: {kind} {av}")
                except jsonschema.ValidationError as e:
                    errors += 1
                    loc = "/".join(str(p) for p in e.absolute_path) or "<root>"
                    print(f"FAIL - {tf}: {kind} {av} at {loc}: {e.message}", file=sys.stderr)

    if checked == 0:
        print("ERROR - no agentgateway CRD resources matched; schema wiring broken", file=sys.stderr)
        return 3
    print(f"INFO - validated {checked} agentgateway CRD resource(s), {errors} error(s)")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
