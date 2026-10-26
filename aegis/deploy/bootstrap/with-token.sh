#!/bin/sh
# Exports AEGIS_GMS_TOKEN from the bootstrap volume (unless already set) and execs `aegis <args>`.
set -eu
TOKEN_FILE="${AEGIS_GMS_TOKEN_FILE:-/run/aegis/gms_token}"
if [ -z "${AEGIS_GMS_TOKEN:-}" ] && [ -r "$TOKEN_FILE" ]; then
  AEGIS_GMS_TOKEN="$(cat "$TOKEN_FILE")"
  export AEGIS_GMS_TOKEN
fi
exec aegis "$@"
