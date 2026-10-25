#!/bin/sh
# Exports DHE_GMS_TOKEN from the bootstrap volume (unless already set) and execs `dhe <args>`.
set -eu
TOKEN_FILE="${DHE_GMS_TOKEN_FILE:-/run/dhe/gms_token}"
if [ -z "${DHE_GMS_TOKEN:-}" ] && [ -r "$TOKEN_FILE" ]; then
  DHE_GMS_TOKEN="$(cat "$TOKEN_FILE")"
  export DHE_GMS_TOKEN
fi
exec dhe "$@"
