#!/bin/sh
# Exports CAIRN_CORE_TOKEN from the bootstrap volume (unless already set) and execs `cairn <args>`.
set -eu
TOKEN_FILE="${CAIRN_CORE_TOKEN_FILE:-/run/cairn/core_token}"
if [ -z "${CAIRN_CORE_TOKEN:-}" ] && [ -r "$TOKEN_FILE" ]; then
  CAIRN_CORE_TOKEN="$(cat "$TOKEN_FILE")"
  export CAIRN_CORE_TOKEN
fi
exec cairn "$@"
