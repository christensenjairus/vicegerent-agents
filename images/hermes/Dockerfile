# Also supplies the Go toolchain (/usr/local/go) copied into the final image below.
# renovate: datasource=go depName=golang.org/x/tools/gopls
ARG GOPLS_VERSION=v0.22.0
FROM golang:1.26 AS gopls
ARG GOPLS_VERSION
RUN GOBIN=/out GOTOOLCHAIN=auto go install "golang.org/x/tools/gopls@${GOPLS_VERSION}"

# Offline Mnemosyne seed for the egress-locked sandbox; preserve HF cache layout.
FROM python:3.14-slim AS mnemosyne-models
# renovate: datasource=pypi depName=huggingface-hub
ARG HUGGINGFACE_HUB_VERSION=1.22.0
RUN pip install --no-cache-dir "huggingface-hub==${HUGGINGFACE_HUB_VERSION}"
RUN python - <<'PY'
from huggingface_hub import hf_hub_download, snapshot_download
hf_hub_download("openbmb/MiniCPM5-1B-GGUF", "MiniCPM5-1B-Q4_K_M.gguf",
                local_dir="/seed/mnemosyne/models")
snapshot_download("qdrant/bge-small-en-v1.5-onnx-q",
                  cache_dir="/seed/cache/fastembed")
PY
# Drop GGUF download metadata, keep fastembed's HF cache layout, and fail build if the seed is empty.
RUN rm -rf /seed/mnemosyne/models/.cache \
    && test -f /seed/mnemosyne/models/MiniCPM5-1B-Q4_K_M.gguf \
    && test -d /seed/cache/fastembed/models--qdrant--bge-small-en-v1.5-onnx-q

# Thin Vicegerent derivation of upstream Hermes Agent.
FROM nousresearch/hermes-agent:v2026.7.7@sha256:ce3b39adf33fffae8cb19dcae74f971975b972efe91207f7d7539f80a0aae66a

LABEL org.opencontainers.image.title="vicegerent-hermes-agent" \
      org.opencontainers.image.description="Vicegerent derivation of nousresearch/hermes-agent" \
      org.opencontainers.image.source="https://gitlab.hahomelabs.com/jchristensen/vicegerent-agents" \
      org.opencontainers.image.base.name="docker.io/nousresearch/hermes-agent:v2026.7.7"

# requirements.txt tools (pre-commit, detect-secrets, yamlfix) live here but aren't on PATH upstream.
# ENV covers non-login shells (exec sessions); /etc/profile.d re-prepends it since Debian's /etc/profile
# unconditionally reassigns PATH for non-root UIDs in login shells (Hermes terminal()'s bash -l snapshot).
ENV PATH="/opt/hermes/.venv/bin:${PATH}"
RUN printf 'export PATH="/opt/hermes/.venv/bin:$PATH"\n' > /etc/profile.d/00-hermes-venv-path.sh \
    && chmod 644 /etc/profile.d/00-hermes-venv-path.sh

# Shadow bash builtins that have a real coreutils/util-linux equivalent so
# invoking them triggers a genuine execve() -- Tetragon's process_exec hook
# only sees real syscalls, so builtins (echo, printf, etc.) run in-process
# and are otherwise invisible in VictoriaLogs. Covers 8 shadowable builtins;
# state-mutating ones (cd, export, eval, set, ...) have no binary equivalent
# and can't be covered this way. BASH_ENV (not just bash.bashrc) is required
# because Hermes' terminal() tool runs plain non-interactive, non-login
# `bash -c` -- the one shell-invocation mode that sources neither
# /etc/profile nor /etc/bash.bashrc, only $BASH_ENV. Accepted tradeoff:
# external pwd resolves symlinks (physical path) where the builtin preserves
# the logical $PWD, and external kill can't take job specs (%1) -- global
# BASH_ENV shadowing means every bash -c in the sandbox gets this, not just
# interactive sessions.
ENV BASH_ENV=/etc/bash-shadow-builtins.sh
RUN printf 'enable -n "[" echo false kill printf pwd test true\n' > /etc/bash-shadow-builtins.sh \
    && chmod 644 /etc/bash-shadow-builtins.sh \
    && printf '[ -r /etc/bash-shadow-builtins.sh ] \&\& . /etc/bash-shadow-builtins.sh\n' >> /etc/bash.bashrc

# Bake the Mnemosyne seed outside /opt/data so the data PVC cannot shadow it.
COPY --from=mnemosyne-models /seed /opt/hermes/mnemosyne-seed
RUN test -f /opt/hermes/mnemosyne-seed/mnemosyne/models/MiniCPM5-1B-Q4_K_M.gguf \
    && test -d /opt/hermes/mnemosyne-seed/cache/fastembed/models--qdrant--bge-small-en-v1.5-onnx-q \
    && chmod -R a+rX /opt/hermes/mnemosyne-seed
# Stable seed digest for init-container reseed-on-change.
RUN find /opt/hermes/mnemosyne-seed -type f -exec sha256sum {} + \
      | sort -k2 | sha256sum | cut -d' ' -f1 > /opt/hermes/mnemosyne-seed.sha256

# shellcheck is required by bash-language-server for diagnostics.
RUN apt-get update \
    && apt-get install -y --no-install-recommends shellcheck \
    && rm -rf /var/lib/apt/lists/*

# Network debug tools that work without extra caps; blocked egress shows in ss SYN-SENT.
RUN apt-get update \
    && apt-get install -y --no-install-recommends iproute2 dnsutils netcat-openbsd tmux \
    && rm -rf /var/lib/apt/lists/*

# Runtime Python deps for the egress-locked sandbox; Renovate tracks requirements.txt.
COPY requirements.txt /tmp/requirements.txt
RUN uv pip install --python /opt/hermes/.venv/bin/python --no-cache -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt

# Global npm CLIs used by Hermes/coding agents; package.json is the Renovate-tracked source.
COPY package.json /tmp/npm/package.json
RUN cd /tmp/npm \
    && npm install --global --no-fund --no-audit \
        $(node -p "Object.entries(require('./package.json').dependencies).map(([k,v])=>k+'@'+v).join(' ')") \
    && rm -rf /tmp/npm /root/.npm

# terraform-ls is downloaded from HashiCorp releases; strip leading v for URL.
# renovate: datasource=github-releases depName=hashicorp/terraform-ls
ARG TERRAFORM_LS_VERSION=v0.38.8
ARG TARGETARCH
RUN TFLS_VER="${TERRAFORM_LS_VERSION#v}" \
    && curl -fsSL -o /tmp/terraform-ls.zip \
        "https://releases.hashicorp.com/terraform-ls/${TFLS_VER}/terraform-ls_${TFLS_VER}_linux_${TARGETARCH}.zip" \
    && python3 -c "import zipfile; zipfile.ZipFile('/tmp/terraform-ls.zip').extractall('/usr/local/bin')" \
    && chmod +x /usr/local/bin/terraform-ls \
    && rm /tmp/terraform-ls.zip \
    && terraform-ls version

# terragrunt for formatting HCL/Terraform files in repos that use it.
# renovate: datasource=github-releases depName=gruntwork-io/terragrunt
ARG TERRAGRUNT_VERSION=v1.1.0
ARG TARGETARCH
RUN curl -fsSL -o /usr/local/bin/terragrunt \
        "https://github.com/gruntwork-io/terragrunt/releases/download/${TERRAGRUNT_VERSION}/terragrunt_linux_${TARGETARCH}" \
    && chmod +x /usr/local/bin/terragrunt \
    && terragrunt --version

# Full Go toolchain so agents can build/test Go code (e.g. mcp-cerbos-shim).
COPY --from=gopls /usr/local/go /usr/local/go
ENV PATH="/usr/local/go/bin:${PATH}" \
    GOPROXY=https://proxy.golang.org,direct \
    GOSUMDB=sum.golang.org \
    GOTOOLCHAIN=local
RUN printf 'export PATH="/usr/local/go/bin:$PATH"\n' >> /etc/profile.d/00-hermes-venv-path.sh \
    && go version
COPY --from=gopls /out/gopls /usr/local/go/bin/gopls

# hermes-lcm must live in Hermes' bundled context_engine tree, not ~/.hermes/plugins.
# renovate: datasource=github-releases depName=stephenschoettler/hermes-lcm
ARG HERMES_LCM_VERSION=v0.19.0
RUN LCM_DEST="$(/opt/hermes/.venv/bin/python -c 'import plugins.context_engine as p, os; print(os.path.dirname(p.__file__))')/lcm" \
    && mkdir -p "$LCM_DEST" \
    && curl -fsSL "https://github.com/stephenschoettler/hermes-lcm/archive/refs/tags/${HERMES_LCM_VERSION}.tar.gz" \
        | tar -xz -C "$LCM_DEST" --strip-components=1 \
    && /opt/hermes/.venv/bin/python -c "from plugins.context_engine import discover_context_engines as d; assert 'lcm' in [n for n, _, _ in d()], 'lcm engine not discovered after bake'; print('hermes-lcm ${HERMES_LCM_VERSION} baked + discoverable')"

# Bake tirith pre-exec scanner; runtime auto-download would fail behind egress policy.
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

# Bake jq/yq only; cluster/cloud CLIs belong in MCP servers, not the sandbox image.
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

# Bake buf for repos with a protobuf pre-commit toolchain (buf lint/format).
# renovate: datasource=github-releases depName=bufbuild/buf
ARG BUF_VERSION=v1.71.0
ARG TARGETARCH
RUN case "${TARGETARCH}" in \
        arm64) BUF_ARCH=aarch64 ;; \
        amd64) BUF_ARCH=x86_64 ;; \
        *) echo "buf: unsupported TARGETARCH=${TARGETARCH}" >&2; exit 1 ;; \
    esac \
    && curl -fsSL -o /tmp/buf.tar.gz \
        "https://github.com/bufbuild/buf/releases/download/${BUF_VERSION}/buf-Linux-${BUF_ARCH}.tar.gz" \
    && tar -xz -C /tmp -f /tmp/buf.tar.gz buf/bin/buf --strip-components=2 \
    && mv /tmp/buf /usr/local/bin/buf \
    && rm /tmp/buf.tar.gz \
    && chmod +x /usr/local/bin/buf \
    && buf --version

# Bake kustomize + kubeconform + cerbos + helm so validate.sh and pre-commit hooks
# work offline in the egress-locked sandbox without downloading at runtime.
# renovate: datasource=github-releases depName=kubernetes-sigs/kustomize
ARG KUSTOMIZE_VERSION=v5.8.1
# renovate: datasource=github-releases depName=yannh/kubeconform
ARG KUBECONFORM_VERSION=v0.8.0
# renovate: datasource=github-releases depName=cerbos/cerbos
ARG CERBOS_VERSION=v0.53.0
ARG TARGETARCH
RUN case "${TARGETARCH}" in \
        arm64) KUSTOMIZE_ARCH=arm64 ;; \
        amd64) KUSTOMIZE_ARCH=amd64 ;; \
        *) echo "kustomize: unsupported TARGETARCH=${TARGETARCH}" >&2; exit 1 ;; \
    esac \
    && curl -fsSL -o /tmp/kustomize.tar.gz \
        "https://github.com/kubernetes-sigs/kustomize/releases/download/kustomize%2F${KUSTOMIZE_VERSION}/kustomize_${KUSTOMIZE_VERSION}_linux_${KUSTOMIZE_ARCH}.tar.gz" \
    && tar -xz -C /usr/local/bin -f /tmp/kustomize.tar.gz kustomize \
    && rm /tmp/kustomize.tar.gz \
    && kustomize version \
    && curl -fsSL -o /tmp/kubeconform.tar.gz \
        "https://github.com/yannh/kubeconform/releases/download/${KUBECONFORM_VERSION}/kubeconform-linux-${TARGETARCH}.tar.gz" \
    && tar -xz -C /usr/local/bin -f /tmp/kubeconform.tar.gz kubeconform \
    && rm /tmp/kubeconform.tar.gz \
    && kubeconform -v \
    && CERBOS_VER="${CERBOS_VERSION#v}" \
    && case "${TARGETARCH}" in \
        arm64) CERBOS_ARCH=arm64 ;; \
        amd64) CERBOS_ARCH=x86_64 ;; \
        *) echo "cerbos: unsupported TARGETARCH=${TARGETARCH}" >&2; exit 1 ;; \
    esac \
    && curl -fsSL -o /tmp/cerbos.tar.gz \
        "https://github.com/cerbos/cerbos/releases/download/${CERBOS_VERSION}/cerbos_${CERBOS_VER}_Linux_${CERBOS_ARCH}.tar.gz" \
    && tar -xz -C /usr/local/bin -f /tmp/cerbos.tar.gz cerbos \
    && rm /tmp/cerbos.tar.gz \
    && cerbos --version

# Bake helm so scripts/validate.sh and pre-commit validate-flux-manifests can run offline.
# renovate: datasource=github-releases depName=helm/helm
ARG HELM_VERSION=v4.2.2
ARG TARGETARCH
RUN case "${TARGETARCH}" in \
        arm64) HELM_ARCH=arm64 ;; \
        amd64) HELM_ARCH=amd64 ;; \
        *) echo "helm: unsupported TARGETARCH=${TARGETARCH}" >&2; exit 1 ;; \
    esac \
    && curl -fsSL -o /tmp/helm.tar.gz \
        "https://get.helm.sh/helm-${HELM_VERSION}-linux-${HELM_ARCH}.tar.gz" \
    && tar -xz -C /usr/local/bin --strip-components=1 \
        -f /tmp/helm.tar.gz "linux-${HELM_ARCH}/helm" \
    && rm /tmp/helm.tar.gz \
    && helm version

# Bake bazelisk + buildifier/buildozer so agents can build/run Bazel targets in
# repos that use Bazel (e.g. k8s-manifests' helm_charts renders), and reformat/edit
# BUILD files, entirely offline in the egress-locked sandbox. bazelisk itself is a thin
# version-selecting launcher; the actual bazel(1) binary it execs is downloaded on first
# use from releases.bazel.build (verified: NOT github.com -- a plain
# `bazelisk version` attempt against the sandbox's own egress proxy gets a hard
# `Forbidden` from releases.bazel.build even though github.com release assets are
# reachable). Pre-warming bazelisk's on-disk cache at build time (BAZELISK_HOME below,
# baked with BAZEL_PINNED_VERSION) means the sandbox never has to dial
# releases.bazel.build at runtime for that one pinned version -- but this build step
# itself needs releases.bazel.build reachable from wherever `docker build` runs (the
# operator's laptop per this README, not the sandbox). Bzlmod dependency resolution
# (bcr.bazel.build, or an internal mirror) still needs its host reachable at *runtime* --
# this bake only guarantees bazel/buildozer/buildifier themselves are present offline.
#
# keytool (headless JRE) so _sandbox.tpl can build a Bazel-trusted PKCS12 truststore
# at pod-start.
RUN apt-get update \
    && apt-get install -y --no-install-recommends openjdk-21-jre-headless \
    && rm -rf /var/lib/apt/lists/* \
    && keytool -help >/dev/null

# renovate: datasource=github-releases depName=bazelbuild/bazelisk
ARG BAZELISK_VERSION=v1.29.0
# renovate: datasource=github-releases depName=bazelbuild/buildtools
ARG BUILDTOOLS_VERSION=v8.5.1
# Pinned Bazel release to pre-warm bazelisk's cache with; keep in sync with the
# .bazelversion of the Bazel repos this sandbox operates against (k8s-manifests: 8.5.1).
ARG BAZEL_PINNED_VERSION=8.5.1
ARG TARGETARCH
ENV BAZELISK_HOME=/opt/hermes/.cache/bazelisk
RUN case "${TARGETARCH}" in \
        arm64) BT_ARCH=arm64 ;; \
        amd64) BT_ARCH=amd64 ;; \
        *) echo "bazelisk/buildtools: unsupported TARGETARCH=${TARGETARCH}" >&2; exit 1 ;; \
    esac \
    && curl -fsSL -o /usr/local/bin/bazelisk \
        "https://github.com/bazelbuild/bazelisk/releases/download/${BAZELISK_VERSION}/bazelisk-linux-${BT_ARCH}" \
    && chmod +x /usr/local/bin/bazelisk \
    && ln -s /usr/local/bin/bazelisk /usr/local/bin/bazel \
    && curl -fsSL -o /usr/local/bin/buildifier \
        "https://github.com/bazelbuild/buildtools/releases/download/${BUILDTOOLS_VERSION}/buildifier-linux-${BT_ARCH}" \
    && chmod +x /usr/local/bin/buildifier \
    && curl -fsSL -o /usr/local/bin/buildozer \
        "https://github.com/bazelbuild/buildtools/releases/download/${BUILDTOOLS_VERSION}/buildozer-linux-${BT_ARCH}" \
    && chmod +x /usr/local/bin/buildozer \
    && buildifier --version \
    && buildozer -version \
    && mkdir -p "${BAZELISK_HOME}" \
    && USE_BAZEL_VERSION="${BAZEL_PINNED_VERSION}" bazelisk version \
    && test -n "$(find "${BAZELISK_HOME}" -type f -name 'bazel-real' -o -type f -iname 'bazel*' 2>/dev/null | head -1)" \
    && chmod -R a+rX "${BAZELISK_HOME}"

# Bake rtk for rtk-hermes; arch triples differ between arm64 and amd64 release assets.
# renovate: datasource=github-releases depName=rtk-ai/rtk
ARG RTK_VERSION=v0.43.0
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

# Patch Hermes Tool Search context-length lookup to avoid a 30s OpenRouter probe in the egress-locked sandbox; remove once upstream lands.
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

# Patch agentburn: (1) HERMES_HOME support in the hermes adapter so the MCP
# server finds state.db at $HERMES_HOME instead of ~/.hermes/state.db, and
# (2) inject missing model prices for Anthropic/OpenAI models we run.
# Remove once agentburn upstreams HERMES_HOME support and adds these models.
COPY patches/0004-agentburn.py /tmp/0004-agentburn.py
RUN /opt/hermes/.venv/bin/python /tmp/0004-agentburn.py
RUN rm -f /tmp/0004-agentburn.py


# Patch Hermes Slack adapter so the catch-all slash command name is configurable
# via HERMES_SLACK_COMMAND_NAME (default: "hermes"). Set in sandbox.yaml to rename
# the bot's slash command (e.g. /hermes). Remove once upstream supports this.
COPY patches/0006-slack-command-name.py /tmp/0006-slack-command-name.py
RUN /opt/hermes/.venv/bin/python /tmp/0006-slack-command-name.py
RUN rm -f /tmp/0006-slack-command-name.py

# Make Slack always bypass the egress MITM proxy. slack_sdk auto-loads HTTPS_PROXY
# (ignoring NO_PROXY), forcing Slack through the GET-only scrubbing proxy where it
# cannot work (token scrubbing + POST/WebSocket blocking). Patch slack_sdk's env
# proxy loader to return None. Remove once slack_sdk honors NO_PROXY.
COPY patches/0007-slack-bypass-egress-proxy.py /tmp/0007-slack-bypass-egress-proxy.py
RUN /opt/hermes/.venv/bin/python /tmp/0007-slack-bypass-egress-proxy.py
RUN rm -f /tmp/0007-slack-bypass-egress-proxy.py

# Patch tools/approval.py to add tirith_only mode: silences pattern-match false
# positives while still blocking on actual tirith security findings.
# Remove once upstream Hermes adds this as a first-class mode.
COPY patches/0008-approval-tirith-only-mode.py /tmp/0008-approval-tirith-only-mode.py
RUN /opt/hermes/.venv/bin/python /tmp/0008-approval-tirith-only-mode.py
RUN rm -f /tmp/0008-approval-tirith-only-mode.py

# Patch tools/mcp_tool.py: two MCP circuit-breaker fixes, merged into one
# patch since they touch the same function (_make_tool_handler's _handler).
# (1) Don't trip the breaker on business errors (isError=true relayed as a
#     JSON "error" key) — only real transport/auth failures should count
#     toward the 3-strike "server unreachable" breaker. Remove once upstream
#     Hermes lands #47918/#47955 (hermes-agent #47851/#11113).
# (2) Scope the breaker per-backend for vMCP optimizer aggregation. One
#     flaky backend (e.g. alertmanager timing out) was tripping the shared
#     "vmcp" server breaker and blocking all ~11 aggregated backends for
#     60s, not just the flaky one. Vicegerent-specific fix (not an upstream
#     issue) — unique to the vMCP optimizer's call_tool meta-tool
#     aggregation pattern this repo uses.
COPY patches/0009-mcp-circuit-breaker.py /tmp/0009-mcp-circuit-breaker.py
RUN /opt/hermes/.venv/bin/python /tmp/0009-mcp-circuit-breaker.py
RUN rm -f /tmp/0009-mcp-circuit-breaker.py

# Patch tools/web_tools.py so web_extract only registers when an
# extract-capable backend (firecrawl/tavily/exa/parallel) is configured.
# We run SearXNG-only (web.search_backend: searxng) — search-only backends
# pass the shared check_web_api_key() gate, so web_extract was registering
# and erroring at call time ("SearXNG is a search-only backend...") instead
# of simply not existing. Remove once upstream gates web_extract on
# supports_extract() itself (loosely tracked with hermes-agent #19198).
COPY patches/0011-web-extract-capability-check.py /tmp/0011-web-extract-capability-check.py
RUN /opt/hermes/.venv/bin/python /tmp/0011-web-extract-capability-check.py
RUN rm -f /tmp/0011-web-extract-capability-check.py

# Patch tools/approval.py so approvals.pattern_silence also silences the
# separate execute_code whole-script approval gate (check_execute_code_guard),
# which previously ignored the silence list entirely. Arbitrary script
# execution is the intended capability of this sandbox; the isolation
# boundary is the pod/network layer, not this gate.
COPY patches/0012-execute-code-pattern-silence.py /tmp/0012-execute-code-pattern-silence.py
RUN /opt/hermes/.venv/bin/python /tmp/0012-execute-code-pattern-silence.py
RUN rm -f /tmp/0012-execute-code-pattern-silence.py

# Trust our in-cluster agentgateway proxy for Anthropic traffic in BOTH the
# auxiliary client (agent/auxiliary_client.py -- approval checks, title
# generation, memory compression, vision fallback) and the main/subagent
# runtime resolver (hermes_cli/runtime_provider.py -- delegate_task()
# subagent credentials). Both independently defaulted to a hostname-only
# allowlist ("api.anthropic.com" literal) that excludes model.base_url's
# agentgateway /anthropic route (see AGENTS.md "Environment"), so calls
# fell back to a direct api.anthropic.com request, which the sealed egress
# boundary blocks -- producing a connection error Hermes treats as an
# Anthropic outage and fails over to OpenAI, burning its quota on what was
# never an outage. Hit live during a delegate_task fan-out (agent.log: two
# ThreadPoolExecutor worker threads both APIConnectionError'd against
# base_url=https://api.anthropic.com, then failed over to OpenAI which was
# quota-exhausted). Also de-duplicates the runtime_provider.py override
# check that was copy-pasted verbatim across its three Anthropic
# resolution call sites (credential-pool path, explicit-override path,
# env-var/fallback path), so a future edit to one copy can't silently
# desync it from the other two. Both edits are a pure trust/refactor
# change -- same inputs, same outputs on the runtime_provider.py side.
# Remove once upstream unifies the auxiliary-client and main-client
# Anthropic-host trust logic and de-duplicates the three call sites
# itself.
COPY patches/0014-auxiliary-anthropic-gateway-host.py /tmp/0014-auxiliary-anthropic-gateway-host.py
RUN /opt/hermes/.venv/bin/python /tmp/0014-auxiliary-anthropic-gateway-host.py
RUN rm -f /tmp/0014-auxiliary-anthropic-gateway-host.py

# Patch 0014 fixed Anthropic gateway-host trust at the resolution layer, but
# a persisted credential-pool entry can silently reintroduce a raw
# api.anthropic.com host: agent/credential_pool.py seeds/re-seeds an
# anthropic pool entry's base_url from a hardcoded default whenever
# ANTHROPIC_BASE_URL isn't set (this platform uses model.base_url instead),
# and that entry persists on the data volume across pod restarts and image
# rebuilds. tools/delegate_tool.py leases this pool for every subagent and
# unconditionally trusts the entry's base_url in run_agent.py's
# _swap_credential(), overwriting the correctly-constructed gateway URL
# right before the subagent's first call. Reproduced live on a freshly
# rebuilt pod with 0014 confirmed present: subagent turns hit
# base_url=https://api.anthropic.com (blocked by the sealed egress
# boundary), fail over to the OpenAI fallback, which is separately
# quota-exhausted. Fixes both the seed-time and use-time windows: seeding
# now consults the same _resolve_anthropic_base_url_override() helper 0014
# introduced, and _swap_credential() re-derives the base_url through it on
# every lease so an already-stale persisted entry self-heals instead of
# requiring a manual pool-file wipe. Depends on 0014 (imports its helper).
COPY patches/0016-credential-pool-anthropic-base-url.py /tmp/0016-credential-pool-anthropic-base-url.py
RUN /opt/hermes/.venv/bin/python /tmp/0016-credential-pool-anthropic-base-url.py
RUN rm -f /tmp/0016-credential-pool-anthropic-base-url.py

# Lower the parked MCP self-probe cadence (upstream's _PARKED_RETRY_INTERVAL,
# default 300s) to 15s so a recovered vMCP backend is picked up in ~15s instead
# of ~5min. Upstream (v2026.7.7) owns the parked-retry mechanism itself; this
# patch only tunes the interval for this single-backend, interactive deployment.
COPY patches/0017-mcp-parked-retry-interval.py /tmp/0017-mcp-parked-retry-interval.py
RUN /opt/hermes/.venv/bin/python /tmp/0017-mcp-parked-retry-interval.py
RUN rm -f /tmp/0017-mcp-parked-retry-interval.py

# Patch tools/registry.py so _check_fn_cached() logs the actual exception +
# traceback when a tool's check_fn raises, instead of only ever logging the
# one-line "check_fn %s raised; dependent tools will be unavailable this turn"
# summary. Hit live diagnosing a `_check_file_reqs raised` warning in gateway/
# dashboard startup logs with no way to see what actually failed. Purely
# additive — does not change caching, control flow, or the existing `raised`
# bool consumed by the flake-vs-real-outage grace-period logic. Remove once
# upstream Hermes logs check_fn exceptions with a traceback itself.
COPY patches/0018-check-fn-exception-traceback.py /tmp/0018-check-fn-exception-traceback.py
RUN /opt/hermes/.venv/bin/python /tmp/0018-check-fn-exception-traceback.py
RUN rm -f /tmp/0018-check-fn-exception-traceback.py

# Bundled skill files are installed 444 (root-owned in the image); make them
# 644 so skill_manager_tool can patch them after copytree seeds /opt/data/skills.
RUN find /opt/hermes/skills -name "SKILL.md" -exec chmod 644 {} +
