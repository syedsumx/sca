# syntax=docker/dockerfile:1.7
# Cairn's egress proxy for user code (cairn.network.proxy): the only way out of jobs, notebooks, flows' steps,
# sandboxes and agent apps when egress is enforced. Only the Python standard library.
#   docker build -f docker/egress-proxy.Dockerfile -t cairn/egress-proxy:0.1.0 .
ARG BASE_IMAGE=python:3.12-slim
FROM ${BASE_IMAGE}
LABEL org.opencontainers.image.title="cairn-egress-proxy" \
      org.opencontainers.image.description="Allowlisting CONNECT/HTTP proxy for Cairn user code; logs every refusal"
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/opt/cairn
COPY src/cairn/__init__.py /opt/cairn/cairn/__init__.py
COPY src/cairn/network/__init__.py src/cairn/network/proxy.py /opt/cairn/cairn/network/
USER 65534:65534
EXPOSE 3128
ENTRYPOINT ["python", "-m", "cairn.network.proxy"]
CMD ["--listen", "0.0.0.0:3128"]
