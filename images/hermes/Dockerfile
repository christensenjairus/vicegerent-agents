# gopls (Go LSP) is not on npm and the base image has no Go toolchain. Build it
# in a throwaway stage and copy only the ~30MB binary into the final image. The
# version ARG carries the leading "v" to match the Go module pseudo-version
# (Renovate go datasource). GOTOOLCHAIN=auto lets `go` fetch the exact toolchain
# gopls's go.mod requires, so a Renovate gopls bump that needs a newer Go than
# the builder ships self-heals instead of breaking the build.
# renovate: datasource=go depName=golang.org/x/tools/gopls
ARG GOPLS_VERSION=v0.22.0
FROM golang:1.26 AS gopls
ARG GOPLS_VERSION
RUN GOBIN=/out GOTOOLCHAIN=auto go install "golang.org/x/tools/gopls@${GOPLS_VERSION}"

# Mnemosyne models, staged so the egress-locked sandbox never fetches them at
# runtime. Mnemosyne pulls both lazily under $HOME/.hermes — which the data PVC
# shadows — so they're baked here and an init-container seeds them onto the PVC
# on first boot (see sandbox.yaml). bge-small (~63MB) is the embedding model hit
# on every recall; MiniCPM5-1B Q4_K_M (~656MB) is the sleep-consolidation LLM.
# Both are GGUF/ONNX weights — arch-agnostic — so this stage carries no platform.
# huggingface-hub's cache layout is what fastembed and mnemosyne expect verbatim.
FROM python:3.14-slim AS mnemosyne-models
# renovate: datasource=pypi depName=huggingface-hub
ARG HUGGINGFACE_HUB_VERSION=1.21.0
RUN pip install --no-cache-dir "huggingface-hub==${HUGGINGFACE_HUB_VERSION}"
RUN python - <<'PY'
from huggingface_hub import hf_hub_download, snapshot_download
hf_hub_download("openbmb/MiniCPM5-1B-GGUF", "MiniCPM5-1B-Q4_K_M.gguf",
                local_dir="/seed/mnemosyne/models")
snapshot_download("qdrant/bge-small-en-v1.5-onnx-q",
                  cache_dir="/seed/cache/fastembed")
PY
# local_dir leaves a .cache/huggingface metadata dir next to the GGUF; it's
# download bookkeeping mnemosyne never reads, so drop it before it pollutes the
# PVC. The fastembed cache_dir layout (blobs/snapshots/refs) is left intact —
# huggingface-hub needs it verbatim for offline resolution. Then assert both
# weights actually landed, so a silently-empty seed fails the BUILD loudly here
# instead of crashing the init-container at runtime.
RUN rm -rf /seed/mnemosyne/models/.cache \
    && test -f /seed/mnemosyne/models/MiniCPM5-1B-Q4_K_M.gguf \
    && test -d /seed/cache/fastembed/models--qdrant--bge-small-en-v1.5-onnx-q

# Thin Vicegerent derivation of the upstream Hermes Agent image.
# Built on a laptop with internet, pushed to harbor.hahomelabs.com/vicegerent,
# and pulled from there by the egress-locked sandbox.
FROM nousresearch/hermes-agent:v2026.6.19@sha256:9f367c7756ef087661a361536a89f438d57a122b958dc23d82d456b1433e6e9e

LABEL org.opencontainers.image.title="vicegerent-hermes-agent" \
      org.opencontainers.image.description="Vicegerent derivation of nousresearch/hermes-agent" \
      org.opencontainers.image.source="https://gitlab.hahomelabs.com/jchristensen/vicegerent-agents" \
      org.opencontainers.image.base.name="docker.io/nousresearch/hermes-agent:v2026.6.19"

# Mnemosyne model seed (~720MB, content-stable) baked outside /opt/data so the
# data PVC doesn't shadow it. Placed early: it rarely changes, so it stays cached
# under the churnier requirements/npm/binary layers below. An init-container
# copies this into the PVC's $HOME/.hermes on first boot (see sandbox.yaml).
COPY --from=mnemosyne-models /seed /opt/hermes/mnemosyne-seed
RUN test -f /opt/hermes/mnemosyne-seed/mnemosyne/models/MiniCPM5-1B-Q4_K_M.gguf \
    && test -d /opt/hermes/mnemosyne-seed/cache/fastembed/models--qdrant--bge-small-en-v1.5-onnx-q
# Content digest of the seed, computed once at build. The init-container (see
# sandbox.yaml) compares this against a marker on the PVC and re-seeds only when it
# differs, so a rebuilt image with upgraded weights actually replaces the otherwise
# copy-once PVC copy. sort -k2 normalizes find's order so unrelated rebuilds don't
# perturb the digest. Written beside the seed dir so it's never copied onto the PVC.
RUN find /opt/hermes/mnemosyne-seed -type f -exec sha256sum {} + \
      | sort -k2 | sha256sum | cut -d' ' -f1 > /opt/hermes/mnemosyne-seed.sha256

# shellcheck: bash-language-server delegates ALL diagnostics to it. Without it
# the server runs but reports nothing — a silent no-op. apt package in trixie.
RUN apt-get update \
    && apt-get install -y --no-install-recommends shellcheck \
    && rm -rf /var/lib/apt/lists/*

# Network debugging tools for diagnosing egress / CiliumNetworkPolicy issues from
# inside the egress-locked sandbox. Default-deny DROPS (doesn't reject) a blocked
# outbound connect, so it hangs in SYN-SENT until timeout — `ss -tanp state
# syn-sent` names the stuck dest IP:port + PID with zero added capabilities. dig/
# nslookup (dnsutils) catch DNS stalls; nc (netcat-openbsd) probes raw TCP port
# reachability. curl is already in the base image. Deliberately NOT strace or
# tcpdump: those need CAP_SYS_PTRACE / CAP_NET_RAW, which the locked-down sandbox
# securityContext strips — they would bake fine but fail to attach at runtime.
RUN apt-get update \
    && apt-get install -y --no-install-recommends iproute2 dnsutils netcat-openbsd \
    && rm -rf /var/lib/apt/lists/*

# Python packages the egress-locked sandbox can't install at runtime. Add a
# package by editing requirements.txt; Renovate tracks it natively, no
# renovate.json change. Installed into the baked venv (on PATH, not /opt/data),
# so no init-container seeding is needed.
COPY requirements.txt /tmp/requirements.txt
RUN uv pip install --python /opt/hermes/.venv/bin/python --no-cache -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt

# Global npm CLIs: LSP servers Hermes launches by binary name for diagnostics,
# plus the coding-agent CLIs (claude-code, codex) Hermes delegates to. Installed
# globally into /usr/local/bin (on PATH); the egress-locked sandbox can't
# npm-install at runtime. package.json is the single Renovate-tracked source of
# truth — add a package = add a line. The coding agents' settings + mnemosyne MCP
# wiring + directory trust are baked just below.
COPY package.json /tmp/npm/package.json
RUN cd /tmp/npm \
    && npm install --global --no-fund --no-audit \
        $(node -p "Object.entries(require('./package.json').dependencies).map(([k,v])=>k+'@'+v).join(' ')") \
    && rm -rf /tmp/npm /root/.npm

# terraform-ls: a HashiCorp Go binary (not on npm). The version ARG carries the
# leading "v" to match its GitHub release tag (Renovate github-releases); the
# HashiCorp download URL has no "v", so it is stripped below. TARGETARCH is
# provided by BuildKit and matches HashiCorp's arch naming (arm64/amd64).
# renovate: datasource=github-releases depName=hashicorp/terraform-ls
ARG TERRAFORM_LS_VERSION=v0.38.7
ARG TARGETARCH
RUN TFLS_VER="${TERRAFORM_LS_VERSION#v}" \
    && curl -fsSL -o /tmp/terraform-ls.zip \
        "https://releases.hashicorp.com/terraform-ls/${TFLS_VER}/terraform-ls_${TFLS_VER}_linux_${TARGETARCH}.zip" \
    && python3 -c "import zipfile; zipfile.ZipFile('/tmp/terraform-ls.zip').extractall('/usr/local/bin')" \
    && chmod +x /usr/local/bin/terraform-ls \
    && rm /tmp/terraform-ls.zip \
    && terraform-ls version

# gopls binary from the build stage above (Go is this repo's primary language —
# the mcp-cerbos-shim is Go). Copied onto PATH; no Go toolchain in the final image.
COPY --from=gopls /out/gopls /usr/local/bin/gopls

# hermes-lcm context engine — the Lossless Context Management plugin selected by
# context.engine: lcm in apps/vicegerent/agents/hermes/config.yaml. Without it
# Hermes logs "Context engine 'lcm' not found" and silently falls back to the
# built-in compressor. It is a pure-stdlib plugin (no PyPI deps) that lives in
# Hermes' BUNDLED plugins/context_engine/<name>/ tree, which is loaded by the
# dedicated context-engine discovery, NOT the general ~/.hermes/plugins system —
# so `hermes plugins install` does not place it correctly. The destination is
# resolved from the installed Hermes package (plugins.context_engine.__file__) so
# it tracks the base image's layout instead of hardcoding a path. It is on PATH /
# in the venv tree, NOT under /opt/data, so the data PVC does not shadow it and no
# init-container seeding is needed. The version ARG carries the leading "v" to
# match the GitHub release tag (Renovate github-releases); GitHub's archive top
# dir drops the "v", so --strip-components=1 flattens it into place.
# renovate: datasource=github-releases depName=stephenschoettler/hermes-lcm
ARG HERMES_LCM_VERSION=v0.18.0
RUN LCM_DEST="$(/opt/hermes/.venv/bin/python -c 'import plugins.context_engine as p, os; print(os.path.dirname(p.__file__))')/lcm" \
    && mkdir -p "$LCM_DEST" \
    && curl -fsSL "https://github.com/stephenschoettler/hermes-lcm/archive/refs/tags/${HERMES_LCM_VERSION}.tar.gz" \
        | tar -xz -C "$LCM_DEST" --strip-components=1 \
    && /opt/hermes/.venv/bin/python -c "from plugins.context_engine import discover_context_engines as d; assert 'lcm' in [n for n, _, _ in d()], 'lcm engine not discovered after bake'; print('hermes-lcm ${HERMES_LCM_VERSION} baked + discoverable')"

# tirith: Hermes' pre-exec command security scanner (security.tirith_enabled
# defaults true; security.tirith_path "tirith" resolves the bare name via PATH).
# It screens each shell command for content-level threats — homograph/punycode
# URLs, pipe-to-interpreter, terminal-escape injection — returning allow/block/
# warn before the command runs. The egress-locked sandbox can't run Hermes'
# runtime auto-download (it would hit the Cilium egress wall and fail open), so
# bake the binary onto PATH; Hermes resolves it via shutil.which first and never
# reaches for the network. A Rust release binary from the same GitHub release the
# Hermes installer pulls; the target-triple arch maps from BuildKit's TARGETARCH
# (arm64->aarch64, amd64->x86_64). The tarball carries the `tirith` binary at top
# level (alongside shell completions + a man page we don't need), so only that
# member is extracted. `tirith --version` smoke-tests that the correct-arch
# binary landed, failing the build loudly otherwise. No pinned checksum — matches
# the terraform-ls/gopls convention here, since this repo's CI has no image-build
# stage to refresh a hash on a Renovate bump.
# renovate: datasource=github-releases depName=sheeki03/tirith
ARG TIRITH_VERSION=v0.3.3
ARG TARGETARCH
RUN case "${TARGETARCH}" in \
        arm64) TIRITH_ARCH=aarch64 ;; \
        amd64) TIRITH_ARCH=x86_64 ;; \
        *) echo "tirith: unsupported TARGETARCH=${TARGETARCH}" >&2; exit 1 ;; \
    esac \
    && curl -fsSL -o /tmp/tirith.tar.gz \
        "https://github.com/sheeki03/tirith/releases/download/${TIRITH_VERSION}/tirith-${TIRITH_ARCH}-unknown-linux-gnu.tar.gz" \
    && tar -xz -C /usr/local/bin -f /tmp/tirith.tar.gz tirith \
    && chmod +x /usr/local/bin/tirith \
    && rm /tmp/tirith.tar.gz \
    && tirith --version

# jq + yq: ubiquitous JSON / YAML processors for infra + manifest work. Both are
# single static binaries from GitHub releases whose asset suffix matches
# BuildKit's TARGETARCH verbatim (linux-arm64 / linux-amd64 for jq; linux_arm64 /
# linux_amd64 for yq), so no arch remap is needed. --version smoke-tests each so a
# wrong-arch or truncated download fails the build loudly. Renovate github-releases
# ARGs (jq tag is "jq-<ver>", so the bare version is composed below). Deliberately
# NOT baking kubectl/helm/gh/glab/terraform/aws — those belong to the MCP servers.
# renovate: datasource=github-releases depName=jqlang/jq
ARG JQ_VERSION=1.8.2
# renovate: datasource=github-releases depName=mikefarah/yq
ARG YQ_VERSION=v4.53.3
ARG TARGETARCH
RUN curl -fsSL -o /usr/local/bin/jq \
        "https://github.com/jqlang/jq/releases/download/jq-${JQ_VERSION}/jq-linux-${TARGETARCH}" \
    && chmod +x /usr/local/bin/jq \
    && jq --version \
    && curl -fsSL -o /usr/local/bin/yq \
        "https://github.com/mikefarah/yq/releases/download/${YQ_VERSION}/yq_linux_${TARGETARCH}" \
    && chmod +x /usr/local/bin/yq \
    && yq --version

# rtk ("Rust Token Killer", rtk-ai/rtk): a CLI proxy that rewrites verbose tool
# output into token-minimized form before it reaches the LLM context. The
# rtk-hermes pip package (requirements.txt) registers the `rtk-rewrite` plugin,
# which on each terminal command shells out to `rtk rewrite` and locates it via
# shutil.which — so the binary MUST be on PATH or the plugin fails-open and
# silently no-ops. Static Rust release binary, `rtk` at the tarball top level.
# Arch quirk worth noting: arm64 ships an unknown-linux-gnu build but x86_64
# ships ONLY unknown-linux-musl (there is no x86_64-gnu asset), so the case maps
# each TARGETARCH to its actual triple rather than assuming a uniform suffix.
# `rtk --version` smoke-tests the correct-arch bake. No pinned checksum — matches
# the terraform-ls/tirith convention (CI here has no image-build stage).
# renovate: datasource=github-releases depName=rtk-ai/rtk
ARG RTK_VERSION=v0.42.4
ARG TARGETARCH
RUN case "${TARGETARCH}" in \
        arm64) RTK_TRIPLE=aarch64-unknown-linux-gnu ;; \
        amd64) RTK_TRIPLE=x86_64-unknown-linux-musl ;; \
        *) echo "rtk: unsupported TARGETARCH=${TARGETARCH}" >&2; exit 1 ;; \
    esac \
    && curl -fsSL -o /tmp/rtk.tar.gz \
        "https://github.com/rtk-ai/rtk/releases/download/${RTK_VERSION}/rtk-${RTK_TRIPLE}.tar.gz" \
    && tar -xz -C /usr/local/bin -f /tmp/rtk.tar.gz rtk \
    && chmod +x /usr/local/bin/rtk \
    && rm /tmp/rtk.tar.gz \
    && rtk --version

# Tool Search context-length probe fix — source patch to the installed Hermes.
# model_tools.py::_resolve_active_context_length() sizes the active model's
# context window to gate Tool Search (defers MCP/plugin tools when their schemas
# exceed a % of the window). Upstream calls get_model_context_length() with ONLY
# the model id, so neither the explicit `model.context_length` override nor the
# per-custom-provider override can resolve, and it falls through to a step-6
# `GET openrouter.ai/api/v1/models` HTTP fetch. In the egress-locked sandbox that
# connect is blackholed and retried IPv4+IPv6, adding ~30s to EVERY CLI start.
# Our providers are custom (`opus`/agentgateway) names with no built-in mapping,
# so this fires unconditionally. The patch threads base_url/config_context_length/
# custom_providers into the call so resolution short-circuits from local config —
# no network — while Tool Search keeps working (it gets a real window to threshold
# against, which matters once many MCP servers are added). The patcher is
# fail-loud: it aborts the build if the upstream anchor is missing or duplicated
# (a refactor signal), and is idempotent on re-run. The post-step asserts the
# marker landed and that the resolver returns a positive value with sockets
# blocked, so a silently-ineffective patch fails the BUILD here, not at runtime.
# Remove once the upstream fix lands.
COPY patches/0001-toolsearch-context-length.py /tmp/0001-toolsearch-context-length.py
RUN /opt/hermes/.venv/bin/python /tmp/0001-toolsearch-context-length.py \
    && /opt/hermes/.venv/bin/python -c "import importlib.util as u; p=u.find_spec('model_tools').origin; s=open(p).read(); assert 'custom_providers=_cps) or 0)' in s, 'toolsearch patch marker missing after apply'; print('toolsearch context-length patch verified in', p)" \
    && /opt/hermes/.venv/bin/python - <<'PY'
import socket
socket.socket.connect = lambda self, addr: (_ for _ in ()).throw(RuntimeError(f"network attempted: {addr}"))
import hermes_cli.config as C
C.load_config = lambda *a, **k: {
    "model": {"provider": "opus", "default": "claude-opus-4-8",
              "base_url": "http://example.invalid/opus"},
    "providers": {"opus": {"name": "Opus", "api": "http://example.invalid/opus",
                  "default_model": "claude-opus-4-8",
                  "models": {"claude-opus-4-8": {"context_length": 1000000}}}},
}
import model_tools
v = model_tools._resolve_active_context_length()
assert v == 1000000, f"offline resolution returned {v}, expected 1000000"
print("toolsearch patch: offline context-length resolution OK ->", v)
PY
RUN rm -f /tmp/0001-toolsearch-context-length.py
