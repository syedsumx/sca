# syntax=docker/dockerfile:1.7
# DHE remote ingestion agent — runs inside the customer VPC, outbound-only.
#   docker build -f docker/agent.Dockerfile \
#     --build-arg INGEST_EXTRAS="postgres,snowflake,bigquery" -t dhe/remote-agent:0.1.0 .
#
# The image is designed for readOnlyRootFilesystem: everything it writes goes to
# $DHE_AGENT_STATE_DIR (keys, spool) and $TMPDIR, both expected to be writable volumes.

ARG PYTHON_VERSION=3.12

FROM python:${PYTHON_VERSION}-slim AS build
# Comma-separated acryl-datahub plugin extras for the sources this agent pool must reach.
ARG INGEST_EXTRAS=postgres
ARG DATAHUB_VERSION=1.3.0
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential libpq-dev \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /src
COPY pyproject.toml README.md* ./
COPY src ./src
RUN python -m venv /opt/dhe \
 && /opt/dhe/bin/pip install --upgrade pip wheel \
 && /opt/dhe/bin/pip install ".[agent]" "acryl-datahub[${INGEST_EXTRAS}]>=${DATAHUB_VERSION}"

FROM python:${PYTHON_VERSION}-slim AS runtime
LABEL org.opencontainers.image.title="dhe-remote-agent" \
      org.opencontainers.image.description="DHE in-VPC remote ingestion agent (outbound-only, E2E encrypted)"
RUN apt-get update \
 && apt-get install -y --no-install-recommends libpq5 ca-certificates tini \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd --system --gid 10001 dhe \
 && useradd --system --uid 10001 --gid dhe --no-create-home --shell /usr/sbin/nologin dhe \
 && mkdir -p /var/lib/dhe-agent /tmp/dhe && chown dhe:dhe /var/lib/dhe-agent /tmp/dhe
COPY --from=build /opt/dhe /opt/dhe
ENV PATH="/opt/dhe/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOME=/tmp/dhe \
    TMPDIR=/tmp/dhe \
    DATAHUB_TELEMETRY_ENABLED=false \
    DHE_AGENT_STATE_DIR=/var/lib/dhe-agent
USER 10001:10001
VOLUME ["/var/lib/dhe-agent", "/tmp/dhe"]
# No EXPOSE: the agent never listens on a port.
ENTRYPOINT ["/usr/bin/tini", "--", "dhe", "agent"]
CMD ["run", "--config", "/etc/dhe-agent/agent.yml"]
