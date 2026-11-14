#!/usr/bin/env bash
# Rehearse an upgrade on this machine: install FROM_REF fresh, fill it with data, upgrade it to the
# working tree with the documented steps (backup, new images, compose up) and check that nothing was
# lost. Run it before every release and every bump of core/UPSTREAM_VERSION.
#
#   deploy/ops/rehearse-upgrade.sh FROM_REF [--keep]
#
# FROM_REF is a git ref of the previous release (a tag, or a commit). The working tree's images must
# already be built (deploy/build-images.sh). DESTROYS the local Compose stack's volumes. The images
# that differ between releases (platform, consumer-ui) are built from FROM_REF; Core images are
# shared, so a Core bump is rehearsed by building the new Core images first.
#
# Checks: the schema reaches the new head (cairn db check), the audit chain verifies with the same
# head, no Cairn table lost rows (except short-lived nonces), and the new release's e2e suite passes.
set -euo pipefail

FROM=${1:?usage: rehearse-upgrade.sh FROM_REF [--keep]}
KEEP=${2:-}
cd "$(dirname "$0")/../.."
ROOT=$(git rev-parse --show-toplevel)
WORK=$(mktemp -d "${TMPDIR:-/tmp}/cairn-rehearse.XXXXXX")
OLD="$WORK/old"
PREFIX=${CAIRN_REGISTRY:-cairn}
VERSION=${CAIRN_VERSION:-0.1.0}
PYTHON=${PYTHON:-python3}
COMPOSE=(docker compose -f docker-compose.cairn.yml -f tests/e2e/docker-compose.sso.yml)
log() { printf '\n== %s %s\n' "$(date -u +%H:%M:%S)" "$*" >&2; }
psql_() { docker compose -f docker-compose.cairn.yml exec -T cairn-postgres psql -U cairn -d cairn -Atc "$1"; }

fingerprint() {   # one line per Cairn table: name rows|md5 of the rows
    psql_ "select version_num from alembic_version"
    for t in $(psql_ "select tablename from pg_tables where schemaname = 'cairn' order by 1"); do
        echo "$t $(psql_ "select count(*), md5(coalesce(string_agg(t::text, '|' order by t::text), '')) from cairn.$t t")"
    done
}

restore_images() {
    for img in platform consumer-ui; do
        docker image inspect "$PREFIX/$img:rehearse-new" >/dev/null 2>&1 \
            && docker tag "$PREFIX/$img:rehearse-new" "$PREFIX/$img:$VERSION"
    done
}
cleanup() {
    restore_images
    git -C "$ROOT" worktree remove --force "$OLD" 2>/dev/null || true
    [ -n "$KEEP" ] || rm -rf "$WORK"
}
trap cleanup EXIT

log "building $FROM's images (the new ones are kept as :rehearse-new)"
for img in platform consumer-ui; do
    docker tag "$PREFIX/$img:$VERSION" "$PREFIX/$img:rehearse-new"
done
git -C "$ROOT" worktree add -q "$OLD" "$FROM"
cp .env "$OLD/cairn/.env"
(cd "$OLD/cairn" && ONLY="platform consumer-ui" deploy/build-images.sh >"$WORK/build-old.log" 2>&1) \
    || { tail -20 "$WORK/build-old.log"; exit 1; }

log "installing $FROM on fresh volumes"
"${COMPOSE[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
(cd "$OLD/cairn" && "${COMPOSE[@]}" up -d --no-build --wait >/dev/null)

log "filling $FROM with data (its own smoke and active-metadata tests; failures there are $FROM's)"
(cd "$OLD/cairn" && KEEP_SINK=1 PYTHON="$PYTHON" tests/e2e/run.sh -q -p no:cacheprovider \
    tests/e2e/test_stack_smoke.py tests/e2e/test_active_metadata_live.py >"$WORK/seed.log" 2>&1) || true
grep -E "^(FAILED|ERROR) " "$WORK/seed.log" >&2 || true
fingerprint > "$WORK/before.txt"
head_before=$(docker compose -f docker-compose.cairn.yml exec -T cairn-api cairn audit verify | sed -n 's/.*"head": "\(.*\)".*/\1/p')

log "upgrading: backup, new images, compose up"
deploy/ops/backup.sh "$WORK/backup" >/dev/null
restore_images
started=$(date +%s)
"${COMPOSE[@]}" up -d --no-build --wait >/dev/null
log "the new release was up in $(( $(date +%s) - started ))s"

log "checking"
docker compose -f docker-compose.cairn.yml exec -T cairn-api cairn db check
audit=$(docker compose -f docker-compose.cairn.yml exec -T cairn-api cairn audit verify)
grep -q '"valid": true' <<<"$audit" || { echo "audit chain broken: $audit"; exit 1; }
grep -q "\"head\": \"$head_before\"" <<<"$audit" || echo "note: new audit entries since the upgrade" >&2
fingerprint > "$WORK/after.txt"
echo "schema $(head -1 "$WORK/before.txt") -> $(head -1 "$WORK/after.txt")"
lost=$(join <(tail -n +2 "$WORK/before.txt" | sort) <(tail -n +2 "$WORK/after.txt" | sort) | awk '
    { split($2, a, "|"); split($3, b, "|") }
    $1 == "cairn_agent_nonces" { next }                              # replay nonces expire
    $2 == $3 { same++; next }
    b[1] + 0 < a[1] + 0 { printf "%s %s->%s rows\n", $1, a[1], b[1] }
    END { printf "%d tables unchanged\n", same > "/dev/stderr" }')
dropped=$(comm -23 <(tail -n +2 "$WORK/before.txt" | cut -d' ' -f1 | sort) \
                   <(tail -n +2 "$WORK/after.txt" | cut -d' ' -f1 | sort))
echo "new tables: $(comm -13 <(tail -n +2 "$WORK/before.txt" | cut -d' ' -f1 | sort) \
                            <(tail -n +2 "$WORK/after.txt" | cut -d' ' -f1 | sort) | tr '\n' ' ')"
if [ -n "$lost$dropped" ]; then
    echo "DATA LOST in the upgrade:"; echo "$lost"; echo "$dropped"; exit 1
fi

log "running the new release's e2e suite on the upgraded stack"
KEEP_SINK=1 PYTHON="$PYTHON" tests/e2e/run.sh -q -rfs -p no:cacheprovider
log "upgrade from $FROM rehearsed: no data lost, schema at head, e2e green"
