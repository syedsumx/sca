#!/usr/bin/env bash
# Build the Cairn images: Cairn Core (core, web, setup, executor), control plane, consumer UI,
# remote agent, Cairn Store, authorizer plugin.
#
#   core/web/build.sh                            # once per engine release: builds the web UI bundle
#   deploy/build-images.sh                       # tag cairn/*:0.1.0
#   CAIRN_VERSION=0.2.0 REGISTRY=ghcr.io/acme deploy/build-images.sh --push
#
# Environment knobs:
#   BASE_IMAGE      Python base image (default python:3.12-slim; e.g. mirror.gcr.io/library/python:3.12-slim
#                   when Docker Hub rate-limits you)
#   BUSYBOX_IMAGE   runtime for the plugin image (default busybox:1.37)
#   RUST_IMAGE      Rust build image for Cairn Store (default rust:1-alpine)
#   ALPINE_IMAGE    runtime for Cairn Store (default alpine:3.20)
#   INGEST_EXTRAS   ingestion source extras baked into the agent (default "postgres")
#   ONLY            space-separated subset to build, e.g. ONLY="core web"
#   EXTRA_CA_FILE   CA bundle of a TLS-intercepting proxy; passed as a BuildKit secret (never stored)
#   HTTPS_PROXY / NO_PROXY are forwarded to the build when set; builds then use --network host.
#   PLUGIN_JAR      "host" (default): build+test the jar with ./gradlew on the host, then package it;
#                   "docker": build it inside Docker with the Gradle image.
set -euo pipefail
cd "$(dirname "$0")/.."

VERSION="${CAIRN_VERSION:-0.1.0}"
PREFIX="${REGISTRY:+${REGISTRY%/}/}cairn"
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

want() { [[ -z "${ONLY:-}" || " ${ONLY} " == *" $1 "* ]]; }

build() {  # build <tag> <dockerfile> [extra args...]
  local tag="$1" file="$2"; shift 2
  echo ">>> building ${tag}"
  docker build "${common[@]}" -f "${file}" -t "${tag}" "$@" .
  if $PUSH; then docker push "${tag}"; fi
}

want platform && build "${PREFIX}/platform:${VERSION}" docker/Dockerfile
want consumer-ui && build "${PREFIX}/consumer-ui:${VERSION}" docker/consumer-ui.Dockerfile
want store && build "${PREFIX}/store:${VERSION}" docker/store.Dockerfile \
  --build-arg "RUST_IMAGE=${RUST_IMAGE:-rust:1-alpine}" --build-arg "RUN_IMAGE=${ALPINE_IMAGE:-alpine:3.20}"
want remote-agent && build "${PREFIX}/remote-agent:${VERSION}" docker/agent.Dockerfile \
  --build-arg "INGEST_EXTRAS=${INGEST_EXTRAS:-postgres}"

if want authorizer || want core; then
  if [[ "${PLUGIN_JAR:-host}" == "host" ]]; then
    (cd plugins/java/cairn-authorizer && ./gradlew --no-daemon -q test jar)
    build "${PREFIX}/authorizer:${VERSION}" docker/authorizer-plugin.Dockerfile \
      --build-arg JAR_STAGE=prebuilt --build-arg "RUNTIME_IMAGE=${BUSYBOX_IMAGE}"
  else
    build "${PREFIX}/authorizer:${VERSION}" docker/authorizer-plugin.Dockerfile \
      --build-arg "RUNTIME_IMAGE=${BUSYBOX_IMAGE}"
  fi
fi

# Cairn Core: one Dockerfile, one target per service (see core/README.md).
upstream="$(cat core/UPSTREAM_VERSION)"
core_args=(--build-arg "UPSTREAM_VERSION=${upstream}" --build-arg "UPSTREAM_JAR_VERSION=${upstream#v}"
           --build-arg "PLUGIN_IMAGE=${PREFIX}/authorizer:${VERSION}" --build-arg "CAIRN_VERSION=${VERSION}"
           --build-arg "PYTHON_IMAGE=${BASE_IMAGE}")
for target in core web setup executor; do
  if want "${target}"; then
    if [[ "${target}" == web && ! -f core/web/build/web-ui-assets.jar ]]; then
      echo "core/web/build/web-ui-assets.jar missing: run core/web/build.sh first" >&2; exit 1
    fi
    build "${PREFIX}/${target}:${VERSION}" core/Dockerfile --target "${target}" "${core_args[@]}"
  fi
done
docker images --filter "reference=${PREFIX}/*:${VERSION}"
