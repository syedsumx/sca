#!/usr/bin/env bash
# Provision, update and remove hosted tenants: one Cairn release per tenant, in namespace cairn-<id>.
#
#   deploy/hosting/tenant.sh create <id> --licence FILE [--metering-key FILE] [--values platform.yaml] [--dry-run]
#   deploy/hosting/tenant.sh upgrade <id> [--licence FILE] [--values platform.yaml]
#   deploy/hosting/tenant.sh delete <id> [--drop-data]
#   deploy/hosting/tenant.sh list
#   deploy/hosting/tenant.sh render-services <id>   # the tenant's own services and isolation, as applied
#
# Shared by the platform (environment, or the --values file for anything chart-level):
#   CAIRN_HOST_DOMAIN     tenants are served at https://<id>.<domain>
#   CAIRN_PG_HOST         PostgreSQL server for every tenant's databases (pgvector installed)
#   CAIRN_PG_ADMIN_URL    admin connection used to create and drop tenant databases and roles
#   CAIRN_SEARCH_HOST     OpenSearch cluster; each tenant gets the index prefix "<id>_"
#   CAIRN_KAFKA_TEMPLATE  Kafka bootstrap per tenant, "{id}" replaced (e.g. kafka.cairn-{id}:9092)
#
# Optional (deploy/hetzner sets them all):
#   CAIRN_PG_KUBECTL      <namespace>/<cluster>: administer a CloudNativePG cluster through its primary's
#                         local socket (kubectl exec) instead of CAIRN_PG_ADMIN_URL
#   CAIRN_TENANT_SERVICES 1: each tenant gets its own OpenSearch and Kafka in its namespace
#                         (tenant-services.yaml); CAIRN_SEARCH_HOST and CAIRN_KAFKA_TEMPLATE are then unused
#   CAIRN_TENANT_SERVICES_HA  1: those services replicated, three nodes each, so a lost node or pod is survived
#                         (tenant-services-ha.yaml). Chosen when a tenant is created: an existing tenant is
#                         not switched in place (see "Hosting tenants" in docs/site/administration.md)
#   CAIRN_TENANT_SAMPLES   a sample set (retail) the new tenant's lakehouse starts with, loaded once (samples)
#   CAIRN_TENANT_SERVICES_SPREAD  DoNotSchedule (default: one pod of each per node, needs three nodes) or
#                         ScheduleAnyway (a single test node)
#   CAIRN_TENANT_SEARCH_HEAP, CAIRN_TENANT_SEARCH_MEMORY, CAIRN_TENANT_KAFKA_HEAP, CAIRN_TENANT_KAFKA_MEMORY
#                         per-node sizes of the replicated services (defaults: 1g heap in 2Gi, 512m in 1Gi)
#   CAIRN_INGRESS_NAMESPACE  the ingress controller's namespace: turns on namespace isolation (isolation.yaml)
#   CAIRN_MONITORING_NAMESPACE  Prometheus's namespace, also admitted by the isolation
#   CAIRN_TLS_ISSUER      a cert-manager ClusterIssuer: each tenant's host gets its certificate from it
#   CAIRN_POD_SECURITY    Pod Security level enforced in tenant namespaces (baseline, restricted)
#   CAIRN_GATEWAY         <namespace>/<name>[/<listener>]: route each tenant through this shared Gateway API
#                         Gateway (HTTPRoutes, core.gateway) instead of an Ingress; TLS is the Gateway's
#                         (a wildcard certificate for *.<domain>), and its routes must be allowed from tenants
#   CAIRN_METERING_URL    the control plane the tenant reports its usage to (with --metering-key: the
#                         tenant's key, kept in Secret cairn-metering; see cairn.hosting.metering)
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
licence=""; metering=""; values=(); drop=0; dry=0
while [ $# -gt 0 ]; do
    case "$1" in
        --licence) licence=$2; shift 2 ;;
        --metering-key) metering=$2; shift 2 ;;
        --values) values+=(-f "$2"); shift 2 ;;
        --drop-data) drop=1; shift ;;
        --dry-run) dry=1; shift ;;
        *) echo "unknown option $1" >&2; exit 64 ;;
    esac
done
die() { echo "tenant: $*" >&2; exit 1; }
need() { for v in "$@"; do [ -n "${!v:-}" ] || die "set $v"; done; }
ns() { echo "cairn-$id"; }
services() { [ "${CAIRN_TENANT_SERVICES:-}" = 1 ]; }
ha() { services && [ "${CAIRN_TENANT_SERVICES_HA:-}" = 1 ]; }
search_host() { if services; then echo "opensearch.$(ns).svc"; else echo "$CAIRN_SEARCH_HOST"; fi; }
kafka_bootstrap() {
    if ha; then local d; d="kafka-headless.$(ns).svc:9092"; echo "kafka-0.$d,kafka-1.$d,kafka-2.$d"
    elif services; then echo "kafka.$(ns).svc:9092"; else echo "${CAIRN_KAFKA_TEMPLATE//\{id\}/$id}"; fi
}
need_shared() {
    need CAIRN_HOST_DOMAIN CAIRN_PG_HOST
    services || need CAIRN_SEARCH_HOST CAIRN_KAFKA_TEMPLATE
}
# shellcheck disable=SC2120  # callers may pass psql options
pg_admin() {   # psql as the server's administrator, statements on stdin
    if [ -n "${CAIRN_PG_KUBECTL:-}" ]; then
        local pns=${CAIRN_PG_KUBECTL%%/*} cluster=${CAIRN_PG_KUBECTL#*/} pod
        pod=$(kubectl -n "$pns" get pod -l "cnpg.io/cluster=$cluster,cnpg.io/instanceRole=primary" -o name | head -1)
        [ -n "$pod" ] || die "no primary for PostgreSQL cluster $CAIRN_PG_KUBECTL"
        kubectl -n "$pns" exec -i "$pod" -c postgres -- psql -U postgres -v ON_ERROR_STOP=1 -q "$@"
    else
        need CAIRN_PG_ADMIN_URL
        psql "$CAIRN_PG_ADMIN_URL" -v ON_ERROR_STOP=1 -q "$@"
    fi
}
# @VAR@ in a manifest -> the variable's value.
fill() {
    local file=$1 text v
    text=$(<"$file"); shift
    for v in "$@"; do text=${text//@$v@/${!v}}; done
    if grep -qE '@[A-Z_]+@' <<<"$text"; then die "$file: unfilled $(grep -oE '@[A-Z_]+@' <<<"$text" | sort -u | tr '\n' ' ')"; fi
    printf '%s\n' "$text"
}
# shellcheck disable=SC2034  # the manifests use these through fill
render_services() {   # isolation (CAIRN_INGRESS_NAMESPACE) and the tenant's own services (CAIRN_TENANT_SERVICES)
    local NS RELEASE INGRESS_NAMESPACE MONITORING_FROM KAFKA_CLUSTER_ID SEARCH_STORAGE KAFKA_STORAGE MODE WAS
    local SPREAD SEARCH_HEAP SEARCH_MEMORY KAFKA_HEAP KAFKA_MEMORY
    NS=$(ns); RELEASE="cairn-$id"
    if [ -n "${CAIRN_INGRESS_NAMESPACE:-}" ]; then
        INGRESS_NAMESPACE=$CAIRN_INGRESS_NAMESPACE; MONITORING_FROM=""
        if [ -n "${CAIRN_MONITORING_NAMESPACE:-}" ]; then
            MONITORING_FROM="        - namespaceSelector:
            matchLabels: { kubernetes.io/metadata.name: $CAIRN_MONITORING_NAMESPACE }"
        fi
        fill deploy/hosting/isolation.yaml NS RELEASE INGRESS_NAMESPACE MONITORING_FROM
        echo ---
    fi
    if services; then
        # Kafka's cluster id is kept with the tenant: a new one would not match the data already on disk.
        KAFKA_CLUSTER_ID=$(kubectl -n "$NS" get configmap cairn-tenant-services -o jsonpath='{.data.kafka-cluster-id}' 2>/dev/null || true)
        [ -n "$KAFKA_CLUSTER_ID" ] || KAFKA_CLUSTER_ID=$(openssl rand -base64 16 | tr '+/' '-_' | tr -d '=')
        SEARCH_STORAGE=${CAIRN_TENANT_SEARCH_STORAGE:-20Gi}; KAFKA_STORAGE=${CAIRN_TENANT_KAFKA_STORAGE:-20Gi}
        # The services' layout is the tenant's: switching one in place would leave Kafka's quorum and the
        # StatefulSets half-changed, so it is refused (recreate the services, see the operator manual).
        if ha; then MODE=replicated; else MODE=single; fi
        WAS=$(kubectl -n "$NS" get configmap cairn-tenant-services -o jsonpath='{.data.layout}' 2>/dev/null || true)
        if [ -z "$WAS" ] && kubectl -n "$NS" get statefulset kafka >/dev/null 2>&1; then WAS=single; fi
        [ -z "$WAS" ] || [ "$WAS" = "$MODE" ] \
            || die "tenant $id has $WAS OpenSearch and Kafka; changing to $MODE is not done in place (see the operator manual)"
        cat <<YAML
apiVersion: v1
kind: ConfigMap
metadata: { name: cairn-tenant-services, namespace: $NS }
data: { kafka-cluster-id: "$KAFKA_CLUSTER_ID", layout: "$MODE" }
---
YAML
        if ha; then
            SPREAD=${CAIRN_TENANT_SERVICES_SPREAD:-DoNotSchedule}
            case "$SPREAD" in DoNotSchedule|ScheduleAnyway) ;; *) die "CAIRN_TENANT_SERVICES_SPREAD: DoNotSchedule or ScheduleAnyway" ;; esac
            SEARCH_HEAP=${CAIRN_TENANT_SEARCH_HEAP:-1g}; SEARCH_MEMORY=${CAIRN_TENANT_SEARCH_MEMORY:-2Gi}
            KAFKA_HEAP=${CAIRN_TENANT_KAFKA_HEAP:-512m}; KAFKA_MEMORY=${CAIRN_TENANT_KAFKA_MEMORY:-1Gi}
            SEARCH_HEAP="-Xms$SEARCH_HEAP -Xmx$SEARCH_HEAP"; KAFKA_HEAP="-Xms$KAFKA_HEAP -Xmx$KAFKA_HEAP"
            fill deploy/hosting/tenant-services-ha.yaml NS RELEASE KAFKA_CLUSTER_ID SEARCH_STORAGE KAFKA_STORAGE SPREAD \
                SEARCH_HEAP SEARCH_MEMORY KAFKA_HEAP KAFKA_MEMORY
        else
            fill deploy/hosting/tenant-services.yaml NS RELEASE KAFKA_CLUSTER_ID SEARCH_STORAGE KAFKA_STORAGE
        fi
    fi
}
apply_services() {
    local manifest; manifest=$(render_services)
    [ -n "$manifest" ] || return 0
    kubectl apply -f - <<<"$manifest" >/dev/null
    if services; then
        echo "waiting for the tenant's OpenSearch and Kafka" >&2
        kubectl -n "$(ns)" rollout status statefulset/opensearch --timeout 10m >&2
        kubectl -n "$(ns)" rollout status statefulset/kafka --timeout 10m >&2
    fi
}
edge_args() {   # a shared Gateway (CAIRN_GATEWAY=<namespace>/<name>[/<listener>]) instead of an Ingress per tenant
    [ -n "${CAIRN_GATEWAY:-}" ] || { tls_args; return 0; }
    local gns gname gsection
    IFS=/ read -r gns gname gsection <<<"$CAIRN_GATEWAY"
    [ -n "$gns" ] && [ -n "$gname" ] || die "CAIRN_GATEWAY: <namespace>/<name>[/<listener>]"
    echo --set core.ingress.enabled=false --set core.gateway.enabled=true \
        --set "core.gateway.parentRefs[0].namespace=$gns" --set "core.gateway.parentRefs[0].name=$gname" \
        ${gsection:+--set "core.gateway.parentRefs[0].sectionName=$gsection"}
}

tls_args() {
    [ -n "${CAIRN_TLS_ISSUER:-}" ] || return 0
    echo --set-string "core.ingress.annotations.cert-manager\.io/cluster-issuer=$CAIRN_TLS_ISSUER" \
        --set "core.ingress.tls[0].hosts[0]=$id.$CAIRN_HOST_DOMAIN" --set "core.ingress.tls[0].secretName=cairn-tls"
}
rand() { openssl rand -hex 32; }

valid_id() {
    [[ "$id" =~ ^[a-z][a-z0-9-]{1,28}[a-z0-9]$ ]] || die "tenant id: 3-30 of a-z, 0-9 and -, starting with a letter"
}
sql_id() { echo "${id//-/_}"; }

# shellcheck disable=SC2046  # helm_args is split into words on purpose
helm_args() {
    local db; db=$(sql_id)
    echo --namespace "$(ns)" -f "$CHART/values.yaml" -f "$CHART/values-ha.yaml" -f "$CHART/values-tenant.yaml" \
        "${values[@]}" \
        --set "core.database.host=$CAIRN_PG_HOST" --set "core.database.name=cairn_catalog_$db" \
        --set "core.database.user=cairn_$db" \
        --set "core.search.host=$(search_host)" --set "core.search.indexPrefix=${db}_" \
        --set "core.kafka.bootstrap=$(kafka_bootstrap | sed 's/,/\\,/g')" \
        --set "core.ingress.host=$id.$CAIRN_HOST_DOMAIN" \
        --set "cairn.publicUrl=https://$id.$CAIRN_HOST_DOMAIN" \
        --set "cairn.licence.tenantId=$id" $(edge_args) $(metering_args) \
        ${CAIRN_TENANT_SAMPLES:+--set "cairn.samples.onStart=$CAIRN_TENANT_SAMPLES"}
}

metering_args() {   # usage reports to the control plane, once the tenant has its metering key
    if [ -n "$metering" ] || { [ $dry = 0 ] && kubectl -n "$(ns)" get secret cairn-metering >/dev/null 2>&1; }; then
        need CAIRN_METERING_URL
        echo --set cairn.metering.enabled=true --set "cairn.metering.url=$CAIRN_METERING_URL"
    fi
}

metering_secret() {
    [ -n "$metering" ] || return 0
    kubectl -n "$(ns)" create secret generic cairn-metering --from-file=key="$metering" \
        --dry-run=client -o yaml | kubectl apply -f - >/dev/null
}

render() {   # what create would apply, without touching a cluster or a database
    # shellcheck disable=SC2046
    $HELM template "cairn-$id" "$CHART" $(helm_args)
}

create() {
    valid_id
    need_shared
    [ -n "$licence" ] || die "--licence FILE is required"
    PYTHONPATH=src "${PYTHON:-python3}" -c 'import sys; from pathlib import Path
from cairn.common.licence import load_keys, verify
lic = verify(open(sys.argv[1]).read(), keys=load_keys(Path("deploy/licence/licence-keys.json")), tenant=sys.argv[2])
assert lic.tenant == sys.argv[2], "the licence must name the tenant"' "$licence" "$id" \
        || die "the licence is not valid for tenant $id"
    if [ $dry = 1 ]; then render; return; fi
    kubectl get namespace "$(ns)" >/dev/null 2>&1 && die "tenant $id exists (use upgrade)"

    local db pw; db=$(sql_id); pw=$(rand)
    echo "databases: cairn_$db, cairn_catalog_$db" >&2
    pg_admin <<SQL
create role cairn_$db login password '$pw';
create database cairn_$db owner cairn_$db;
create database cairn_catalog_$db owner cairn_$db;
-- Other tenants' roles may not even connect to this tenant's databases.
revoke connect, temporary on database cairn_$db from public;
revoke connect, temporary on database cairn_catalog_$db from public;
\connect cairn_$db
create extension if not exists vector;
create schema if not exists cairn authorization cairn_$db;
SQL
    echo "namespace $(ns)" >&2
    kubectl create namespace "$(ns)"
    kubectl label namespace "$(ns)" cairn.io/tenant="$id"
    if [ -n "${CAIRN_POD_SECURITY:-}" ]; then
        kubectl label namespace "$(ns)" "pod-security.kubernetes.io/enforce=$CAIRN_POD_SECURITY" \
            pod-security.kubernetes.io/warn=restricted >/dev/null
    fi
    kubectl -n "$(ns)" create resourcequota cairn --hard=requests.cpu=8,requests.memory=24Gi,limits.memory=48Gi,persistentvolumeclaims=16
    apply_services
    kubectl -n "$(ns)" create secret generic cairn-core-secrets \
        --from-literal=admin-password="$(openssl rand -base64 24)" \
        --from-literal=system-client-secret="$(rand)" --from-literal=session-signing-key="$(rand)" \
        --from-literal=token-salt="$(rand)" --from-literal=web-secret="$(rand)" --from-literal=db-password="$pw" >/dev/null
    kubectl -n "$(ns)" create secret generic cairn-secrets \
        --from-literal=service-token="$(rand)" \
        --from-literal=database-url="postgresql+psycopg://cairn_$db:$pw@$CAIRN_PG_HOST:5432/cairn_$db" \
        --from-literal=action-link-secret="$(rand)" --from-literal=embed-signing-secret="$(rand)" >/dev/null
    kubectl -n "$(ns)" create secret generic cairn-licence --from-file=licence="$licence" >/dev/null
    metering_secret
    # shellcheck disable=SC2046
    $HELM install "cairn-$id" "$CHART" $(helm_args) --wait --timeout 20m
    echo "tenant $id: https://$id.$CAIRN_HOST_DOMAIN (admin password in secret cairn-core-secrets)" >&2
}

upgrade() {
    valid_id
    need_shared
    apply_services
    if [ -n "$licence" ]; then
        kubectl -n "$(ns)" create secret generic cairn-licence --from-file=licence="$licence" \
            --dry-run=client -o yaml | kubectl apply -f - >/dev/null
    fi
    metering_secret
    # shellcheck disable=SC2046
    $HELM upgrade "cairn-$id" "$CHART" $(helm_args) --wait --timeout 20m
}

delete() {
    valid_id
    $HELM uninstall "cairn-$id" --namespace "$(ns)" --wait || true
    kubectl delete namespace "$(ns)" --wait=true || true
    if [ $drop = 1 ]; then
        local db; db=$(sql_id)
        pg_admin <<SQL
drop database if exists cairn_$db;
drop database if exists cairn_catalog_$db;
drop role if exists cairn_$db;
SQL
        echo "tenant $id: databases dropped; delete its indices with prefix ${db}_ and its Kafka" >&2
    else
        echo "tenant $id: release removed; its databases are kept (--drop-data removes them)" >&2
    fi
}

case "$cmd" in
    create) create ;;
    upgrade) upgrade ;;
    delete) delete ;;
    render-services) valid_id; render_services ;;
    list) kubectl get namespaces -l cairn.io/tenant -o custom-columns=TENANT:.metadata.labels.cairn\\.io/tenant,NAMESPACE:.metadata.name,AGE:.metadata.creationTimestamp ;;
    *) sed -n '2,8p' "$0"; exit 64 ;;
esac
