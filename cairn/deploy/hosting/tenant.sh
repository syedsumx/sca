#!/usr/bin/env bash
# Provision, update and remove hosted tenants: one Cairn release per tenant, in namespace cairn-<id>.
#
#   deploy/hosting/tenant.sh create <id> --licence FILE [--values platform.yaml] [--dry-run]
#   deploy/hosting/tenant.sh upgrade <id> [--licence FILE] [--values platform.yaml]
#   deploy/hosting/tenant.sh delete <id> [--drop-data]
#   deploy/hosting/tenant.sh list
#
# Shared by the platform (environment, or the --values file for anything chart-level):
#   CAIRN_HOST_DOMAIN     tenants are served at https://<id>.<domain>
#   CAIRN_PG_HOST         PostgreSQL server for every tenant's databases (pgvector installed)
#   CAIRN_PG_ADMIN_URL    admin connection used to create and drop tenant databases and roles
#   CAIRN_SEARCH_HOST     OpenSearch cluster; each tenant gets the index prefix "<id>_"
#   CAIRN_KAFKA_TEMPLATE  Kafka bootstrap per tenant, "{id}" replaced (e.g. kafka.cairn-{id}:9092)
#
# Per tenant it creates: databases cairn_<id> and cairn_catalog_<id> owned by role cairn_<id>, the
# namespace with a ResourceQuota, the credentials Secrets (random, never printed), the licence Secret
# and the release (helm/cairn/values-tenant.yaml). The licence must be bound to the tenant id.
set -euo pipefail

cd "$(dirname "$0")/../.."
CHART=helm/cairn
HELM=${HELM:-helm}
cmd=${1:-}; id=${2:-}
shift $(( $# >= 2 ? 2 : $# ))
licence=""; values=(); drop=0; dry=0
while [ $# -gt 0 ]; do
    case "$1" in
        --licence) licence=$2; shift 2 ;;
        --values) values+=(-f "$2"); shift 2 ;;
        --drop-data) drop=1; shift ;;
        --dry-run) dry=1; shift ;;
        *) echo "unknown option $1" >&2; exit 64 ;;
    esac
done
die() { echo "tenant: $*" >&2; exit 1; }
need() { for v in "$@"; do [ -n "${!v:-}" ] || die "set $v"; done; }
ns() { echo "cairn-$id"; }
rand() { openssl rand -hex 32; }

valid_id() {
    [[ "$id" =~ ^[a-z][a-z0-9-]{1,28}[a-z0-9]$ ]] || die "tenant id: 3-30 of a-z, 0-9 and -, starting with a letter"
}
sql_id() { echo "${id//-/_}"; }

helm_args() {
    local db; db=$(sql_id)
    echo --namespace "$(ns)" -f "$CHART/values.yaml" -f "$CHART/values-tenant.yaml" "${values[@]}" \
        --set "core.database.host=$CAIRN_PG_HOST" --set "core.database.name=cairn_catalog_$db" \
        --set "core.database.user=cairn_$db" \
        --set "core.search.host=$CAIRN_SEARCH_HOST" --set "core.search.indexPrefix=${db}_" \
        --set "core.kafka.bootstrap=${CAIRN_KAFKA_TEMPLATE//\{id\}/$id}" \
        --set "core.ingress.host=$id.$CAIRN_HOST_DOMAIN" \
        --set "cairn.publicUrl=https://$id.$CAIRN_HOST_DOMAIN" \
        --set "cairn.licence.tenantId=$id"
}

render() {   # what create would apply, without touching a cluster or a database
    # shellcheck disable=SC2046
    $HELM template "cairn-$id" "$CHART" $(helm_args)
}

create() {
    valid_id
    need CAIRN_HOST_DOMAIN CAIRN_PG_HOST CAIRN_SEARCH_HOST CAIRN_KAFKA_TEMPLATE
    [ -n "$licence" ] || die "--licence FILE is required"
    PYTHONPATH=src "${PYTHON:-python3}" -c 'import sys; from pathlib import Path
from cairn.common.licence import load_keys, verify
lic = verify(open(sys.argv[1]).read(), keys=load_keys(Path("deploy/licence/licence-keys.json")), tenant=sys.argv[2])
assert lic.tenant == sys.argv[2], "the licence must name the tenant"' "$licence" "$id" \
        || die "the licence is not valid for tenant $id"
    if [ $dry = 1 ]; then render; return; fi
    need CAIRN_PG_ADMIN_URL
    kubectl get namespace "$(ns)" >/dev/null 2>&1 && die "tenant $id exists (use upgrade)"

    local db pw; db=$(sql_id); pw=$(rand)
    echo "databases: cairn_$db, cairn_catalog_$db" >&2
    psql "$CAIRN_PG_ADMIN_URL" -v ON_ERROR_STOP=1 -q <<SQL
create role cairn_$db login password '$pw';
create database cairn_$db owner cairn_$db;
create database cairn_catalog_$db owner cairn_$db;
\connect cairn_$db
create extension if not exists vector;
create schema if not exists cairn authorization cairn_$db;
SQL
    echo "namespace $(ns)" >&2
    kubectl create namespace "$(ns)"
    kubectl label namespace "$(ns)" cairn.io/tenant="$id"
    kubectl -n "$(ns)" create resourcequota cairn --hard=requests.cpu=8,requests.memory=24Gi,limits.memory=48Gi,persistentvolumeclaims=10
    kubectl -n "$(ns)" create secret generic cairn-core-secrets \
        --from-literal=admin-password="$(openssl rand -base64 24)" \
        --from-literal=system-client-secret="$(rand)" --from-literal=session-signing-key="$(rand)" \
        --from-literal=token-salt="$(rand)" --from-literal=web-secret="$(rand)" --from-literal=db-password="$pw" >/dev/null
    kubectl -n "$(ns)" create secret generic cairn-secrets \
        --from-literal=service-token="$(rand)" \
        --from-literal=database-url="postgresql+psycopg://cairn_$db:$pw@$CAIRN_PG_HOST:5432/cairn_$db" \
        --from-literal=action-link-secret="$(rand)" --from-literal=embed-signing-secret="$(rand)" >/dev/null
    kubectl -n "$(ns)" create secret generic cairn-licence --from-file=licence="$licence" >/dev/null
    # shellcheck disable=SC2046
    $HELM install "cairn-$id" "$CHART" $(helm_args) --wait --timeout 20m
    echo "tenant $id: https://$id.$CAIRN_HOST_DOMAIN (admin password in secret cairn-core-secrets)" >&2
}

upgrade() {
    valid_id
    need CAIRN_HOST_DOMAIN CAIRN_PG_HOST CAIRN_SEARCH_HOST CAIRN_KAFKA_TEMPLATE
    if [ -n "$licence" ]; then
        kubectl -n "$(ns)" create secret generic cairn-licence --from-file=licence="$licence" \
            --dry-run=client -o yaml | kubectl apply -f - >/dev/null
    fi
    # shellcheck disable=SC2046
    $HELM upgrade "cairn-$id" "$CHART" $(helm_args) --wait --timeout 20m
}

delete() {
    valid_id
    $HELM uninstall "cairn-$id" --namespace "$(ns)" --wait || true
    kubectl delete namespace "$(ns)" --wait=true || true
    if [ $drop = 1 ]; then
        need CAIRN_PG_ADMIN_URL
        local db; db=$(sql_id)
        psql "$CAIRN_PG_ADMIN_URL" -v ON_ERROR_STOP=1 -q -c "drop database if exists cairn_$db" \
            -c "drop database if exists cairn_catalog_$db" -c "drop role if exists cairn_$db"
        echo "tenant $id: databases dropped; delete its indices with prefix ${db}_ and its Kafka" >&2
    else
        echo "tenant $id: release removed; its databases are kept (--drop-data removes them)" >&2
    fi
}

case "$cmd" in
    create) create ;;
    upgrade) upgrade ;;
    delete) delete ;;
    list) kubectl get namespaces -l cairn.io/tenant -o custom-columns=TENANT:.metadata.labels.cairn\\.io/tenant,NAMESPACE:.metadata.name,AGE:.metadata.creationTimestamp ;;
    *) sed -n '2,8p' "$0"; exit 64 ;;
esac
