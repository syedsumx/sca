# syntax=docker/dockerfile:1.7
# The image scheduled notebook and Python jobs run in (cairn.pipelines.jobs): Python, a Jupyter kernel and the
# notebook runner, and the clients a job reads the lakehouse with (Trino, pyiceberg). Runs as a non-root user.
#   docker build -f docker/jobs.Dockerfile -t cairn/jobs:0.1.0 .
# Behind a TLS-intercepting proxy, pass its CA as a build secret: --secret id=extra_ca,src=/path/ca.crt
ARG BASE_IMAGE=python:3.12-slim
FROM ${BASE_IMAGE}
LABEL org.opencontainers.image.title="cairn-jobs" \
      org.opencontainers.image.description="Runs scheduled Cairn notebook and Python jobs"
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 PYTHONUNBUFFERED=1
RUN --mount=type=secret,id=extra_ca,required=false \
    if [ -f /run/secrets/extra_ca ]; then export PIP_CERT=/run/secrets/extra_ca SSL_CERT_FILE=/run/secrets/extra_ca; fi \
 && pip install "nbconvert==7.16.6" "nbclient==0.10.2" "ipykernel==6.29.5" "trino==0.333.0" \
        "pyiceberg[pyarrow]==0.9.1" "pandas==2.2.3" \
 && useradd --create-home --uid 1000 job
USER 1000
WORKDIR /home/job
