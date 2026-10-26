#!/usr/bin/env bash
# Run the live e2e suite against a running docker-compose.cairn.yml stack.
#
#   tests/e2e/run.sh [pytest args...]
#
# Starts a webhook sink on the stack network, enrols the demo remote agent in pool "finance"
# (creating the pool if needed), then runs tests/e2e with credentials read from the stack and .env.
set -euo pipefail
cd "$(dirname "$0")/../.."
compose=(docker compose -f docker-compose.cairn.yml)
PY="${PYTHON:-python3}"
env_val() { grep -E "^$1=" .env | cut -d= -f2-; }

# Root actions in the tests use a one-hour admin token; the control plane runs as cairn-service.
token="$(CAIRN_ADMIN_PASSWORD="$(env_val CAIRN_ADMIN_PASSWORD)" "$PY" deploy/bootstrap/mint_core_token.py \
  --frontend-url http://localhost:9002 --core-url http://localhost:8080 --print-admin-token)"
service_token="$("${compose[@]}" exec -T cairn-api cat /run/cairn/core_token)"

# Webhook sink: records POSTs, returns them on GET.
docker rm -f cairn-e2e-sink >/dev/null 2>&1 || true
docker run -d --name cairn-e2e-sink --network cairn_catalog -p 127.0.0.1:8099:8099 \
  -v "$PWD/tests/e2e/sink.py:/sink.py:ro" "${SINK_IMAGE:-python:3.12-slim}" python /sink.py >/dev/null
trap 'docker rm -f cairn-e2e-sink >/dev/null 2>&1 || true' EXIT

# Remote agent: enrol once per fresh database.
if ! "${compose[@]}" logs cairn-remote-agent 2>/dev/null | grep -q "enrolled as"; then
  curl -fsS -o /dev/null -XPOST -H "Authorization: Bearer ${token}" -H 'Content-Type: application/json' \
    localhost:8800/agents/pools -d '{"name": "finance", "allowed_platforms": ["postgres"]}' || true
  enr="$(curl -fsS -XPOST -H "Authorization: Bearer ${token}" localhost:8800/agents/pools/finance/enrollment-tokens \
    | "$PY" -c 'import json,sys; print(json.load(sys.stdin)["token"])')"
  CAIRN_AGENT_ENROLLMENT_TOKEN="${enr}" "${compose[@]}" up -d --no-build --force-recreate cairn-remote-agent >/dev/null
  for _ in $(seq 1 30); do
    "${compose[@]}" logs cairn-remote-agent 2>/dev/null | grep -q "enrolled as" && break
    sleep 2
  done
fi

export CAIRN_E2E=1 CAIRN_E2E_TOKEN="${token}" CAIRN_E2E_SERVICE_TOKEN="${service_token}"
export CAIRN_E2E_UI_PASSWORD="$(env_val CAIRN_ADMIN_PASSWORD)"
export CAIRN_E2E_SLACK_SECRET="$(env_val CAIRN_SLACK_SIGNING_SECRET)"
export CAIRN_E2E_SINK=http://cairn-e2e-sink:8099/ CAIRN_E2E_SINK_HOST=http://127.0.0.1:8099/
"$PY" -m pytest tests/e2e -p no:cacheprovider "$@"
