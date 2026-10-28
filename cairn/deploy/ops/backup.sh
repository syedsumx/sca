#!/usr/bin/env bash
# Back up a Docker Compose deployment of Cairn: the Cairn database (Postgres), the catalog's
# database (MySQL, or Postgres with docker-compose.postgres.yml), the gateway's identity keys and
# the config directory. The search index is not backed up: it is rebuilt from the catalog database on
# restore (deploy/ops/restore.sh).
#
#   deploy/ops/backup.sh [OUT_DIR]          default: backups/cairn-<UTC timestamp>
#
# Both dumps are consistent snapshots taken while Cairn runs (pg_dump, mysqldump
# --single-transaction). COMPOSE overrides the compose command, e.g.
#   COMPOSE="docker compose -f docker-compose.cairn.yml -f docker-compose.override.yml"
set -euo pipefail

cd "$(dirname "$0")/../.."
COMPOSE=${COMPOSE:-docker compose -f docker-compose.cairn.yml}
OUT=${1:-backups/cairn-$(date -u +%Y%m%dT%H%M%SZ)}
mkdir -p "$OUT"
log() { printf '%s %s\n' "$(date -u +%H:%M:%S)" "$*" >&2; }

log "postgres: dumping database cairn"
$COMPOSE exec -T cairn-postgres sh -c 'exec pg_dump -U "$POSTGRES_USER" -d cairn -Fc --no-owner' \
    > "$OUT/cairn-postgres.dump"

if $COMPOSE ps --services --status running | grep -qx mysql; then
    catalog=cairn-mysql.sql.gz
    log "mysql: dumping the catalog database cairn"
    $COMPOSE exec -T mysql sh -c 'exec mysqldump -uroot -p"$MYSQL_ROOT_PASSWORD" --single-transaction \
        --quick --routines --triggers --hex-blob --set-gtid-purged=OFF --databases cairn 2>/dev/null' \
        | gzip -c > "$OUT/$catalog"
    gzip -dc "$OUT/$catalog" | tail -1 | grep -q 'Dump completed' \
        || { log "mysql: dump is incomplete"; exit 1; }
else                                    # docker-compose.postgres.yml: the catalog is in cairn-postgres
    catalog=cairn-catalog-postgres.dump
    log "postgres: dumping the catalog database cairn_catalog"
    $COMPOSE exec -T cairn-postgres sh -c 'exec pg_dump -U "$POSTGRES_USER" -d cairn_catalog -Fc --no-owner' \
        > "$OUT/$catalog"
fi

log "gateway: archiving identity keys"
$COMPOSE exec -T cairn-gateway tar -C /var/lib/cairn/keys -czf - . > "$OUT/gateway-keys.tar.gz"

log "config: archiving config/"
tar -czf "$OUT/config.tar.gz" --exclude='*.example' config

version=$(sed -n 's/^version *= *"\(.*\)"/\1/p' pyproject.toml | head -1)
revision=$($COMPOSE exec -T cairn-postgres psql -U cairn -d cairn -Atc 'select version_num from alembic_version' \
    2>/dev/null | tr -d '\r' || true)
{
    printf '{\n  "created_at": "%s",\n  "cairn_version": "%s",\n  "alembic_revision": "%s",\n  "files": {\n' \
        "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$version" "$revision"
    first=1
    for f in cairn-postgres.dump "$catalog" gateway-keys.tar.gz config.tar.gz; do
        [ $first = 1 ] || printf ',\n'
        first=0
        printf '    "%s": {"sha256": "%s", "bytes": %s}' "$f" \
            "$(sha256sum "$OUT/$f" | cut -d' ' -f1)" "$(wc -c < "$OUT/$f" | tr -d ' ')"
    done
    printf '\n  }\n}\n'
} > "$OUT/manifest.json"
chmod 600 "$OUT"/*
log "backup written to $OUT"
echo "$OUT"
