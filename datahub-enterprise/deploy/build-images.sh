#!/usr/bin/env bash
# Build the DHE images: control plane, remote agent, authorizer plugin.
#
#   deploy/build-images.sh                       # tag dhe/*:0.1.0
#   DHE_VERSION=0.2.0 REGISTRY=ghcr.io/acme deploy/build-images.sh --push
#
# Environment knobs:
#   BASE_IMAGE      Python base image (default python:3.12-slim; e.g. mirror.gcr.io/library/python:3.12-slim
#                   when Docker Hub rate-limits you)
#   BUSYBOX_IMAGE   runtime for the plugin image (default busybox:1.37)
#   INGEST_EXTRAS   acryl-datahub source extras baked into the agent (default "postgres")
#   EXTRA_CA_FILE   CA bundle of a TLS-intercepting proxy; passed as a BuildKit secret (never stored)
#   HTTPS_PROXY / NO_PROXY are forwarded to the build when set; builds then use --network host.
#   PLUGIN_JAR      "host" (default): build+test the jar with ./gradlew on the host, then package it;
#                   "docker": build it inside Docker with the Gradle image.
set -euo pipefail
cd "$(dirname "$0")/.."

VERSION="${DHE_VERSION:-0.1.0}"
PREFIX="${REGISTRY:+${REGISTRY%/}/}dhe"
BASE_IMAGE="${BASE_IMAGE:-python:3.12-slim}"
BUSYBOX_IMAGE="${BUSYBOX_IMAGE:-busybox:1.37}"
PUSH=false
[[ "${1:-}" == "--push" ]] && PUSH=true

common=(--build-arg "BASE_IMAGE=${BASE_IMAGE}")
if [[ -n "${HTTPS_PROXY:-}" ]]; then
  common+=(--network host
           --build-arg "HTTPS_PROXY=${HTTPS_PROXY}" --build-arg "https_proxy=${HTTPS_PROXY}"
           --build-arg "NO_PROXY=${NO_PROXY:-}" --build-arg "no_proxy=${NO_PROXY:-}")
fi
if [[ -n "${EXTRA_CA_FILE:-}" ]]; then
  common+=(--secret "id=extra_ca,src=${EXTRA_CA_FILE}")
fi

build() {  # build <tag> <dockerfile> [extra args...]
  local tag="$1" file="$2"; shift 2
  echo ">>> building ${tag}"
  docker build "${common[@]}" -f "${file}" -t "${tag}" "$@" .
  if $PUSH; then docker push "${tag}"; fi
}

build "${PREFIX}/datahub-enterprise:${VERSION}" docker/Dockerfile
build "${PREFIX}/remote-agent:${VERSION}" docker/agent.Dockerfile \
  --build-arg "INGEST_EXTRAS=${INGEST_EXTRAS:-postgres}"

if [[ "${PLUGIN_JAR:-host}" == "host" ]]; then
  (cd plugins/java/enterprise-authorizer && ./gradlew --no-daemon -q test jar)
  build "${PREFIX}/enterprise-authorizer:${VERSION}" docker/authorizer-plugin.Dockerfile \
    --build-arg JAR_STAGE=prebuilt --build-arg "RUNTIME_IMAGE=${BUSYBOX_IMAGE}"
else
  build "${PREFIX}/enterprise-authorizer:${VERSION}" docker/authorizer-plugin.Dockerfile \
    --build-arg "RUNTIME_IMAGE=${BUSYBOX_IMAGE}"
fi
docker images --filter "reference=${PREFIX}/*:${VERSION}"
