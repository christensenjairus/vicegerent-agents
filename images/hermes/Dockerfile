# Thin Vicegerent derivation of the upstream Hermes Agent image.
# Built on a laptop with internet, pushed to harbor.hahomelabs.com/vicegerent,
# and pulled from there by the egress-locked sandbox. Remaining bakes (mnemosyne
# + MiniCPM gguf, hermes-lcm, LSP servers, yq/jq/pygount, rtk-hermes) land as
# their own layers in follow-up cards.
FROM nousresearch/hermes-agent:v2026.6.19@sha256:9f367c7756ef087661a361536a89f438d57a122b958dc23d82d456b1433e6e9e

LABEL org.opencontainers.image.title="vicegerent-hermes-agent" \
      org.opencontainers.image.description="Vicegerent derivation of nousresearch/hermes-agent" \
      org.opencontainers.image.source="https://gitlab.hahomelabs.com/jchristensen/vicegerent-agents" \
      org.opencontainers.image.base.name="docker.io/nousresearch/hermes-agent:v2026.6.19"

# Python packages the egress-locked sandbox can't install at runtime. Add a
# package by editing requirements.txt; Renovate tracks it natively, no
# renovate.json change. Installed into the baked venv (on PATH, not /opt/data),
# so no init-container seeding is needed.
COPY requirements.txt /tmp/requirements.txt
RUN uv pip install --python /opt/hermes/.venv/bin/python --no-cache -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt
