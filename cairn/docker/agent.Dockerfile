# syntax=docker/dockerfile:1.7
# Cairn remote ingestion agent — runs inside the customer VPC, outbound-only.
#   docker build -f docker/agent.Dockerfile \
#     --build-arg INGEST_EXTRAS="postgres,snowflake,bigquery" -t cairn/remote-agent:0.1.0 .
#
# The image is designed for readOnlyRootFilesystem: everything it writes goes to
# $CAIRN_AGENT_STATE_DIR (keys, spool) and $TMPDIR, both expected to be writable volumes.
# Like the control-plane image it installs no OS packages (see docker/Dockerfile).

ARG BASE_IMAGE=python:3.12-slim

FROM ${BASE_IMAGE} AS build
# Comma-separated ingestion-framework extras for the sources this agent pool must reach.
ARG INGEST_EXTRAS=postgres
ARG INGEST_VERSION=1.3.0
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /src
COPY pyproject.toml README.md* ./
COPY src ./src
RUN --mount=type=secret,id=extra_ca,required=false \
    if [ -f /run/secrets/extra_ca ]; then export PIP_CERT=/run/secrets/extra_ca; fi \
 && python -m venv /opt/cairn \
 && /opt/cairn/bin/pip install --upgrade pip wheel \
 && /opt/cairn/bin/pip install ".[agent]" "acryl-datahub[${INGEST_EXTRAS}]>=${INGEST_VERSION}" dumb-init

FROM ${BASE_IMAGE} AS runtime
LABEL org.opencontainers.image.title="cairn-remote-agent" \
      org.opencontainers.image.description="Cairn in-VPC remote ingestion agent (outbound-only, E2E encrypted)"
RUN groupadd --system --gid 10001 cairn \
 && useradd --system --uid 10001 --gid cairn --no-create-home --shell /usr/sbin/nologin cairn \
 && mkdir -p /var/lib/cairn-agent /tmp/cairn && chown cairn:cairn /var/lib/cairn-agent /tmp/cairn
COPY --from=build /opt/cairn /opt/cairn
ENV PATH="/opt/cairn/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOME=/tmp/cairn \
    TMPDIR=/tmp/cairn \
    DATAHUB_TELEMETRY_ENABLED=false \
    CAIRN_AGENT_STATE_DIR=/var/lib/cairn-agent
USER 10001:10001
VOLUME ["/var/lib/cairn-agent", "/tmp/cairn"]
# No EXPOSE: the agent never listens on a port.
ENTRYPOINT ["/opt/cairn/bin/dumb-init", "--", "cairn", "agent"]
CMD ["run", "--config", "/etc/cairn-agent/agent.yml"]
