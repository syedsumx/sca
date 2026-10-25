# syntax=docker/dockerfile:1.7
# DHE control-plane image: one image, many entry points (dhe api | pdp | gateway | scheduler |
# actions | mcp). Build context: the datahub-enterprise/ directory.
#   docker build -f docker/Dockerfile -t dhe/datahub-enterprise:0.1.0 .
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
 && python -m venv /opt/dhe \
 && /opt/dhe/bin/pip install --upgrade pip wheel \
 && /opt/dhe/bin/pip install ".[${EXTRAS}]" dumb-init

FROM ${BASE_IMAGE} AS runtime
LABEL org.opencontainers.image.title="datahub-enterprise" \
      org.opencontainers.image.description="DHE control plane: DQ engine, PDP, agent gateway, AI/MCP" \
      org.opencontainers.image.licenses="Apache-2.0"
RUN groupadd --system --gid 10001 dhe \
 && useradd --system --uid 10001 --gid dhe --home-dir /home/dhe --create-home --shell /usr/sbin/nologin dhe \
 && mkdir -p /var/lib/dhe/keys && chown -R dhe:dhe /var/lib/dhe
COPY --from=build /opt/dhe /opt/dhe
COPY config /etc/dhe/config
COPY docker/healthcheck.py /usr/local/bin/dhe-healthcheck
ENV PATH="/opt/dhe/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DHE_POLICY_DIR=/etc/dhe/config/policies \
    DHE_GATEWAY_KEY_DIR=/var/lib/dhe/keys
USER 10001:10001
WORKDIR /home/dhe
EXPOSE 8800 8810 8820 8830
ENTRYPOINT ["/opt/dhe/bin/dumb-init", "--", "dhe"]
CMD ["api", "--host", "0.0.0.0", "--port", "8800"]
