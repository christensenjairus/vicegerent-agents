# egress-gitleaks-sidecar

A tiny localhost-only HTTP service that runs [gitleaks](https://github.com/gitleaks/gitleaks)'
embedded default ruleset (~180 rules) over arbitrary strings, on behalf of the
egress-proxy's mitmproxy `scrub.py` addon. It is the **second** secret-redaction
layer for sandbox egress traffic; `scrub.py`'s own hand-rolled regex registry is
the first.

## Why a sidecar

gitleaks is a Go library with no Python binding, and mitmproxy's addon is Python.
The alternatives are worse:

- **Shell out to the gitleaks CLI per request** — subprocess + temp-file overhead
  on every request/response, and the CLI is built around scanning files/git repos,
  not stdin strings.
- **Reimplement ~180 rules in Python** — a second copy of the ruleset to keep in
  sync forever.

Running the exact same `detect.Detector` the `mcp-cerbos-shim` already proved out
(same v8.20.1 pin, same build-once/share pattern, same `Finding.Secret`→`Finding.Match`
substring-replace logic), as a second container in the **same Pod** as mitmproxy,
avoids both. `scrub.py` POSTs each string it wants scanned to `127.0.0.1` and gets
back the gitleaks-redacted text plus a count.

This gives request **and** response egress traffic the same two-layer coverage the
shim gives MCP tool calls — see
`charts/egress-proxy/templates/addon-configmap.yaml` (Python regex registry + this
sidecar) and `images/mcp-cerbos-shim/internal/server/secrets_redact.go` (Go regex
registry + gitleaks in-process). gitleaks lives here, the regex registry lives in
Python; neither duplicates the other.

## API

Loopback only. Binds `127.0.0.1:8081` by default (`LISTEN_ADDR` overrides — but the
deployment relies on the loopback bind for its no-external-reachability property).

| Method + path | Body | Response |
| --- | --- | --- |
| `POST /redact` | `{"text": "..."}` | `{"text": "<redacted>", "count": <int>}` |
| `GET /healthz` | — | `200 ok` (kubelet liveness/readiness) |

`count` is the number of substrings replaced with `<masked>` (the same placeholder
`scrub.py` uses, so the two layers never re-redact each other's output).

## Reachability

Containers in a Pod share a network namespace, so the mitmproxy container reaches this
over loopback and nothing outside the Pod can. Intra-pod loopback traffic is **not**
subject to Cilium/NetworkPolicy enforcement (policy applies to traffic crossing the pod
boundary), so no `CiliumNetworkPolicy` rule is needed or added for port 8081 — see
`charts/egress-proxy/templates/networkpolicy.yaml`.

## Failure posture

If gitleaks' detector fails to build at startup, `/redact` degrades to a no-op (returns
the text unchanged, `count: 0`) and logs it — `scrub.py`'s regex registry still runs, so
coverage weakens but egress traffic never breaks. `scrub.py` itself calls `/redact` with
a short timeout and **fails open** (falls through to regex-only redaction) if this sidecar
is slow or unreachable, since it is on the hot path for every external request the sandbox
makes.

## Build

A `Makefile` wraps the common tasks (`make help` lists them). The image name is fixed to
`harbor.hahomelabs.com/vicegerent/egress-gitleaks-sidecar`; override the tag with `TAG=`.

```bash
make check                 # gofmt-check + go vet + go test ./... (CI parity)
make image TAG=v0.1.0      # docker build
make push  TAG=v0.1.0      # docker push to Harbor
make release TAG=v0.1.0    # check + image + push
```

Like `mcp-cerbos-shim`, this image is built and pushed to Harbor out-of-band via this
Makefile — there is no in-repo CI job that builds/pushes it (`.gitlab-ci.yml` only runs
`go test` on `images/**`). Renovate tracks the deployed tag via the `# renovate:` annotation
on the container's `image:` in `charts/egress-proxy/templates/deployment.yaml`.

The `go.mod` floors `go 1.22.0` and pins `gitleaks/v8` to `v8.20.1` — the same versions as
`mcp-cerbos-shim`. gitleaks past v8.20.1 raises its Go floor above 1.22, so do not bump it
without bumping the toolchain in lock-step with the shim.
