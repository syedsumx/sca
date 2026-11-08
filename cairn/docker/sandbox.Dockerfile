# syntax=docker/dockerfile:1.7
# The image code sandboxes run in (cairn.ai.sandboxes): Python with the usual data libraries and DuckDB (for SQL runs).
# Cairn mounts its runner, the code and the inputs read-only and runs it as uid 1000 with no network interface and a
# read-only root file system; nothing here needs to be writable.
#   docker build -f docker/sandbox.Dockerfile -t cairn/sandbox:0.1.0 .
# Behind a TLS-intercepting proxy, pass its CA as a build secret: --secret id=extra_ca,src=/path/ca.crt
ARG BASE_IMAGE=python:3.12-slim
FROM ${BASE_IMAGE}
LABEL org.opencontainers.image.title="cairn-sandbox" \
      org.opencontainers.image.description="Runs code in Cairn's sandboxes"
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    MPLCONFIGDIR=/tmp HOME=/work
RUN --mount=type=secret,id=extra_ca,required=false \
    if [ -f /run/secrets/extra_ca ]; then export PIP_CERT=/run/secrets/extra_ca SSL_CERT_FILE=/run/secrets/extra_ca; fi \
 && pip install "duckdb==1.1.3" "pandas==2.2.3" "pyarrow==18.1.0" "numpy==2.1.3" \
 && useradd --no-create-home --uid 1000 sandbox
USER 1000
WORKDIR /work
