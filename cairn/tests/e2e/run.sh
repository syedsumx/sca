#!/usr/bin/env bash
# Run the live e2e suite against a running docker-compose.cairn.yml stack.
#
#   tests/e2e/run.sh [pytest args...]
#
# Starts a webhook sink on the stack network, enrols the demo remote agent in pool "finance"
# (creating the pool if needed), then runs tests/e2e with credentials read from the stack and .env.
# The stack must allow the embed origins the suite uses, and send Slack and Teams to the doubles in
# tests/e2e/sink.py (the CI workflow writes the same into .env):
#   CAIRN_EMBED_ALLOWED_ORIGINS=https://*.tableau.example.com,http://localhost:9000,http://localhost:8099
#   CAIRN_SLACK_BOT_TOKEN=xoxb-e2e-double CAIRN_SLACK_API_URL=http://cairn-e2e-sink:8099/slack/api
#   CAIRN_SLACK_RESPONSE_HOSTS=http://cairn-e2e-sink:8099
#   CAIRN_TEAMS_WEBHOOK_HOSTS=.webhook.office.com,.logic.azure.com,http://cairn-e2e-sink:8099
set -euo pipefail
cd "$(dirname "$0")/../.."
# CAIRN_E2E_COMPOSE: the stack's compose files, colon-separated (e.g. with docker-compose.postgres.yml).
compose=(docker compose)
IFS=: read -r -a compose_files <<< "${CAIRN_E2E_COMPOSE:-docker-compose.cairn.yml}"
for f in "${compose_files[@]}"; do compose+=(-f "$f"); done
export CAIRN_E2E_COMPOSE="${CAIRN_E2E_COMPOSE:-docker-compose.cairn.yml}"
PY="${PYTHON:-python3}"
env_val() { grep -E "^$1=" .env | cut -d= -f2-; }
# The store and catalog credentials the stack runs with (test_store_live, test_catalog_live).
for k in CAIRN_STORE_ACCESS_KEY CAIRN_STORE_SECRET_KEY CAIRN_CATALOG_TOKEN; do
  v="$(env_val "$k" || true)"
  if [ -z "${!k:-}" ] && [ -n "$v" ]; then export "$k=$v"; fi
done

# Root actions in the tests use a one-hour admin token; the control plane runs as cairn-service.
token="$(CAIRN_ADMIN_PASSWORD="$(env_val CAIRN_ADMIN_PASSWORD)" "$PY" deploy/bootstrap/mint_core_token.py \
  --frontend-url http://localhost:9002 --core-url http://localhost:8080 --print-admin-token)"
service_token="$("${compose[@]}" exec -T cairn-api cat /run/cairn/core_token)"

# Webhook sink: records POSTs, returns them on GET.
docker rm -f cairn-e2e-sink >/dev/null 2>&1 || true
docker run -d --name cairn-e2e-sink --network cairn_catalog -p 127.0.0.1:8099:8099 \
  -v "$PWD/tests/e2e/sink.py:/sink.py:ro" "${SINK_IMAGE:-python:3.12-slim}" python /sink.py >/dev/null
# KEEP_SINK=1 leaves it running (CI hands its recorded payloads to the leak scan).
[ "${KEEP_SINK:-0}" = 1 ] || trap 'docker rm -f cairn-e2e-sink >/dev/null 2>&1 || true' EXIT

# Remote agent: enrol once per fresh database.
if ! "${compose[@]}" logs cairn-remote-agent 2>/dev/null | grep -q "enrolled as"; then
  curl -sS -o /dev/null -XPOST -H "Authorization: Bearer ${token}" -H 'Content-Type: application/json' \
    localhost:8800/agents/pools -d '{"name": "finance", "allowed_platforms": ["postgres"]}' || true
  enr="$(curl -fsS -XPOST -H "Authorization: Bearer ${token}" localhost:8800/agents/pools/finance/enrollment-tokens \
    | "$PY" -c 'import json,sys; print(json.load(sys.stdin)["token"])')"
  CAIRN_AGENT_ENROLLMENT_TOKEN="${enr}" "${compose[@]}" up -d --no-build --force-recreate cairn-remote-agent >/dev/null
  for _ in $(seq 1 30); do
    "${compose[@]}" logs cairn-remote-agent 2>/dev/null | grep -q "enrolled as" && break
    sleep 2
  done
fi

# Single sign-on against a test identity provider (CAIRN_E2E_SSO=0 skips it). cairn-web keeps
# password sign-in alongside SSO, so the other tests are unaffected.
if [ "${CAIRN_E2E_SSO:-1}" = 1 ]; then
  "${compose[@]}" -f tests/e2e/docker-compose.sso.yml up -d --no-build --wait cairn-oidc cairn-web >/dev/null
  export CAIRN_E2E_SSO=1 CAIRN_E2E_SSO_SECRET="${CAIRN_E2E_SSO_SECRET:-e2e-sso-secret}"
fi

# Edge HTTPS with a throwaway certificate (CAIRN_E2E_TLS=0 skips it).
if [ "${CAIRN_E2E_TLS:-1}" = 1 ]; then
  tls_dir="$PWD/tests/e2e/.tls"
  if [ ! -f "$tls_dir/tls.crt" ]; then
    mkdir -p "$tls_dir"
    openssl req -x509 -newkey rsa:2048 -nodes -days 7 -subj "/CN=localhost" \
      -addext "subjectAltName=DNS:localhost,IP:127.0.0.1" -keyout "$tls_dir/tls.key" -out "$tls_dir/tls.crt" 2>/dev/null
    chmod a+r "$tls_dir"/tls.*
  fi
  CAIRN_TLS_DIR="$tls_dir" "${compose[@]}" up -d --no-build --no-deps --wait edge >/dev/null
  tls_port="$(env_val CAIRN_MAPPED_EDGE_TLS_PORT || true)"
  export CAIRN_E2E_TLS_URL="https://localhost:${tls_port:-9443}" CAIRN_E2E_TLS_CA="$tls_dir/tls.crt"
fi

export CAIRN_E2E=1 CAIRN_E2E_TOKEN="${token}" CAIRN_E2E_SERVICE_TOKEN="${service_token}"
export CAIRN_E2E_UI_PASSWORD="$(env_val CAIRN_ADMIN_PASSWORD)"
export CAIRN_E2E_SLACK_SECRET="$(env_val CAIRN_SLACK_SIGNING_SECRET)"
export CAIRN_E2E_PDP_TOKEN="$(env_val CAIRN_SERVICE_TOKEN)"
export CAIRN_E2E_SCIM_TOKEN="$(env_val CAIRN_SCIM_TOKEN || true)"
export CAIRN_E2E_AUTHZ_MODE="$(env_val CAIRN_AUTHZ_MODE || true)"; CAIRN_E2E_AUTHZ_MODE="${CAIRN_E2E_AUTHZ_MODE:-additive}"
export CAIRN_E2E_SINK=http://cairn-e2e-sink:8099/ CAIRN_E2E_SINK_HOST=http://127.0.0.1:8099/
"$PY" -m pytest tests/e2e -p no:cacheprovider "$@"
