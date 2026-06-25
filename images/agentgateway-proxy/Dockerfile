# syntax=docker/dockerfile:1.11
# Vicegerent build of the upstream agentgateway data plane, carrying a single
# source patch until it lands upstream: a request-phase mcpGuardrails rejection
# is returned as HTTP 200 + JSON-RPC error (not 400), so MCP clients surface a
# policy deny as a tool error instead of tearing down the session. See
# patches/0001-guardrail-reject-200.patch and README.md.
#
# Built on a laptop with internet, pushed to harbor.hahomelabs.com/vicegerent,
# pulled from there by the egress-locked cluster. UI feature is intentionally
# omitted (the proxy does not serve the dashboard here), so the upstream node
# build stage is dropped.

# renovate: datasource=github-releases depName=agentgateway/agentgateway
ARG AGW_VERSION=v1.3.1

FROM docker.io/library/rust:1.96.0-trixie AS builder
ARG AGW_VERSION
ARG TARGETARCH

RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates && rm -rf /var/lib/apt/lists/*

WORKDIR /app
RUN git clone --depth 1 --branch "${AGW_VERSION}" https://github.com/agentgateway/agentgateway.git .

# Carry our patch(es). git apply --verbose hard-fails the build if a patch ever
# stops applying against a new AGW_VERSION — that is the signal to re-verify or
# drop it, never a silent miscompile.
COPY patches/ /patches/
RUN git apply --verbose /patches/*.patch

# Match upstream's glibc target + arm64 jemalloc 64KB-page build so the image
# runs on any host page size <= 64KB. UI feature omitted (default features only).
RUN <<EOF
set -eu
if [ "${TARGETARCH}" = "arm64" ]; then
  TARGET=aarch64-unknown-linux-gnu
  export JEMALLOC_SYS_WITH_LG_PAGE=16
else
  TARGET=x86_64-unknown-linux-gnu
fi
rustup target add "${TARGET}"
export VERSION="${AGW_VERSION}"
export GIT_REVISION="$(git rev-parse HEAD)"
cargo build --release --bin agentgateway --target "${TARGET}"
mkdir /out
mv "/app/target/${TARGET}/release/agentgateway" /out/agentgateway
/out/agentgateway --version
EOF

FROM cgr.dev/chainguard/glibc-dynamic AS runner
ENV AGENTGATEWAY_ENV=container
WORKDIR /
COPY --from=builder /out/agentgateway /app/agentgateway

LABEL org.opencontainers.image.title="vicegerent-agentgateway" \
      org.opencontainers.image.description="Vicegerent build of agentgateway/agentgateway carrying the request-phase guardrail 200 patch" \
      org.opencontainers.image.source="https://gitlab.hahomelabs.com/jchristensen/vicegerent-agents" \
      org.opencontainers.image.base.name="https://github.com/agentgateway/agentgateway"

ENTRYPOINT ["/app/agentgateway"]
