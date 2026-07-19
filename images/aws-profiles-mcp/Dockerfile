# renovate: datasource=pypi depName=fastmcp
ARG FASTMCP_VERSION=3.4.4
FROM python:3.14-slim
ARG FASTMCP_VERSION
RUN pip install --no-cache-dir "fastmcp==${FASTMCP_VERSION}"
COPY server.py /app/server.py
WORKDIR /app
# Non-root: this server only reads the mounted, read-only ~/.aws/config.
RUN useradd -u 10001 -m app
USER 10001
ENTRYPOINT ["python", "server.py"]
