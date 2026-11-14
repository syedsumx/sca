# syntax=docker/dockerfile:1.7
# Cairn dbt runner: dbt-core with dbt-trino, run as a Cairn identity (`cairn dbt <dbt arguments>`).
#   docker build -f docker/dbt.Dockerfile -t cairn/dbt:0.1.0 .
#
# A project's own image adds its code on top:
#   FROM cairn/dbt:0.1.0
#   COPY . /project
# The image works with readOnlyRootFilesystem and a read-only project: dbt writes only under /tmp/cairn
# (artifacts in /tmp/cairn/target, logs, packages), expected to be a writable volume.

ARG BASE_IMAGE=python:3.12-slim

FROM ${BASE_IMAGE} AS build
ARG DBT_VERSION=1.12.6
ARG DBT_TRINO_VERSION=1.11.0
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /src
COPY pyproject.toml README.md* ./
COPY src ./src
RUN --mount=type=secret,id=extra_ca,required=false \
    if [ -f /run/secrets/extra_ca ]; then export PIP_CERT=/run/secrets/extra_ca SSL_CERT_FILE=/run/secrets/extra_ca; fi \
 && python -m venv /opt/cairn \
 && /opt/cairn/bin/pip install --upgrade pip wheel \
 && /opt/cairn/bin/pip install . "dbt-core==${DBT_VERSION}" "dbt-trino==${DBT_TRINO_VERSION}" dumb-init

FROM ${BASE_IMAGE} AS runtime
LABEL org.opencontainers.image.title="cairn-dbt" \
      org.opencontainers.image.description="dbt on the Cairn lakehouse, signed in as a Cairn identity"
RUN groupadd --system --gid 10001 cairn \
 && useradd --system --uid 10001 --gid cairn --no-create-home --shell /usr/sbin/nologin cairn \
 && mkdir -p /project /tmp/cairn && chown cairn:cairn /tmp/cairn
COPY --from=build /opt/cairn /opt/cairn
ENV PATH="/opt/cairn/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOME=/tmp/cairn \
    TMPDIR=/tmp/cairn \
    DBT_SEND_ANONYMOUS_USAGE_STATS=false \
    DBT_LOG_PATH=/tmp/cairn/logs \
    DBT_PACKAGES_INSTALL_PATH=/tmp/cairn/dbt_packages \
    CAIRN_DBT_TARGET_PATH=/tmp/cairn/target
WORKDIR /project
USER 10001:10001
VOLUME ["/tmp/cairn"]
ENTRYPOINT ["/opt/cairn/bin/dumb-init", "--", "cairn", "dbt"]
CMD ["build"]
