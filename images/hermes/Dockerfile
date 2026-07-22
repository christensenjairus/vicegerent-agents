# Also supplies the Go toolchain (/usr/local/go) copied into the final image below.
# renovate: datasource=go depName=golang.org/x/tools/gopls
ARG GOPLS_VERSION=v0.23.0
FROM golang:1.26 AS gopls
ARG GOPLS_VERSION
RUN GOBIN=/out GOTOOLCHAIN=auto go install "golang.org/x/tools/gopls@${GOPLS_VERSION}"

# Every purely-static release binary this image bakes, fetched here instead
# of in the main stage below so a Renovate bump of the nousresearch/hermes-agent
# base (FROM line further down) doesn't invalidate any of these downloads --
# none of them depend on the base image's contents. Kept on a plain floating
# tag (no digest pin), matching the gopls/mnemosyne-models builder stages'
# convention below.
#
# Portability note: every tool here except tirith ships a statically-linked
# Go/Rust binary (CGO_ENABLED=0 equivalent), so its glibc/musl ABI is
# irrelevant -- only architecture (TARGETARCH) matters. tirith's release
# asset is named "...-unknown-linux-gnu.tar.gz", i.e. dynamically linked
# against glibc, but nothing in this stage executes it (verification happens
# in the main stage against the real target ABI -- see below). This stage
# uses debian:trixie-slim (glibc 2.41) rather than bookworm-slim (glibc
# 2.36) because the real bazel(1) binary bazelisk downloads below to warm
# its cache IS executed here and is itself glibc-linked. Hit exactly this
# class of failure with rtk's arm64 build needing GLIBC_2.39 against
# bookworm's 2.36 -- fixed by removing rtk's version-check from this stage
# entirely (this stage's glibc was never relevant to rtk's compatibility
# with the final image; the main stage's re-verification catches any real
# incompatibility). Bumping to trixie is extra headroom for the one thing
# that does execute here (bazelisk's real download-and-run).
FROM debian:trixie-slim AS tools
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl tar unzip \
    && rm -rf /var/lib/apt/lists/*
ARG TARGETARCH

# terraform-ls is downloaded from HashiCorp releases; strip leading v for URL.
# renovate: datasource=github-releases depName=hashicorp/terraform-ls
ARG TERRAFORM_LS_VERSION=v0.38.8
RUN TFLS_VER="${TERRAFORM_LS_VERSION#v}" \
    && curl -fsSL -o /tmp/terraform-ls.zip \
        "https://releases.hashicorp.com/terraform-ls/${TFLS_VER}/terraform-ls_${TFLS_VER}_linux_${TARGETARCH}.zip" \
    && unzip -o /tmp/terraform-ls.zip -d /usr/local/bin \
    && chmod +x /usr/local/bin/terraform-ls \
    && rm /tmp/terraform-ls.zip

# terragrunt for formatting HCL/Terraform files in repos that use it.
# renovate: datasource=github-releases depName=gruntwork-io/terragrunt
ARG TERRAGRUNT_VERSION=v1.1.1
RUN curl -fsSL -o /usr/local/bin/terragrunt \
        "https://github.com/gruntwork-io/terragrunt/releases/download/${TERRAGRUNT_VERSION}/terragrunt_linux_${TARGETARCH}" \
    && chmod +x /usr/local/bin/terragrunt

# Bake tirith pre-exec scanner; runtime auto-download would fail behind egress policy.
# renovate: datasource=github-releases depName=sheeki03/tirith
ARG TIRITH_VERSION=v0.3.3
RUN case "${TARGETARCH}" in \
        arm64) TIRITH_ARCH=aarch64 ;; \
        amd64) TIRITH_ARCH=x86_64 ;; \
        *) echo "tirith: unsupported TARGETARCH=${TARGETARCH}" >&2; exit 1 ;; \
    esac \
    && curl -fsSL -o /tmp/tirith.tar.gz \
        "https://github.com/sheeki03/tirith/releases/download/${TIRITH_VERSION}/tirith-${TIRITH_ARCH}-unknown-linux-gnu.tar.gz" \
    && tar -xz -C /usr/local/bin -f /tmp/tirith.tar.gz tirith \
    && chmod +x /usr/local/bin/tirith \
    && rm /tmp/tirith.tar.gz

# Bake jq/yq only; cluster/cloud CLIs belong in MCP servers, not the sandbox image.
# renovate: datasource=github-releases depName=jqlang/jq
ARG JQ_VERSION=1.8.2
# renovate: datasource=github-releases depName=mikefarah/yq
ARG YQ_VERSION=v4.53.3
RUN curl -fsSL -o /usr/local/bin/jq \
        "https://github.com/jqlang/jq/releases/download/jq-${JQ_VERSION}/jq-linux-${TARGETARCH}" \
    && chmod +x /usr/local/bin/jq \
    && curl -fsSL -o /usr/local/bin/yq \
        "https://github.com/mikefarah/yq/releases/download/${YQ_VERSION}/yq_linux_${TARGETARCH}" \
    && chmod +x /usr/local/bin/yq

# Bake buf for repos with a protobuf pre-commit toolchain (buf lint/format).
# renovate: datasource=github-releases depName=bufbuild/buf
ARG BUF_VERSION=v1.72.0
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
    && chmod +x /usr/local/bin/buf

# Bake kustomize + kubeconform + cerbos + helm so validate.sh and pre-commit hooks
# work offline in the egress-locked sandbox without downloading at runtime.
# renovate: datasource=github-releases depName=kubernetes-sigs/kustomize
ARG KUSTOMIZE_VERSION=v5.8.1
# renovate: datasource=github-releases depName=yannh/kubeconform
ARG KUBECONFORM_VERSION=v0.8.0
# renovate: datasource=github-releases depName=cerbos/cerbos
ARG CERBOS_VERSION=v0.54.0
RUN case "${TARGETARCH}" in \
        arm64) KUSTOMIZE_ARCH=arm64 ;; \
        amd64) KUSTOMIZE_ARCH=amd64 ;; \
        *) echo "kustomize: unsupported TARGETARCH=${TARGETARCH}" >&2; exit 1 ;; \
    esac \
    && curl -fsSL -o /tmp/kustomize.tar.gz \
        "https://github.com/kubernetes-sigs/kustomize/releases/download/kustomize%2F${KUSTOMIZE_VERSION}/kustomize_${KUSTOMIZE_VERSION}_linux_${KUSTOMIZE_ARCH}.tar.gz" \
    && tar -xz -C /usr/local/bin -f /tmp/kustomize.tar.gz kustomize \
    && rm /tmp/kustomize.tar.gz \
    && curl -fsSL -o /tmp/kubeconform.tar.gz \
        "https://github.com/yannh/kubeconform/releases/download/${KUBECONFORM_VERSION}/kubeconform-linux-${TARGETARCH}.tar.gz" \
    && tar -xz -C /usr/local/bin -f /tmp/kubeconform.tar.gz kubeconform \
    && rm /tmp/kubeconform.tar.gz \
    && CERBOS_VER="${CERBOS_VERSION#v}" \
    && case "${TARGETARCH}" in \
        arm64) CERBOS_ARCH=arm64 ;; \
        amd64) CERBOS_ARCH=x86_64 ;; \
        *) echo "cerbos: unsupported TARGETARCH=${TARGETARCH}" >&2; exit 1 ;; \
    esac \
    && curl -fsSL -o /tmp/cerbos.tar.gz \
        "https://github.com/cerbos/cerbos/releases/download/${CERBOS_VERSION}/cerbos_${CERBOS_VER}_Linux_${CERBOS_ARCH}.tar.gz" \
    && tar -xz -C /usr/local/bin -f /tmp/cerbos.tar.gz cerbos \
    && rm /tmp/cerbos.tar.gz

# Bake helm so scripts/validate.sh and pre-commit validate-flux-manifests can run offline.
# renovate: datasource=github-releases depName=helm/helm
ARG HELM_VERSION=v4.2.3
RUN case "${TARGETARCH}" in \
        arm64) HELM_ARCH=arm64 ;; \
        amd64) HELM_ARCH=amd64 ;; \
        *) echo "helm: unsupported TARGETARCH=${TARGETARCH}" >&2; exit 1 ;; \
    esac \
    && curl -fsSL -o /tmp/helm.tar.gz \
        "https://get.helm.sh/helm-${HELM_VERSION}-linux-${HELM_ARCH}.tar.gz" \
    && tar -xz -C /usr/local/bin --strip-components=1 \
        -f /tmp/helm.tar.gz "linux-${HELM_ARCH}/helm" \
    && rm /tmp/helm.tar.gz

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
# renovate: datasource=github-releases depName=bazelbuild/bazelisk
ARG BAZELISK_VERSION=v1.29.0
# renovate: datasource=github-releases depName=bazelbuild/buildtools
ARG BUILDTOOLS_VERSION=v8.5.1
# Pinned Bazel release to pre-warm bazelisk's cache with; keep in sync with the
# .bazelversion of the Bazel repos this sandbox operates against (k8s-manifests: 8.5.1).
ARG BAZEL_PINNED_VERSION=8.5.1
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
    && mkdir -p "${BAZELISK_HOME}" \
    && USE_BAZEL_VERSION="${BAZEL_PINNED_VERSION}" bazelisk version \
    && test -n "$(find "${BAZELISK_HOME}" -type f -name 'bazel-real' -o -type f -iname 'bazel*' 2>/dev/null | head -1)" \
    && chmod -R a+rX "${BAZELISK_HOME}"

# Bake rtk for rtk-hermes; arch triples differ between arm64 and amd64 release assets.
# renovate: datasource=github-releases depName=rtk-ai/rtk
ARG RTK_VERSION=v0.43.0
RUN case "${TARGETARCH}" in \
        arm64) RTK_TRIPLE=aarch64-unknown-linux-gnu ;; \
        amd64) RTK_TRIPLE=x86_64-unknown-linux-musl ;; \
        *) echo "rtk: unsupported TARGETARCH=${TARGETARCH}" >&2; exit 1 ;; \
    esac \
    && curl -fsSL -o /tmp/rtk.tar.gz \
        "https://github.com/rtk-ai/rtk/releases/download/${RTK_VERSION}/rtk-${RTK_TRIPLE}.tar.gz" \
    && tar -xz -C /usr/local/bin -f /tmp/rtk.tar.gz rtk \
    && chmod +x /usr/local/bin/rtk \
    && rm /tmp/rtk.tar.gz

# Offline Mnemosyne + STT model seed for the egress-locked sandbox; preserve HF cache layout.
FROM python:3.14-slim AS mnemosyne-models
# renovate: datasource=pypi depName=huggingface-hub
ARG HUGGINGFACE_HUB_VERSION=1.24.0
RUN pip install --no-cache-dir "huggingface-hub==${HUGGINGFACE_HUB_VERSION}"
RUN python - <<'PY'
from huggingface_hub import hf_hub_download, snapshot_download
hf_hub_download("openbmb/MiniCPM5-1B-GGUF", "MiniCPM5-1B-Q4_K_M.gguf",
                local_dir="/seed/mnemosyne/models")
snapshot_download("qdrant/bge-small-en-v1.5-onnx-q",
                  cache_dir="/seed/cache/fastembed")
# faster-whisper's default stt.local.model ("base") maps to this HF repo
# (see faster_whisper/utils.py _MODELS); baked here since HF_HUB_OFFLINE=1
# blocks the runtime download faster-whisper does on first transcription.
snapshot_download("Systran/faster-whisper-base",
                  cache_dir="/seed/cache/faster-whisper")
PY
# Drop GGUF download metadata, keep HF cache layout, and fail build if the seed is empty.
RUN rm -rf /seed/mnemosyne/models/.cache \
    && test -f /seed/mnemosyne/models/MiniCPM5-1B-Q4_K_M.gguf \
    && test -d /seed/cache/fastembed/models--qdrant--bge-small-en-v1.5-onnx-q \
    && test -d /seed/cache/faster-whisper/models--Systran--faster-whisper-base

# Thin Vicegerent derivation of upstream Hermes Agent.
FROM nousresearch/hermes-agent:v2026.7.20@sha256:f7b35053268f532f98955195c909f15a230470fbcbdacaa9fdecb95707dad04a

LABEL org.opencontainers.image.title="vicegerent-hermes-agent" \
      org.opencontainers.image.description="Vicegerent derivation of nousresearch/hermes-agent" \
      org.opencontainers.image.source="https://gitlab.hahomelabs.com/jchristensen/vicegerent-agents" \
      org.opencontainers.image.base.name="docker.io/nousresearch/hermes-agent:v2026.7.20"

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
# and can't be covered this way. BASH_ENV (not bash.bashrc) is required
# because Hermes' terminal() tool runs plain non-interactive, non-login
# `bash -c` -- the one shell-invocation mode that sources neither
# /etc/profile nor /etc/bash.bashrc, only $BASH_ENV. It's a Docker ENV, so
# every process in the container inherits it, including kubectl exec
# sessions -- no bash.bashrc fallback needed. Accepted tradeoff: external
# pwd resolves symlinks (physical path) where the builtin preserves the
# logical $PWD, and external kill can't take job specs (%1) -- global
# BASH_ENV shadowing means every bash -c in the sandbox gets this, not just
# interactive sessions.
ENV BASH_ENV=/etc/bash-shadow-builtins.sh
RUN printf 'enable -n "[" echo false kill printf pwd test true\n' > /etc/bash-shadow-builtins.sh \
    && chmod 644 /etc/bash-shadow-builtins.sh

# Bake the Mnemosyne + STT model seed outside /opt/data so the data PVC cannot shadow it.
COPY --from=mnemosyne-models /seed /opt/hermes/mnemosyne-seed
RUN test -f /opt/hermes/mnemosyne-seed/mnemosyne/models/MiniCPM5-1B-Q4_K_M.gguf \
    && test -d /opt/hermes/mnemosyne-seed/cache/fastembed/models--qdrant--bge-small-en-v1.5-onnx-q \
    && test -d /opt/hermes/mnemosyne-seed/cache/faster-whisper/models--Systran--faster-whisper-base \
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
    && apt-get install -y --no-install-recommends iproute2 dnsutils netcat-openbsd tmux vim \
    && rm -rf /var/lib/apt/lists/*

# Runtime Python deps for the egress-locked sandbox; Renovate tracks requirements.txt.
COPY requirements.txt /tmp/requirements.txt
RUN uv pip install --python /opt/hermes/.venv/bin/python --no-cache -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt

# Patch: mnemosyne-memory (pulled in by mnemosyne-hermes[all]) ships a stray
# top-level tools/ directory of its own dev/benchmark scripts with no
# __init__.py, which Python treats as a namespace package and which then
# shadows Hermes Agent's real `tools` package for any process whose cwd
# isn't /opt/hermes -- i.e. every real session, since terminal.cwd is
# /workspace. This breaks tools/file_tools.py's lazy `from tools import
# check_file_requirements` import, making `hermes doctor` falsely report
# "file (system dependency not met)". Remove once mnemosyne-memory stops
# shipping tools/ as a floating top-level directory in its wheel.
COPY patches/0019-remove-mnemosyne-tools-namespace-collision.py /tmp/0019-remove-mnemosyne-tools-namespace-collision.py
RUN /opt/hermes/.venv/bin/python /tmp/0019-remove-mnemosyne-tools-namespace-collision.py \
    && rm /tmp/0019-remove-mnemosyne-tools-namespace-collision.py

# Global npm CLIs used by Hermes/coding agents; package.json is the Renovate-tracked source.
COPY package.json /tmp/npm/package.json
RUN cd /tmp/npm \
    && npm install --global --no-fund --no-audit \
        $(node -p "Object.entries(require('./package.json').dependencies).map(([k,v])=>k+'@'+v).join(' ')") \
    && rm -rf /tmp/npm /root/.npm

# terraform-ls, terragrunt, tirith, jq, yq, buf, kustomize, kubeconform,
# cerbos, helm, bazelisk/buildifier/buildozer, and rtk are all baked in the
# independent `tools` stage above (decoupled from this base image so a
# Renovate bump here doesn't invalidate any of those downloads). Only
# runtime-required apt packages (openjdk for keytool) and env vars stay here.
COPY --from=tools /usr/local/bin/terraform-ls /usr/local/bin/terraform-ls
COPY --from=tools /usr/local/bin/terragrunt /usr/local/bin/terragrunt

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

COPY --from=tools /usr/local/bin/tirith /usr/local/bin/tirith
RUN tirith --version

# cluster/cloud CLIs belong in MCP servers, not the sandbox image: shadow
# their conventional binary names with a stub that redirects an agent to the
# vMCP server instead of a bare "command not found". binaries.txt is the
# single list to extend when a new toolhive-servers.json backend gets an
# obvious CLI equivalent.
COPY vmcp-redirect/stub.sh /usr/local/bin/vmcp-redirect-stub
RUN chmod +x /usr/local/bin/vmcp-redirect-stub
COPY vmcp-redirect/binaries.txt /tmp/vmcp-redirect-binaries.txt
RUN while IFS= read -r name; do \
        name="${name%%#*}"; \
        name="$(echo "$name" | xargs)"; \
        [ -z "$name" ] && continue; \
        ln -sf /usr/local/bin/vmcp-redirect-stub "/usr/local/bin/${name}"; \
    done < /tmp/vmcp-redirect-binaries.txt \
    && rm /tmp/vmcp-redirect-binaries.txt \
    && kubectl 2>&1 | grep -q "use the vMCP server instead"

COPY --from=tools /usr/local/bin/jq /usr/local/bin/jq
COPY --from=tools /usr/local/bin/yq /usr/local/bin/yq
RUN jq --version && yq --version

COPY --from=tools /usr/local/bin/buf /usr/local/bin/buf
RUN buf --version

# kustomize + kubeconform + cerbos + helm so validate.sh and pre-commit hooks
# work offline in the egress-locked sandbox without downloading at runtime.
COPY --from=tools /usr/local/bin/kustomize /usr/local/bin/kustomize
COPY --from=tools /usr/local/bin/kubeconform /usr/local/bin/kubeconform
COPY --from=tools /usr/local/bin/cerbos /usr/local/bin/cerbos
COPY --from=tools /usr/local/bin/helm /usr/local/bin/helm
RUN kustomize version && kubeconform -v && cerbos --version && helm version

# bazelisk + buildifier/buildozer so agents can build/run Bazel targets in
# repos that use Bazel (e.g. k8s-manifests' helm_charts renders), and
# reformat/edit BUILD files, entirely offline in the egress-locked sandbox.
# The pre-warmed bazelisk cache (BAZELISK_HOME, baked in the `tools` stage
# against BAZEL_PINNED_VERSION) is copied verbatim so the sandbox never has
# to dial releases.bazel.build at runtime for that one pinned version.
#
# keytool (headless JRE) so _sandbox.tpl can build a Bazel-trusted PKCS12
# truststore at pod-start. This apt package is the one thing here that
# genuinely depends on the base image's package repos, so it stays in the
# main stage.
RUN apt-get update \
    && apt-get install -y --no-install-recommends openjdk-21-jre-headless \
    && rm -rf /var/lib/apt/lists/* \
    && keytool -help >/dev/null

ENV BAZELISK_HOME=/opt/hermes/.cache/bazelisk
COPY --from=tools /usr/local/bin/bazelisk /usr/local/bin/bazelisk
COPY --from=tools /usr/local/bin/buildifier /usr/local/bin/buildifier
COPY --from=tools /usr/local/bin/buildozer /usr/local/bin/buildozer
COPY --from=tools "${BAZELISK_HOME}" "${BAZELISK_HOME}"
RUN ln -s /usr/local/bin/bazelisk /usr/local/bin/bazel \
    && buildifier --version \
    && buildozer -version \
    && test -n "$(find "${BAZELISK_HOME}" -type f -name 'bazel-real' -o -type f -iname 'bazel*' 2>/dev/null | head -1)" \
    && chmod -R a+rX "${BAZELISK_HOME}"

COPY --from=tools /usr/local/bin/rtk /usr/local/bin/rtk
RUN rtk --version

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

# Patch hermes_cli/runtime_provider.py's native-Anthropic resolution (both
# the pool-entry path AND the fallback branch) so a gateway-routed
# placeholder token (this platform's ANTHROPIC_API_KEY="none", needed to  # pragma: allowlist secret
# keep the canonical anthropic/openai-api providers from falsely appearing
# "configured" per the HAH-112 fix) doesn't make the desktop's
# setup.runtime_check RPC report "No usable credentials found for anthropic"
# even though real requests work fine (agentgateway supplies the actual key
# downstream). The pool-entry path is the one actually exercised on this
# platform (agent/credential_pool.py's _seed_from_env() unconditionally
# seeds ANTHROPIC_API_KEY into the persisted pool -- "none" is truthy, no
# placeholder check -- so resolve_runtime_provider() always takes that path
# here); the fallback branch is patched too for deployments where the pool
# has no usable "anthropic" entry. Normalizes such tokens to the existing
# "no-key-required" sentinel setup.runtime_check already allowlists.
# Verified live: without this patch, resolve_runtime_provider() returns
# api_key="none" (not "no-key-required"), reproducing the exact false  # pragma: allowlist secret
# warning. Depends on 0014 (needs cfg_base_url / _anthropic_base_url_override_ok
# / _resolve_anthropic_base_url_override already in place). Remove once
# upstream either does this normalization itself or teaches
# setup.runtime_check that a trusted gateway override doesn't need a
# locally-usable secret.
COPY patches/0020-anthropic-gateway-placeholder-credential.py /tmp/0020-anthropic-gateway-placeholder-credential.py
RUN /opt/hermes/.venv/bin/python /tmp/0020-anthropic-gateway-placeholder-credential.py
RUN rm -f /tmp/0020-anthropic-gateway-placeholder-credential.py

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

# Lower tools/mcp_tool.py's parked-MCP-server self-probe cadence from
# upstream's default 300s to 60s. Upstream now natively self-heals a parked
# server (reconnect budget exhausted, tools deregistered) on a periodic
# probe -- this repo's old patch 0017 hand-added the same behavior and was
# removed on upgrade to v2026.7.7.2 since upstream's own fix made it dead
# code (its anchor no longer matches the refactored function at all). But
# upstream's constant is a hardcoded 300s with no per-server config knob
# (0017's version was configurable, default 30s). This deployment has
# exactly one MCP server (vmcp, ToolHive's aggregator -- see 0009's patch
# docstring), so a flat 60s self-probe interval is a reasonable middle
# ground: fast enough that a vMCP flap self-heals within a minute, slow
# enough not to busy-loop a genuinely-down backend. Remove once upstream
# exposes mcp_servers.<name>.parked_retry_interval as a config option.
COPY patches/0022-mcp-parked-retry-interval-60s.py /tmp/0022-mcp-parked-retry-interval-60s.py
RUN /opt/hermes/.venv/bin/python /tmp/0022-mcp-parked-retry-interval-60s.py
RUN rm -f /tmp/0022-mcp-parked-retry-interval-60s.py

# Raise tools/mcp_tool.py's MCP circuit-breaker strike threshold from
# upstream's 3 to 15 consecutive real transport/auth failures before a
# backend is parked for the 60s cooldown. With 0009 already excluding
# business/isError responses and scoping the breaker per vMCP backend, 3
# genuine blips is too twitchy for the single host-vMCP-over-mTLS path;
# 15 keeps the #10447 anti-burn-loop protection while tolerating transient
# flakiness. Remove once upstream exposes the threshold as a config option.
COPY patches/0023-mcp-circuit-breaker-threshold-15.py /tmp/0023-mcp-circuit-breaker-threshold-15.py
RUN /opt/hermes/.venv/bin/python /tmp/0023-mcp-circuit-breaker-threshold-15.py
RUN rm -f /tmp/0023-mcp-circuit-breaker-threshold-15.py

# Unwrap the swallowed sub-exception in tools/mcp_tool.py's "connection lost"
# reconnect-loop warning. anyio TaskGroups (used by the MCP SDK's streamable-
# HTTP/stdio transports) wrap any single inner-task failure in a
# BaseExceptionGroup/ExceptionGroup whose default str() is just "unhandled
# errors in a TaskGroup (N sub-exception(s))" -- confirmed live, 564
# occurrences of this exact opaque message in errors.log since 2026-07-04
# with zero diagnostic detail on what actually failed. Purely a
# logging/diagnostics change -- no reconnect timing, backoff, or control
# flow is touched. Remove once upstream unwraps ExceptionGroups in this log
# line itself.
COPY patches/0024-mcp-connection-lost-diagnostics.py /tmp/0024-mcp-connection-lost-diagnostics.py
RUN /opt/hermes/.venv/bin/python /tmp/0024-mcp-connection-lost-diagnostics.py
RUN rm -f /tmp/0024-mcp-connection-lost-diagnostics.py

# Fix tools/skill_manager_tool.py's background-curator read-before-write
# tracking: it used a ContextVar holding an immutable frozenset, rebound on
# every mark_background_review_skill_read() call. execute_tool_calls_concurrent
# dispatches parallel-safe tools (skill_view included) onto worker threads via
# contextvars.copy_context(), and a ContextVar rebind inside one of those
# copies is invisible to sibling copies and the parent thread -- so a
# skill_view mark made in one worker was never visible to the
# skill_manage(patch) call that follows, causing the background curator's
# read-before-write guard to fire on literally every patch/edit/delete call.
# Confirmed live: 223/223 background-curator write failures over 10+ days,
# 100% failure rate, all with "the current SKILL.md content has not been
# loaded in this review turn" despite skill_view having in fact just been
# called. Fix: hold a plain mutable set instead, bound once per curator run
# and mutated in place thereafter -- copy_context() copies the variable
# binding (a reference), so every fork after that one bind shares the same
# underlying set. Remove once upstream fixes this ContextVar/copy_context
# interaction.
COPY patches/0025-curator-skill-read-marks-contextvar-isolation.py /tmp/0025-curator-skill-read-marks-contextvar-isolation.py
RUN /opt/hermes/.venv/bin/python /tmp/0025-curator-skill-read-marks-contextvar-isolation.py
RUN rm -f /tmp/0025-curator-skill-read-marks-contextvar-isolation.py

# A parked MCP server (reconnect budget exhausted) used to have its tools
# deregistered from the global registry, so the model's tool list silently
# shrank/grew across turns whenever a server had a rough patch. Keep tools
# registered while parked instead: shutdown() still deregisters them (a
# genuine, permanent teardown), but the two park-while-still-alive paths in
# run() no longer do. A tool call against a parked server now returns a
# clean, human-readable "reconnecting, try again later" error from
# _make_tool_handler instead of a registry-lookup failure -- without ever
# calling _signal_reconnect, so the existing 60s self-probe cadence (patch
# 0022) is completely unaffected by tool-call traffic.
COPY patches/0027-mcp-parked-tools-stay-registered.py /tmp/0027-mcp-parked-tools-stay-registered.py
RUN /opt/hermes/.venv/bin/python /tmp/0027-mcp-parked-tools-stay-registered.py
RUN rm -f /tmp/0027-mcp-parked-tools-stay-registered.py

# Bundled skill files are installed 444 (root-owned in the image); make them
# 644 so skill_manager_tool can patch them after copytree seeds /opt/data/skills.
RUN find /opt/hermes/skills -name "SKILL.md" -exec chmod 644 {} +

# gateway/runtime_footer.py only supports model/context_pct/cwd fields; add an
# "effort" field so display.runtime_footer.fields can show the current
# reasoning-effort level. Single patch touching two files: adds the field to
# runtime_footer.py itself, then threads the live value from
# gateway/run.py's self._reasoning_config into the build_footer_line() call
# site. Remove once upstream Hermes's runtime footer supports an effort
# field itself.
COPY patches/0028-runtime-footer-effort-field.py /tmp/0028-runtime-footer-effort-field.py
RUN /opt/hermes/.venv/bin/python /tmp/0028-runtime-footer-effort-field.py
RUN rm -f /tmp/0028-runtime-footer-effort-field.py

# file_mutation_verifier and memory_notifications are read as flat
# global-only settings today (run_agent.py / gateway/run.py), unlike every
# other per-platform display toggle in this codebase. Teach both to honor
# display.platforms.<platform>.<key> overrides (falls through to the
# existing global setting unchanged when no override is set). Remove once
# upstream Hermes routes both through gateway/display_config.py's
# resolve_display_setting() itself.
COPY patches/0030-per-platform-verifier-and-memory-notifications.py /tmp/0030-per-platform-verifier-and-memory-notifications.py
RUN /opt/hermes/.venv/bin/python /tmp/0030-per-platform-verifier-and-memory-notifications.py
RUN rm -f /tmp/0030-per-platform-verifier-and-memory-notifications.py

# Fix two independent bugs in Slack's _handle_slack_message message-text
# assembly that both silently corrupt in-thread/bang-command handling:
# (1) prepending the fetched thread-context summary directly onto the
# trigger message's `text` made `event.text` no longer start with "/", so
# every `event.get_command()` check silently returned None and the
# command dispatched as plain chat instead -- routed through
# MessageEvent.channel_context (already consumed correctly, and late, by
# gateway/run.py) instead. (2) the quoted/forwarded-blocks merge deduped
# against the possibly !->/ rewritten `text` instead of the raw message
# (blocks always mirror the raw, un-rewritten text), silently duplicating
# every bang-command's raw text onto `text` and corrupting command
# argument parsing (e.g. `/reasoning high !reasoning high` instead of
# `/reasoning high`). Remove once upstream Hermes's Slack adapter routes
# thread-context through channel_context and its blocks-merge dedup
# compares against raw text.
COPY patches/0031-slack-thread-context-command-detection.py /tmp/0031-slack-thread-context-command-detection.py
RUN /opt/hermes/.venv/bin/python /tmp/0031-slack-thread-context-command-detection.py
RUN rm -f /tmp/0031-slack-thread-context-command-detection.py

# Fix agentburn's by_model cost breakdown: it only ever read `sessions`
# (one row per session, model = the session's STARTING model), so a session
# that switched models mid-conversation via /model had its entire cost
# credited/blamed on whichever model it started with -- silently dropping
# every dollar spent on a model switched to later. Loads Hermes'
# session_model_usage table (per-(session, model) cost/token split) and uses
# it for by_model whenever a session has rows there, falling back to the old
# whole-session attribution otherwise. Remove once agentburn's own Hermes
# adapter reads session_model_usage natively.
COPY patches/0034-agentburn-per-model-cost-attribution.py /tmp/0034-agentburn-per-model-cost-attribution.py
RUN /opt/hermes/.venv/bin/python /tmp/0034-agentburn-per-model-cost-attribution.py
RUN rm -f /tmp/0034-agentburn-per-model-cost-attribution.py

# Render markdown pipe-tables as fenced, aligned-monospace text in
# format_message() -- the flat-text-only table rendering, now that
# slack.extra.rich_blocks is disabled entirely (its Block Kit table block
# carried its own "Show more"/"Show less" collapse chrome on every
# surrounding section, worse than the raw-pipe rendering it replaced;
# patch 0033's table-only Block Kit rendering was removed for the same
# reason -- see this repo's MR history). Remove once Slack's own client
# stops applying per-block collapse chrome to Block Kit sections, or a
# native non-collapsing table primitive becomes available.
COPY patches/0035-slack-flat-text-table-rendering.py /tmp/0035-slack-flat-text-table-rendering.py
RUN /opt/hermes/.venv/bin/python /tmp/0035-slack-flat-text-table-rendering.py
RUN rm -f /tmp/0035-slack-flat-text-table-rendering.py

# Suppress the Slack runtime footer (model/context/effort/cwd) outside DMs.
# display.platforms.slack.runtime_footer.enabled is on unconditionally in
# this chart's default, which is fine 1:1 but leaks internal runtime state
# into any shared channel the bot is invited into. Gates the single footer
# build call site on source.chat_id starting with "D" (Slack's own DM
# channel-ID prefix); every other platform is unaffected. Remove once
# upstream Hermes's runtime_footer gains native per-channel-type scoping.
COPY patches/0036-slack-runtime-footer-dm-only.py /tmp/0036-slack-runtime-footer-dm-only.py
RUN /opt/hermes/.venv/bin/python /tmp/0036-slack-runtime-footer-dm-only.py
RUN rm -f /tmp/0036-slack-runtime-footer-dm-only.py

# jiter (the Rust JSON parser the Anthropic SDK's streaming snapshot builder
# uses to reassemble tool-call input_json_delta chunks) raises a bare
# ValueError on malformed JSON that is NOT a json.JSONDecodeError subclass,
# so the classifier's existing "malformed response body -> retryable"
# exclusion never catches it. Misclassified as a local programming bug, it
# triggers an immediate non-retryable-error -> fallback-provider switch
# instead of a same-provider retry, even when the primary call actually
# returned HTTP 200 (live incident: agentgateway logged a clean 200/3932ms
# response that Hermes then failed to json-parse). See the patch file for
# the full writeup. Remove once upstream checks jiter's actual error shape
# instead of exception-type identity.
COPY patches/0037-jiter-streaming-parse-error-retryable.py /tmp/0037-jiter-streaming-parse-error-retryable.py
RUN /opt/hermes/.venv/bin/python /tmp/0037-jiter-streaming-parse-error-retryable.py
RUN rm -f /tmp/0037-jiter-streaming-parse-error-retryable.py

# Replace the runtime footer's cwd with real spend visibility. TERMINAL_CWD is
# copied once from terminal.cwd at gateway startup and never follows /new,
# session/turn boundaries, or terminal-tool cd calls, so displaying it falsely
# implies a relationship to the active work. Snapshot the cached agent's
# running session cost and wall-clock time around each turn to render the turn
# cost, session total, and last-turn duration. Applied after 0028 and 0036
# because it preserves their effort field and Slack DM-only/italics behavior
# while updating their shared call site. Remove once upstream tracks per-turn
# cost and duration and exposes them in the footer.
COPY patches/0039-runtime-footer-cost-field.py /tmp/0039-runtime-footer-cost-field.py
RUN /opt/hermes/.venv/bin/python /tmp/0039-runtime-footer-cost-field.py
RUN rm -f /tmp/0039-runtime-footer-cost-field.py

# The custom provider profile (Ollama/vLLM/llama.cpp/GLM-ARK/gpt-5.4-via-
# agentgateway -- anything registered as provider: custom) unconditionally
# emitted extra_body.think=False whenever reasoning was disabled. The
# OpenAI SDK flattens extra_body into literal top-level JSON keys, and
# schema-strict OpenAI-compatible endpoints (real OpenAI, and proxies like
# agentgateway that pass extra_body straight through) hard-400 on the
# unrecognized "think" field -- while the field was already dead weight
# for its intended Ollama target (Ollama's own docs: /v1/chat/completions
# ignores think, only /api/chat honours it, and Hermes never calls the
# latter). Live incident: the SAME turn that MR !604's reasoning_effort
# fix was meant to make survivable still 400'd on fallback, this time on
# "think" instead. Remove once upstream scopes the think field to real
# Ollama endpoints only (e.g. via is_local_endpoint), or Ollama's own
# /v1/chat/completions starts honouring it.
COPY patches/0040-custom-provider-drop-unconditional-think-field.py /tmp/0040-custom-provider-drop-unconditional-think-field.py
RUN /opt/hermes/.venv/bin/python /tmp/0040-custom-provider-drop-unconditional-think-field.py
RUN rm -f /tmp/0040-custom-provider-drop-unconditional-think-field.py
