FROM node:24-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
      curl unzip ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# renovate: datasource=github-tags depName=aws/aws-cli
ARG AWSCLI_VERSION=2.36.5
RUN ARCH="$(dpkg --print-architecture)" \
    && case "$ARCH" in \
         amd64) AWS_ARCH=x86_64 ;; \
         arm64) AWS_ARCH=aarch64 ;; \
         *) echo "unsupported arch: $ARCH" >&2; exit 1 ;; \
       esac \
    && curl -sSL "https://awscli.amazonaws.com/awscli-exe-linux-${AWS_ARCH}-${AWSCLI_VERSION}.zip" -o /tmp/awscliv2.zip \
    && unzip -q /tmp/awscliv2.zip -d /tmp \
    && /tmp/aws/install \
    && rm -rf /tmp/awscliv2.zip /tmp/aws

# renovate: datasource=npm depName=kubernetes-mcp-server
ARG KUBERNETES_MCP_SERVER_VERSION=0.0.65
RUN npm install -g "kubernetes-mcp-server@${KUBERNETES_MCP_SERVER_VERSION}" \
    && npm cache clean --force

RUN useradd -u 10001 -m mcp
USER 10001

ENTRYPOINT ["kubernetes-mcp-server"]
