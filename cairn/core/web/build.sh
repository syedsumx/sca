#!/usr/bin/env bash
# Build the Cairn web UI from the pinned engine release and package it for the web image.
#
#   core/web/build.sh [workdir]        # -> core/web/build/web-ui-assets.jar
#
# Needs git, Node 22 and yarn. The workdir (default: a temp dir) keeps the source checkout and
# node_modules between runs.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
version="$(cat "${here}/../UPSTREAM_VERSION")"
work="${1:-$(mktemp -d)}"
src="${work}/src"
repo="${UPSTREAM_GIT_URL:-https://github.com/datahub-project/datahub.git}"

if [[ ! -d "${src}/.git" ]]; then
  git clone --quiet --depth 1 --branch "${version}" "${repo}" "${src}"
else
  git -C "${src}" checkout --quiet -- . && git -C "${src}" clean --quiet -fd -- datahub-web-react/src datahub-web-react/public
fi
ui="${src}/datahub-web-react"
(cd "${ui}" && yarn install --frozen-lockfile --silent)
node "${here}/rebrand.mjs" "${ui}" "${here}/brand"
(cd "${ui}" && node scripts/generate-lazy-icon-stubs.js && DATAHUB_APP_VERSION="${CAIRN_VERSION:-0.1.0}" yarn build)
mkdir -p "${here}/build"
python3 "${here}/package.py" "${ui}/dist" "${here}/brand" "${here}/build/web-ui-assets.jar"
