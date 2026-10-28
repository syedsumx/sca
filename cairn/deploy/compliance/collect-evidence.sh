#!/usr/bin/env bash
# Collect the machine evidence a SOC 2 (or ISO 27001) auditor asks for, from a running Compose
# deployment, into one dated folder with checksums. See docs/operations/soc2-readiness.md for which
# control each file supports.
#
#   deploy/compliance/collect-evidence.sh [OUT_DIR] [--since 2026-07-01]
#
# Secrets are never collected: deployment settings are written with every password, secret, token
# and key value replaced by "***". COMPOSE overrides the compose command (as for backup.sh).
set -euo pipefail

cd "$(dirname "$0")/../.."
COMPOSE=${COMPOSE:-docker compose -f docker-compose.cairn.yml}
OUT=""; SINCE=""
while [ $# -gt 0 ]; do
    case "$1" in
        --since) SINCE=$2; shift 2 ;;
        *) OUT=$1; shift ;;
    esac
done
OUT=${OUT:-evidence/cairn-$(date -u +%Y%m%dT%H%M%SZ)}
SINCE=${SINCE:-$(date -u -d '90 days ago' +%Y-%m-%dT00:00:00Z 2>/dev/null || date -u -v-90d +%Y-%m-%dT00:00:00Z)}
mkdir -p "$OUT"
log() { printf '%s %s\n' "$(date -u +%H:%M:%S)" "$*" >&2; }
api() { $COMPOSE exec -T cairn-api cairn "$@"; }
get() {   # GET a Cairn API path as the control-plane service account
    $COMPOSE exec -T cairn-api python -c '
import json, os, sys, urllib.request
req = urllib.request.Request("http://127.0.0.1:8800" + sys.argv[1],
                             headers={"Authorization": "Bearer " + os.environ["CAIRN_SERVICE_TOKEN"]})
print(json.dumps(json.load(urllib.request.urlopen(req, timeout=30)), indent=2))' "$1"
}

log "audit log: chain verification and entries since $SINCE"
api audit verify > "$OUT/audit-verify.json"
api audit export --since "$SINCE" > "$OUT/audit-log.jsonl"

log "access review: policies, personas, purposes, extensions, licence"
get /governance/policies > "$OUT/access-policies.json"
get /governance/personas > "$OUT/access-personas.json"
get /governance/purposes > "$OUT/access-purposes.json"
get /admin/extensions > "$OUT/access-extensions.json"
get /admin/licence > "$OUT/licence.json"
get /governance/ai-systems/report > "$OUT/ai-systems-inventory.json"

log "change management: schema revision and running images"
api db current > "$OUT/schema-revision.json"
$COMPOSE images --format json > "$OUT/images.json" 2>/dev/null || $COMPOSE images > "$OUT/images.txt"
git rev-parse HEAD > "$OUT/source-revision.txt" 2>/dev/null || true

log "configuration (secrets redacted)"
$COMPOSE config 2>/dev/null | sed -E 's/^([[:space:]]*[A-Za-z0-9_]*(PASSWORD|SECRET|TOKEN|KEY|PASS)[A-Za-z0-9_]*:[[:space:]]*).*/\1"***"/I' \
    > "$OUT/deployment-config.yaml"

log "availability: backups and monitoring"
{ ls -1d backups/*/ 2>/dev/null | while read -r d; do
      [ -f "$d/manifest.json" ] && python3 -c 'import json,sys; m=json.load(open(sys.argv[1])); print(json.dumps({"backup": sys.argv[1], "created_at": m["created_at"], "schema": m.get("alembic_revision")}))' "$d/manifest.json"
  done; } > "$OUT/backups.jsonl"
cp deploy/observability/cairn-alerts.yaml "$OUT/alert-rules.yaml"
$COMPOSE ps --format json > "$OUT/services.json" 2>/dev/null || $COMPOSE ps > "$OUT/services.txt"

python3 - "$OUT" "$SINCE" <<'EOF'
import hashlib, json, pathlib, sys, datetime
d = pathlib.Path(sys.argv[1])
files = {p.name: {"sha256": hashlib.sha256(p.read_bytes()).hexdigest(), "bytes": p.stat().st_size}
         for p in sorted(d.iterdir()) if p.is_file() and p.name != "manifest.json"}
(d / "manifest.json").write_text(json.dumps({
    "collected_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
    "audit_since": sys.argv[2], "files": files}, indent=2))
EOF
chmod -R go-rwx "$OUT"
log "evidence written to $OUT"
echo "$OUT"
