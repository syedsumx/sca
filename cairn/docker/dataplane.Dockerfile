# syntax=docker/dockerfile:1.7
# The data plane connector (helm/cairn-dataplane): runs in the customer's cluster, connects only out to Cairn's
# gateway, relays Trino's access-control questions to Cairn's PDP, runs jobs and signs storage URLs there.
#   docker build -f docker/dataplane.Dockerfile -t cairn/dataplane-connector:0.1.0 .
# Only Python, httpx and cryptography, and the few modules it uses: no Cairn server code, no database driver.
# Behind a TLS-intercepting proxy, pass its CA as a build secret: --secret id=extra_ca,src=/path/ca.crt
ARG BASE_IMAGE=python:3.12-slim
FROM ${BASE_IMAGE}
LABEL org.opencontainers.image.title="cairn-dataplane-connector" \
      org.opencontainers.image.description="Cairn data plane connector (outbound only, end-to-end encrypted)"
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/opt/cairn HOME=/tmp CAIRN_DATAPLANE_STATE_DIR=/var/lib/cairn-dataplane
RUN --mount=type=secret,id=extra_ca,required=false \
    if [ -f /run/secrets/extra_ca ]; then export PIP_CERT=/run/secrets/extra_ca; fi \
 && pip install "httpx==0.28.1" "cryptography==46.0.3" \
 && groupadd --system --gid 10001 cairn \
 && useradd --system --uid 10001 --gid cairn --no-create-home --shell /usr/sbin/nologin cairn \
 && mkdir -p /var/lib/cairn-dataplane && chown cairn:cairn /var/lib/cairn-dataplane
COPY src/cairn/__init__.py /opt/cairn/cairn/__init__.py
COPY src/cairn/agent/__init__.py src/cairn/agent/crypto.py src/cairn/agent/client.py /opt/cairn/cairn/agent/
COPY src/cairn/dataplane/__init__.py src/cairn/dataplane/connector.py /opt/cairn/cairn/dataplane/
COPY src/cairn/network/__init__.py src/cairn/network/proxy.py /opt/cairn/cairn/network/
USER 10001:10001
VOLUME ["/var/lib/cairn-dataplane"]
# 8181: Trino's access-control questions, from inside the cluster only (the chart's NetworkPolicy).
EXPOSE 8181
ENTRYPOINT ["python", "-m", "cairn.dataplane.connector"]
