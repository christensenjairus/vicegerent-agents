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

# Thin Vicegerent derivation of the upstream Hermes Agent image.
# Built on a laptop with internet, pushed to harbor.hahomelabs.com/vicegerent,
# and pulled from there by the egress-locked sandbox. Remaining bakes (mnemosyne
# + MiniCPM gguf, hermes-lcm, yq/jq/pygount, rtk-hermes) land as their own
# layers in follow-up cards.
FROM nousresearch/hermes-agent:v2026.6.19@sha256:9f367c7756ef087661a361536a89f438d57a122b958dc23d82d456b1433e6e9e

LABEL org.opencontainers.image.title="vicegerent-hermes-agent" \
      org.opencontainers.image.description="Vicegerent derivation of nousresearch/hermes-agent" \
      org.opencontainers.image.source="https://gitlab.hahomelabs.com/jchristensen/vicegerent-agents" \
      org.opencontainers.image.base.name="docker.io/nousresearch/hermes-agent:v2026.6.19"

# shellcheck: bash-language-server delegates ALL diagnostics to it. Without it
# the server runs but reports nothing — a silent no-op. apt package in trixie.
RUN apt-get update \
    && apt-get install -y --no-install-recommends shellcheck \
    && rm -rf /var/lib/apt/lists/*

# Python packages the egress-locked sandbox can't install at runtime. Add a
# package by editing requirements.txt; Renovate tracks it natively, no
# renovate.json change. Installed into the baked venv (on PATH, not /opt/data),
# so no init-container seeding is needed.
COPY requirements.txt /tmp/requirements.txt
RUN uv pip install --python /opt/hermes/.venv/bin/python --no-cache -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt

# npm-based LSP servers. Hermes launches these by binary name for semantic
# diagnostics but cannot npm-install them behind the egress lock. Installed
# globally into /usr/local/bin (on PATH). Versions come from package.json so it
# stays the single Renovate-tracked source of truth; add a server = add a line.
COPY package.json /tmp/lsp/package.json
RUN cd /tmp/lsp \
    && npm install --global --no-fund --no-audit \
        $(node -p "Object.entries(require('./package.json').dependencies).map(([k,v])=>k+'@'+v).join(' ')") \
    && rm -rf /tmp/lsp /root/.npm

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
