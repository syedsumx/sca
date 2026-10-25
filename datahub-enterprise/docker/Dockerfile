# syntax=docker/dockerfile:1.7
# DHE control-plane image: one image, many entry points (dhe api | pdp | gateway | scheduler |
# actions | mcp). Build context: the datahub-enterprise/ directory.
#   docker build -f docker/Dockerfile -t dhe/datahub-enterprise:0.1.0 .

ARG PYTHON_VERSION=3.12

FROM python:${PYTHON_VERSION}-slim AS build
ARG EXTRAS=all
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential libpq-dev \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /src
COPY pyproject.toml README.md* ./
COPY src ./src
RUN python -m venv /opt/dhe \
 && /opt/dhe/bin/pip install --upgrade pip wheel \
 && /opt/dhe/bin/pip install ".[${EXTRAS}]"

FROM python:${PYTHON_VERSION}-slim AS runtime
LABEL org.opencontainers.image.title="datahub-enterprise" \
      org.opencontainers.image.description="DHE control plane: DQ engine, PDP, agent gateway, AI/MCP" \
      org.opencontainers.image.licenses="Apache-2.0"
RUN apt-get update \
 && apt-get install -y --no-install-recommends libpq5 ca-certificates tini curl \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd --system --gid 10001 dhe \
 && useradd --system --uid 10001 --gid dhe --home-dir /home/dhe --create-home --shell /usr/sbin/nologin dhe
COPY --from=build /opt/dhe /opt/dhe
COPY config /etc/dhe/config
ENV PATH="/opt/dhe/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DHE_POLICY_DIR=/etc/dhe/config/policies \
    DHE_GATEWAY_KEY_DIR=/var/lib/dhe/keys
RUN mkdir -p /var/lib/dhe/keys && chown -R dhe:dhe /var/lib/dhe
USER 10001:10001
WORKDIR /home/dhe
EXPOSE 8800 8810 8820 8830
ENTRYPOINT ["/usr/bin/tini", "--", "dhe"]
CMD ["api", "--host", "0.0.0.0", "--port", "8800"]
