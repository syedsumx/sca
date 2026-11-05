#!/usr/bin/env bash
# shellcheck disable=SC2034  # the manifests use several variables through fill
# Installs the services every tenant shares, on the cluster in $KUBECONFIG (cluster.sh platform runs it):
#
#   ingress-nginx   the one entry point, behind the Hetzner load balancer (node ports 30080/30443, PROXY
#                   protocol, so tenants see their users' addresses)
#   cert-manager    Let's Encrypt certificates for each tenant's host (HTTP-01)
#   CloudNativePG   the shared PostgreSQL cluster (pgvector): one primary and replicas on different nodes,
#                   continuous WAL archiving and daily base backups to Hetzner Object Storage
#   monitoring      kube-prometheus-stack (MONITORING=true): Prometheus scrapes Cairn's ServiceMonitors
#
# Each tenant's own OpenSearch and Kafka are created with the tenant (deploy/hosting/tenant.sh).
# Chart versions are pinned below; change them here, after reading their upgrade notes.
set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd)
env_file=${1:?usage: platform.sh <cluster.env>}
# shellcheck disable=SC1090
source "$env_file"
die() { echo "platform: $*" >&2; exit 1; }
for c in helm kubectl; do command -v "$c" >/dev/null || die "needs $c"; done
: "${ACME_EMAIL:?set ACME_EMAIL, the Lets Encrypt account}"

INGRESS_NGINX_CHART=4.13.0
CERT_MANAGER_CHART=v1.18.2
CNPG_CHART=0.26.0
KPS_CHART=77.0.0
PG_IMAGE=${PG_IMAGE:-ghcr.io/cloudnative-pg/postgresql:16.10-standard-bookworm}
PG_INSTANCES=${PG_INSTANCES:-3}
PG_STORAGE=${PG_STORAGE:-200Gi}
PG_MEMORY=${PG_MEMORY:-4Gi}
PG_CPU=${PG_CPU:-1}
LB_SOURCE_CIDRS=${LB_SOURCE_CIDRS:-}
MONITORING=${MONITORING:-true}
BACKUPS=false
[ -z "${OBJECT_STORAGE_ACCESS_KEY:-}" ] || BACKUPS=true

# @VAR@ in a manifest -> the value of that variable (in bash: values may hold any character).
fill() {
    local file=$1 text v
    text=$(<"$file"); shift
    for v in "$@"; do text=${text//@$v@/${!v}}; done
    if grep -qE '@[A-Z_]+@' <<<"$text"; then die "$file: unfilled $(grep -oE '@[A-Z_]+@' <<<"$text" | sort -u | tr '\n' ' ')"; fi
    printf '%s\n' "$text"
}
ns() {   # namespace with its role label (network policies select namespaces by it)
    kubectl create namespace "$1" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
    kubectl label namespace "$1" "cairn.io/role=$2" --overwrite >/dev/null
}
retry() {   # a command that fetches from the internet: GitHub and chart repositories answer 5xx now and then
    local i
    for i in 1 2 3 4 5; do "$@" && return 0; [ "$i" = 5 ] && break; echo "  retrying in $((i * 10))s: $*" >&2; sleep $((i * 10)); done
    return 1
}
repo() { retry helm repo add "$1" "$2" --force-update >/dev/null; }
# A chart fetched first (retried), then installed from the file: a download that fails is tried again, a
# rollout that fails is not.
charts=$(mktemp -d)
trap 'rm -rf "$charts"' EXIT
chart() {   # repo/chart version -> the downloaded chart's path
    local dir="$charts/${1//\//_}-$2"
    mkdir -p "$dir"
    retry helm pull "$1" --version "$2" -d "$dir" >/dev/null || die "could not download chart $1 $2"
    ls "$dir"/*.tgz
}
# An operator's webhook can take a moment to serve after its chart is "ready".
apply_retry() {
    local manifest; manifest=$(cat)
    for _ in $(seq 30); do kubectl apply -f - <<<"$manifest" && return 0; sleep 5; done
    die "could not apply (the operator's webhook does not answer)"
}

repo ingress-nginx https://kubernetes.github.io/ingress-nginx
repo jetstack https://charts.jetstack.io
repo cnpg https://cloudnative-pg.github.io/charts
[ "$MONITORING" != true ] || repo prometheus-community https://prometheus-community.github.io/helm-charts
retry helm repo update >/dev/null

ns cairn-platform platform; ns ingress-nginx ingress; ns cert-manager platform; ns cnpg-system platform
[ "$MONITORING" != true ] || ns monitoring monitoring

# First: the others register their ServiceMonitors and PodMonitors with it.
if [ "$MONITORING" = true ]; then
    echo "== kube-prometheus-stack $KPS_CHART" >&2
    if ! kubectl -n monitoring get secret grafana-admin >/dev/null 2>&1; then
        kubectl -n monitoring create secret generic grafana-admin --from-literal=admin-user=admin \
            --from-file=admin-password=<(openssl rand -base64 24 | tr -d '\n') >/dev/null
    fi
    helm upgrade --install monitoring "$(chart prometheus-community/kube-prometheus-stack "$KPS_CHART")" \
        -n monitoring -f "$here/platform/monitoring.yaml" --wait --timeout 15m
fi

echo "== ingress-nginx $INGRESS_NGINX_CHART" >&2
# The load balancer's addresses are the only PROXY-protocol senders trusted for the client address.
REAL_IP_CIDRS=${LB_SOURCE_CIDRS:-0.0.0.0/32}
REAL_IP_CIDRS=${REAL_IP_CIDRS// /}
fill "$here/platform/ingress-nginx.yaml" REAL_IP_CIDRS MONITORING >"${TMPDIR:-/tmp}/cairn-ingress-values.yaml"
helm upgrade --install ingress-nginx "$(chart ingress-nginx/ingress-nginx "$INGRESS_NGINX_CHART")" \
    -n ingress-nginx -f "${TMPDIR:-/tmp}/cairn-ingress-values.yaml" --wait --timeout 10m
rm -f "${TMPDIR:-/tmp}/cairn-ingress-values.yaml"

echo "== cert-manager $CERT_MANAGER_CHART" >&2
helm upgrade --install cert-manager "$(chart jetstack/cert-manager "$CERT_MANAGER_CHART")" -n cert-manager \
    --set crds.enabled=true --set prometheus.enabled="$MONITORING" --wait --timeout 10m
fill "$here/platform/cluster-issuers.yaml" ACME_EMAIL | apply_retry

echo "== CloudNativePG $CNPG_CHART, PostgreSQL ($PG_INSTANCES instances, $PG_IMAGE)" >&2
helm upgrade --install cnpg "$(chart cnpg/cloudnative-pg "$CNPG_CHART")" -n cnpg-system --wait --timeout 10m
PG_ANTI_AFFINITY=required
[ "$PG_INSTANCES" -gt 1 ] || PG_ANTI_AFFINITY=preferred
if [ "$BACKUPS" = true ]; then
    kubectl -n cairn-platform create secret generic cairn-pg-backup \
        --from-file=ACCESS_KEY_ID=<(printf %s "$OBJECT_STORAGE_ACCESS_KEY") \
        --from-file=ACCESS_SECRET_KEY=<(printf %s "$OBJECT_STORAGE_SECRET_KEY") \
        --dry-run=client -o yaml | kubectl apply -f - >/dev/null
    PG_BUCKET="$BUCKET_PREFIX-postgres"
    BACKUP_BLOCK=$(fill "$here/platform/postgres-backup.yaml" PG_BUCKET OBJECT_STORAGE_ENDPOINT)
else
    echo "   no OBJECT_STORAGE_ACCESS_KEY: PostgreSQL runs WITHOUT backups (only acceptable for a test cluster)" >&2
    BACKUP_BLOCK=""
fi
MONITORING_PODMONITOR=$MONITORING
fill "$here/platform/postgres.yaml" PG_IMAGE PG_INSTANCES PG_STORAGE PG_MEMORY PG_CPU PG_ANTI_AFFINITY \
    BACKUP_BLOCK MONITORING_PODMONITOR | apply_retry
[ "$BACKUPS" != true ] || apply_retry <"$here/platform/postgres-scheduled-backup.yaml"
kubectl apply -f "$here/platform/networkpolicies.yaml"
echo "   waiting for PostgreSQL (first start takes a few minutes)" >&2
kubectl -n cairn-platform wait --for=condition=Ready cluster.postgresql.cnpg.io/cairn-pg --timeout 20m


cat >&2 <<EOF

Platform ready. Tenants: set these for deploy/hosting/tenant.sh (also in cluster.env.example):
  CAIRN_PG_HOST=cairn-pg-rw.cairn-platform.svc   CAIRN_PG_KUBECTL=cairn-platform/cairn-pg
  CAIRN_TENANT_SERVICES=1   CAIRN_INGRESS_NAMESPACE=ingress-nginx   CAIRN_TLS_ISSUER=letsencrypt
  CAIRN_POD_SECURITY=baseline   $( [ "$MONITORING" = true ] && echo "CAIRN_MONITORING_NAMESPACE=monitoring" )
and pass --values deploy/hetzner/platform-values.yaml.
EOF
