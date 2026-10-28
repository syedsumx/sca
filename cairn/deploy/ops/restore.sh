#!/usr/bin/env bash
# Restore a Docker Compose deployment of Cairn from a backup made by deploy/ops/backup.sh.
#
#   deploy/ops/restore.sh BACKUP_DIR [--yes]
#
# Stops every Cairn service except the databases, replaces the Cairn (Postgres) and catalog (MySQL)
# databases and the gateway keys with the backup's, rebuilds the search index from MySQL and starts
# everything again. Anything written after the backup was taken is lost. The backup's config/ is not
# unpacked over yours; set RESTORE_CONFIG=1 to do that too.
set -euo pipefail

cd "$(dirname "$0")/../.."
COMPOSE=${COMPOSE:-docker compose -f docker-compose.cairn.yml}
IN=${1:?usage: restore.sh BACKUP_DIR [--yes]}
log() { printf '%s %s\n' "$(date -u +%H:%M:%S)" "$*" >&2; }
DATA_SERVICES="mysql cairn-postgres search broker"

[ -f "$IN/manifest.json" ] || { log "$IN/manifest.json not found"; exit 1; }
python3 - "$IN" <<'EOF'
import hashlib, json, pathlib, sys
d = pathlib.Path(sys.argv[1]); m = json.loads((d / "manifest.json").read_text())
for name, f in m["files"].items():
    got = hashlib.sha256((d / name).read_bytes()).hexdigest()
    if got != f["sha256"]:
        sys.exit(f"{name}: checksum mismatch, the backup is damaged")
print(f"backup of {m['created_at']} (Cairn {m['cairn_version']}, schema {m['alembic_revision']}): checksums match",
      file=sys.stderr)
EOF

if [ "${2:-}" != "--yes" ]; then
    read -r -p "This replaces all Cairn data with the backup's. Type 'restore' to continue: " answer
    [ "$answer" = restore ] || exit 1
fi

apps=$($COMPOSE ps --services --status running | grep -vxF -f <(tr ' ' '\n' <<< "$DATA_SERVICES") || true)
log "stopping: $(echo $apps)"
[ -z "$apps" ] || $COMPOSE stop $apps
$COMPOSE up -d --wait $DATA_SERVICES

log "postgres: restoring database cairn"
$COMPOSE exec -T cairn-postgres sh -c 'psql -U "$POSTGRES_USER" -d postgres -v ON_ERROR_STOP=1 -q \
    -c "select pg_terminate_backend(pid) from pg_stat_activity where datname = '\''cairn'\'' and pid <> pg_backend_pid()" \
    -c "drop database if exists cairn" -c "create database cairn owner \"$POSTGRES_USER\"" >/dev/null'
$COMPOSE exec -T cairn-postgres sh -c 'psql -U "$POSTGRES_USER" -d cairn -q -c "create extension if not exists vector"'
$COMPOSE exec -T cairn-postgres sh -c 'exec pg_restore -U "$POSTGRES_USER" -d cairn --no-owner --exit-on-error' \
    < "$IN/cairn-postgres.dump"

log "mysql: restoring database cairn"
$COMPOSE exec -T mysql sh -c 'mysql -uroot -p"$MYSQL_ROOT_PASSWORD" -e "drop database if exists cairn" 2>/dev/null'
gzip -dc "$IN/cairn-mysql.sql.gz" | $COMPOSE exec -T mysql sh -c 'exec mysql -uroot -p"$MYSQL_ROOT_PASSWORD" 2>/dev/null'

log "gateway: restoring identity keys"
$COMPOSE run --rm --no-deps -T --entrypoint sh cairn-gateway \
    -c 'find /var/lib/cairn/keys -mindepth 1 -delete && tar -xzf - -C /var/lib/cairn/keys' < "$IN/gateway-keys.tar.gz"

if [ "${RESTORE_CONFIG:-0}" = 1 ]; then
    log "config: unpacking the backup's config/"
    tar -xzf "$IN/config.tar.gz"
fi

log "search: rebuilding the index from the catalog database"
$COMPOSE run --rm cairn-setup -u SystemUpdate >/dev/null
$COMPOSE run --rm cairn-setup -u RestoreIndices -a clean >/dev/null

log "starting Cairn"
$COMPOSE up -d --wait
log "restore complete"
