# syntax=docker/dockerfile:1.7
# The image deployed agent apps run in on Kubernetes (cairn.ai.agent_apps, CAIRN_AGENT_APPS_RUNTIME=kubernetes):
# Cairn's agent runtime (cairn.agentkit) and the model SDKs, nothing else of Cairn. Each pod unpacks its app's
# project from a Secret into /tmp and serves it; it runs as a non-root user on a read-only root file system.
#   docker build -f docker/agent-runtime.Dockerfile -t cairn/agent-runtime:0.1.0 .
# Projects that need more packages (langgraph, openai-agents, ...): add them here, or build FROM this image and set
#   CAIRN_AGENT_APPS_K8S_IMAGE (chart: cairn.agentApps.kubernetes.image). A deploy whose requirements.txt names a
#   package the image lacks is refused, naming it.
# Behind a TLS-intercepting proxy, pass its CA as a build secret: --secret id=extra_ca,src=/path/ca.crt
ARG BASE_IMAGE=python:3.12-slim
FROM ${BASE_IMAGE}
LABEL org.opencontainers.image.title="cairn-agent-runtime" \
      org.opencontainers.image.description="Runs Cairn agent apps: an agent project's respond() over HTTP"
ARG EXTRA_REQUIREMENTS=""
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/opt/cairn-runtime HOME=/tmp
RUN --mount=type=secret,id=extra_ca,required=false \
    if [ -f /run/secrets/extra_ca ]; then export PIP_CERT=/run/secrets/extra_ca SSL_CERT_FILE=/run/secrets/extra_ca; fi \
 && pip install "fastapi==0.162.0" "uvicorn==0.54.0" "httpx==0.28.1" "anthropic==2.3.0" "openai==3.46.0" \
        ${EXTRA_REQUIREMENTS} \
 && groupadd --system --gid 10001 agent \
 && useradd --system --uid 10001 --gid agent --no-create-home --shell /usr/sbin/nologin agent
COPY src/cairn/__init__.py /opt/cairn-runtime/cairn/__init__.py
COPY src/cairn/agentkit /opt/cairn-runtime/cairn/agentkit
USER 10001
EXPOSE 8080
ENTRYPOINT ["python", "-m", "cairn.agentkit.runtime"]
