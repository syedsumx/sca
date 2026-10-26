#!/bin/sh
# Exports CAIRN_CORE_TOKEN from the bootstrap volume (unless already set) and runs `cairn <args>`.
# Python clients re-read CAIRN_CORE_TOKEN_FILE on every change, so rotation needs no restart. Processes
# that hold the token in their own config (the actions pipelines) set CAIRN_RESTART_ON_TOKEN_CHANGE=1:
# the service exits when the file changes and the container restart policy brings it back.
set -eu
TOKEN_FILE="${CAIRN_CORE_TOKEN_FILE:-/run/cairn/core_token}"
if [ -z "${CAIRN_CORE_TOKEN:-}" ] && [ -r "$TOKEN_FILE" ]; then
  CAIRN_CORE_TOKEN="$(cat "$TOKEN_FILE")"
  export CAIRN_CORE_TOKEN
fi
if [ "${CAIRN_RESTART_ON_TOKEN_CHANGE:-0}" != "1" ] || [ ! -r "$TOKEN_FILE" ]; then
  exec cairn "$@"
fi
start="$(stat -c %Y "$TOKEN_FILE")"
cairn "$@" &
child=$!
trap 'kill "$child" 2>/dev/null; wait "$child"; exit 143' TERM INT
while kill -0 "$child" 2>/dev/null; do
  sleep "${CAIRN_TOKEN_POLL_SECONDS:-30}"
  if [ "$(stat -c %Y "$TOKEN_FILE" 2>/dev/null || echo "$start")" != "$start" ]; then
    echo "[with-token] service token rotated; restarting" >&2
    kill "$child" 2>/dev/null || true
    wait "$child" 2>/dev/null || true
    exit 0
  fi
done
wait "$child"
