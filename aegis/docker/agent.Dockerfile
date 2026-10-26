# syntax=docker/dockerfile:1.7
# Aegis remote ingestion agent — runs inside the customer VPC, outbound-only.
#   docker build -f docker/agent.Dockerfile \
#     --build-arg INGEST_EXTRAS="postgres,snowflake,bigquery" -t aegis/remote-agent:0.1.0 .
#
# The image is designed for readOnlyRootFilesystem: everything it writes goes to
# $AEGIS_AGENT_STATE_DIR (keys, spool) and $TMPDIR, both expected to be writable volumes.
# Like the control-plane image it installs no OS packages (see docker/Dockerfile).

ARG BASE_IMAGE=python:3.12-slim

FROM ${BASE_IMAGE} AS build
# Comma-separated acryl-datahub plugin extras for the sources this agent pool must reach.
ARG INGEST_EXTRAS=postgres
ARG DATAHUB_VERSION=1.3.0
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /src
COPY pyproject.toml README.md* ./
COPY src ./src
RUN --mount=type=secret,id=extra_ca,required=false \
    if [ -f /run/secrets/extra_ca ]; then export PIP_CERT=/run/secrets/extra_ca; fi \
 && python -m venv /opt/aegis \
 && /opt/aegis/bin/pip install --upgrade pip wheel \
 && /opt/aegis/bin/pip install ".[agent]" "acryl-datahub[${INGEST_EXTRAS}]>=${DATAHUB_VERSION}" dumb-init

FROM ${BASE_IMAGE} AS runtime
LABEL org.opencontainers.image.title="aegis-remote-agent" \
      org.opencontainers.image.description="Aegis in-VPC remote ingestion agent (outbound-only, E2E encrypted)"
RUN groupadd --system --gid 10001 aegis \
 && useradd --system --uid 10001 --gid aegis --no-create-home --shell /usr/sbin/nologin aegis \
 && mkdir -p /var/lib/aegis-agent /tmp/aegis && chown aegis:aegis /var/lib/aegis-agent /tmp/aegis
COPY --from=build /opt/aegis /opt/aegis
ENV PATH="/opt/aegis/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOME=/tmp/aegis \
    TMPDIR=/tmp/aegis \
    DATAHUB_TELEMETRY_ENABLED=false \
    AEGIS_AGENT_STATE_DIR=/var/lib/aegis-agent
USER 10001:10001
VOLUME ["/var/lib/aegis-agent", "/tmp/aegis"]
# No EXPOSE: the agent never listens on a port.
ENTRYPOINT ["/opt/aegis/bin/dumb-init", "--", "aegis", "agent"]
CMD ["run", "--config", "/etc/aegis-agent/agent.yml"]
