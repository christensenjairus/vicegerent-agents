# syntax=docker/dockerfile:1.25
# Upstream agentgateway with request-phase guardrail rejects returned as JSON-RPC errors; UI omitted.

# renovate: datasource=github-releases depName=agentgateway/agentgateway
ARG AGW_VERSION=v1.3.1

FROM docker.io/library/rust:1.96.1-trixie AS builder
ARG AGW_VERSION
ARG TARGETARCH

RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates && rm -rf /var/lib/apt/lists/*

WORKDIR /app
RUN git clone --depth 1 --branch "${AGW_VERSION}" https://github.com/agentgateway/agentgateway.git .

# Fail build when carried patches stop applying to AGW_VERSION.
COPY patches/ /patches/
RUN git apply --verbose /patches/*.patch

# Keep upstream glibc target; arm64 needs jemalloc 64KB pages.
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
