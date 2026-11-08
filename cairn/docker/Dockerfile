# syntax=docker/dockerfile:1.7
# Cairn control-plane image: one image, many entry points (cairn api | pdp | gateway | scheduler |
# actions | mcp). Build context: the cairn/ directory.
#   docker build -f docker/Dockerfile -t cairn/platform:0.1.0 .
#   (or deploy/build-images.sh, which also handles proxies and registry mirrors)
#
# Python dependencies ship as manylinux wheels (psycopg[binary], cryptography, uvloop, confluent-kafka),
# PID 1 is dumb-init from PyPI and health checks use Python. The one OS package is Tesseract, for OCR of
# scanned documents (OCR_LANGUAGES adds language data, e.g. "eng deu"; OCR_LANGUAGES="" leaves it out).
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
 && python -m venv /opt/cairn \
 && /opt/cairn/bin/pip install --upgrade pip wheel \
 && /opt/cairn/bin/pip install ".[${EXTRAS}]" dumb-init

FROM ${BASE_IMAGE} AS runtime
ARG OCR_LANGUAGES=eng
RUN if [ -n "${OCR_LANGUAGES}" ]; then \
      apt-get update \
   && apt-get install -y --no-install-recommends tesseract-ocr $(for l in ${OCR_LANGUAGES}; do printf 'tesseract-ocr-%s ' "$l"; done) \
   && rm -rf /var/lib/apt/lists/*; \
    fi
# pdftoppm: PDF pages rendered for the model when documents are parsed for charts (cairn.pipelines.parse).
RUN apt-get update \
 && apt-get install -y --no-install-recommends poppler-utils \
 && rm -rf /var/lib/apt/lists/*
LABEL org.opencontainers.image.title="cairn" \
      org.opencontainers.image.description="Cairn control plane: DQ engine, PDP, agent gateway, AI/MCP" \
      org.opencontainers.image.licenses="Apache-2.0"
RUN groupadd --system --gid 10001 cairn \
 && useradd --system --uid 10001 --gid cairn --home-dir /home/cairn --create-home --shell /usr/sbin/nologin cairn \
 && mkdir -p /var/lib/cairn/keys && chown -R cairn:cairn /var/lib/cairn
COPY --from=build /opt/cairn /opt/cairn
COPY config /etc/cairn/config
COPY docker/healthcheck.py /usr/local/bin/cairn-healthcheck
# Public keys licences are checked against (deploy/ops/licence.py keygen); none in the source tree.
COPY deploy/licence/licence-keys.json /usr/share/cairn/licence-keys.json
# Service-account bootstrap / token keeper and the token wrapper (used by Helm; compose mounts the same files).
COPY deploy/bootstrap/mint_core_token.py deploy/bootstrap/with-token.sh /opt/cairn/bootstrap/
ENV PATH="/opt/cairn/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    CAIRN_POLICY_DIR=/etc/cairn/config/policies \
    CAIRN_GATEWAY_KEY_DIR=/var/lib/cairn/keys
USER 10001:10001
WORKDIR /home/cairn
EXPOSE 8800 8810 8820 8830
ENTRYPOINT ["/opt/cairn/bin/dumb-init", "--", "cairn"]
CMD ["api", "--host", "0.0.0.0", "--port", "8800"]
