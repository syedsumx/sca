# syntax=docker/dockerfile:1.7
# Aegis control-plane image: one image, many entry points (aegis api | pdp | gateway | scheduler |
# actions | mcp). Build context: the aegis/ directory.
#   docker build -f docker/Dockerfile -t aegis/platform:0.1.0 .
#   (or deploy/build-images.sh, which also handles proxies and registry mirrors)
#
# No OS packages are installed: every dependency ships as a manylinux wheel (psycopg[binary],
# cryptography, uvloop, confluent-kafka), PID 1 is dumb-init from PyPI and health checks use
# Python. This keeps the image small and its CVE surface to the base image only.
#
# Behind a TLS-intercepting proxy pass its CA as a BuildKit secret (it is never stored in a layer):
#   --secret id=extra_ca,src=/path/to/ca.crt --build-arg HTTPS_PROXY=...

ARG BASE_IMAGE=python:3.12-slim

FROM ${BASE_IMAGE} AS build
ARG EXTRAS=all
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /src
COPY pyproject.toml README.md* ./
COPY src ./src
RUN --mount=type=secret,id=extra_ca,required=false \
    if [ -f /run/secrets/extra_ca ]; then export PIP_CERT=/run/secrets/extra_ca; fi \
 && python -m venv /opt/aegis \
 && /opt/aegis/bin/pip install --upgrade pip wheel \
 && /opt/aegis/bin/pip install ".[${EXTRAS}]" dumb-init

FROM ${BASE_IMAGE} AS runtime
LABEL org.opencontainers.image.title="aegis" \
      org.opencontainers.image.description="Aegis control plane: DQ engine, PDP, agent gateway, AI/MCP" \
      org.opencontainers.image.licenses="Apache-2.0"
RUN groupadd --system --gid 10001 aegis \
 && useradd --system --uid 10001 --gid aegis --home-dir /home/aegis --create-home --shell /usr/sbin/nologin aegis \
 && mkdir -p /var/lib/aegis/keys && chown -R aegis:aegis /var/lib/aegis
COPY --from=build /opt/aegis /opt/aegis
COPY config /etc/aegis/config
COPY docker/healthcheck.py /usr/local/bin/aegis-healthcheck
ENV PATH="/opt/aegis/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    AEGIS_POLICY_DIR=/etc/aegis/config/policies \
    AEGIS_GATEWAY_KEY_DIR=/var/lib/aegis/keys
USER 10001:10001
WORKDIR /home/aegis
EXPOSE 8800 8810 8820 8830
ENTRYPOINT ["/opt/aegis/bin/dumb-init", "--", "aegis"]
CMD ["api", "--host", "0.0.0.0", "--port", "8800"]
