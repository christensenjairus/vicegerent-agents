# gopls is built in a throwaway stage; GOTOOLCHAIN=auto handles newer Renovate bumps.
# renovate: datasource=go depName=golang.org/x/tools/gopls
ARG GOPLS_VERSION=v0.22.0
FROM golang:1.26 AS gopls
ARG GOPLS_VERSION
RUN GOBIN=/out GOTOOLCHAIN=auto go install "golang.org/x/tools/gopls@${GOPLS_VERSION}"

# Offline Mnemosyne seed for the egress-locked sandbox; preserve HF cache layout.
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
# Drop GGUF download metadata, keep fastembed's HF cache layout, and fail build if the seed is empty.
RUN rm -rf /seed/mnemosyne/models/.cache \
    && test -f /seed/mnemosyne/models/MiniCPM5-1B-Q4_K_M.gguf \
    && test -d /seed/cache/fastembed/models--qdrant--bge-small-en-v1.5-onnx-q

# Thin Vicegerent derivation of upstream Hermes Agent.
FROM nousresearch/hermes-agent:v2027.7.1@sha256:9f367c7756ef087661a361536a89f438d57a122b958dc23d82d456b1433e6e9e

LABEL org.opencontainers.image.title="vicegerent-hermes-agent" \
      org.opencontainers.image.description="Vicegerent derivation of nousresearch/hermes-agent" \
      org.opencontainers.image.source="https://gitlab.hahomelabs.com/jchristensen/vicegerent-agents" \
      org.opencontainers.image.base.name="docker.io/nousresearch/hermes-agent:v2027.7.1"

# Bake the Mnemosyne seed outside /opt/data so the data PVC cannot shadow it.
COPY --from=mnemosyne-models /seed /opt/hermes/mnemosyne-seed
RUN test -f /opt/hermes/mnemosyne-seed/mnemosyne/models/MiniCPM5-1B-Q4_K_M.gguf \
    && test -d /opt/hermes/mnemosyne-seed/cache/fastembed/models--qdrant--bge-small-en-v1.5-onnx-q
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

# Copy gopls only; no Go toolchain in final image.
COPY --from=gopls /out/gopls /usr/local/bin/gopls

# hermes-lcm must live in Hermes' bundled context_engine tree, not ~/.hermes/plugins.
# renovate: datasource=github-releases depName=stephenschoettler/hermes-lcm
ARG HERMES_LCM_VERSION=v0.18.1
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

# Patch tools/mcp_tool.py so the MCP circuit breaker doesn't trip on business
# errors (isError=true relayed as a JSON "error" key) — only real transport/auth
# failures should count toward the 3-strike "server unreachable" breaker.
# Remove once upstream Hermes lands #47918/#47955 (hermes-agent #47851/#11113).
COPY patches/0009-mcp-circuit-breaker-business-errors.py /tmp/0009-mcp-circuit-breaker-business-errors.py
RUN /opt/hermes/.venv/bin/python /tmp/0009-mcp-circuit-breaker-business-errors.py
RUN rm -f /tmp/0009-mcp-circuit-breaker-business-errors.py

# Patch agent/file_safety.py so HERMES_WRITE_SAFE_ROOT supports multiple
# os.pathsep-delimited roots (sandbox.yaml sets it to "/opt/data:/workspace").
# Upstream treated the whole value as one literal path, silently denying
# every write_file/patch call. Remove once upstream fixes this.
COPY patches/0010-write-safe-root-multi-path.py /tmp/0010-write-safe-root-multi-path.py
RUN /opt/hermes/.venv/bin/python /tmp/0010-write-safe-root-multi-path.py
RUN rm -f /tmp/0010-write-safe-root-multi-path.py

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

# Bundled skill files are installed 444 (root-owned in the image); make them
# 644 so skill_manager_tool can patch them after copytree seeds /opt/data/skills.
RUN find /opt/hermes/skills -name "SKILL.md" -exec chmod 644 {} +
