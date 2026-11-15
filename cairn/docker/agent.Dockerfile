# syntax=docker/dockerfile:1.7
# Cairn remote ingestion agent — runs inside the customer VPC, outbound-only.
#   docker build -f docker/agent.Dockerfile -t cairn/remote-agent:0.1.0 .
# By default the image can run every connector in the gallery (sources/catalog.py; tests/test_gallery_ingestion_e2e.py
# checks the two lists agree and runs the open-source ones for real). A pool that reaches only some sources can be
# built smaller: --build-arg INGEST_EXTRAS="postgres,snowflake".
#
# The image is designed for readOnlyRootFilesystem: everything it writes goes to
# $CAIRN_AGENT_STATE_DIR (keys, spool) and $TMPDIR, both expected to be writable volumes.
# Like the control-plane image it installs no OS packages (see docker/Dockerfile).

ARG BASE_IMAGE=python:3.12-slim

FROM ${BASE_IMAGE} AS build
# Comma-separated ingestion-framework extras for the sources this agent pool must reach.
ARG INGEST_EXTRAS=postgres,mysql,mssql,trino,redshift,snowflake,bigquery,unity-catalog,dbt,dbt-cloud,looker,tableau,powerbi,superset,preset,metabase,sigma,qlik-sense,mlflow,sagemaker,vertexai,kafka,s3,glue,mongodb,oracle,teradata,hana,db2,athena,hive,clickhouse,dynamodb,thoughtspot,mode,quicksight,microstrategy,kinesis,salesforce,openapi,fivetran,airbyte,azure-data-factory,fabric-data-factory,informatica,matillion-dpc
ARG INGEST_VERSION=1.7.0.20
# Together the extras pin NumPy 1.x (spaCy, for classification), which the newest pyarrow refuses (the S3 source would
# not load); and several sources import pkg_resources, which setuptools 81 dropped (Snowflake, BigQuery, Databricks,
# Redshift and Power BI would not load).
ARG INGEST_CONSTRAINTS="pyarrow<20 setuptools<81"
# uv resolves these extras in seconds. pip failed building a source package that imports pkg_resources and, with the
# constraints also given to its build environments, was still resolving after 25 minutes. The version is exact: what tests/test_gallery_ingestion_e2e.py runs is what the image has.
ARG UV_VERSION=0.11.32
# Overrides of the framework's own pins, so nothing needs a C compiler (the image has none): the
# ClickHouse extra pins zstd<1.5.6.8, which has no x86-64 Linux wheel; 1.5.7.2 does, and the driver uses it only to
# compress. tests/test_gallery_ingestion_e2e.py runs ClickHouse with it.
ARG INGEST_OVERRIDES="zstd==1.5.7.2"
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /src
COPY pyproject.toml README.md* ./
COPY src ./src
RUN --mount=type=secret,id=extra_ca,required=false \
    if [ -f /run/secrets/extra_ca ]; then export PIP_CERT=/run/secrets/extra_ca SSL_CERT_FILE=/run/secrets/extra_ca; fi \
 && python -m venv /opt/cairn \
 && /opt/cairn/bin/pip install --upgrade pip wheel "uv==${UV_VERSION}" \
 && printf '%s\n' ${INGEST_OVERRIDES} > /tmp/ingest-overrides.txt \
 && /opt/cairn/bin/uv pip install --python /opt/cairn/bin/python --no-cache \
    --override /tmp/ingest-overrides.txt \
    ".[agent]" "acryl-datahub[${INGEST_EXTRAS}]==${INGEST_VERSION}" ${INGEST_CONSTRAINTS} dumb-init \
 && /opt/cairn/bin/pip uninstall -y uv

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
ENTRYPOINT ["/opt/cairn/bin/dumb-init", "--", "cairn", "collectors"]
CMD ["run", "--config", "/etc/cairn-agent/agent.yml"]
