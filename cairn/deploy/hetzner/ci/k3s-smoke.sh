#!/usr/bin/env bash
# CI: the Hetzner setup on one real machine (a GitHub runner standing in for a node). Runs as a user
# with sudo; needs docker-free Ubuntu 24.04 with internet access. What it does, in order:
#   1. node-prepare.sh renders the node's files; the firewall is checked and applied, the kernel settings
#      applied, a dummy interface stands in for the vSwitch VLAN
#   2. k3s from cluster.sh render-k3s (embedded etcd, WireGuard pod network, secrets encryption,
#      protect-kernel-defaults, audit log), behind that firewall
#   3. platform.sh: ingress-nginx, cert-manager, CloudNativePG + PostgreSQL, kube-prometheus-stack
#   4. a tenant created by tenant.sh (real databases, namespace, own OpenSearch and Kafka, isolation,
#      Pod Security); only the final Cairn release install is skipped (HELM stub), its manifests are
#      rendered with the real chart and checked by the API server instead
#   5. isolation: the tenant's release reaches its services and PostgreSQL; another tenant, the tenant's
#      own Spark pods and a namespace that is no tenant do not
set -euo pipefail

cd "$(dirname "$0")/../../.."      # the cairn/ directory
work=${RUNNER_TEMP:-/tmp}/cairn-hetzner-ci
rm -rf "$work"; mkdir -p "$work"
step() { echo; echo "=== $*"; }
fail() { echo "FAIL: $*" >&2; exit 1; }

cat >"$work/cluster.env" <<EOF
CLUSTER_NAME=ci
NODES=("ci-1 10.10.0.11 10.10.0.11")
VLAN_ID=4000
ADMIN_CIDRS="127.0.0.1/32"
LB_SOURCE_CIDRS="127.0.0.1/32"
K3S_VERSION=${K3S_VERSION:-v1.33.3+k3s1}
ACME_EMAIL=ci@cairn-ci.dev
PG_INSTANCES=1
PG_STORAGE=5Gi
PG_MEMORY=1Gi
PG_CPU=250m
MONITORING=true
STATE_DIR=$work/state
EOF

# Fail at once, and say so, if the node comes under disk pressure: evicted pods make every later check meaningless.
pressure() {
    if kubectl get node ci-1 -o jsonpath='{.status.conditions[?(@.type=="DiskPressure")].status}' 2>/dev/null | grep -q True; then
        df -h / >&2; fail "the node is under disk pressure: pods are being evicted (free disk on the runner)"
    fi
}
step "node: files, firewall, kernel settings"
public_if=$(ip -o route show default | awk '{print $5; exit}')
PUBLIC_IF=$public_if VLAN_ID=4000 PRIVATE_IP=10.10.0.11 ADMIN_CIDRS=127.0.0.1/32 LB_SOURCE_CIDRS=127.0.0.1/32 \
    deploy/hetzner/node-prepare.sh --render "$work/node"
sudo nft -c -f "$work/node/nftables.conf"
sudo ip link add vlan4000 type dummy
sudo ip addr add 10.10.0.11/24 dev vlan4000
sudo ip link set vlan4000 up
sudo cp "$work/node/90-cairn-kubernetes.conf" /etc/sysctl.d/ && sudo sysctl --system >/dev/null
sudo nft -f "$work/node/nftables.conf"
sudo nft list table inet cairn_host >/dev/null

step "k3s (the config cluster.sh installs on node 1)"
deploy/hetzner/cluster.sh "$work/cluster.env" render-k3s 0 >"$work/k3s.yaml"
deploy/hetzner/cluster.sh "$work/cluster.env" render-audit-policy >"$work/audit-policy.yaml"
sudo mkdir -p /etc/rancher/k3s
sudo cp "$work/k3s.yaml" /etc/rancher/k3s/config.yaml && sudo chmod 600 /etc/rancher/k3s/config.yaml
sudo cp "$work/audit-policy.yaml" /etc/rancher/k3s/audit-policy.yaml
curl -sfL https://get.k3s.io | sudo INSTALL_K3S_VERSION="$(grep K3S_VERSION "$work/cluster.env" | cut -d= -f2)" sh -s - >/dev/null
mkdir -p "$work/state"
# shellcheck disable=SC2024  # root reads the file, the runner user owns the copy
sudo cat /etc/rancher/k3s/k3s.yaml >"$work/state/kubeconfig"; chmod 600 "$work/state/kubeconfig"
export KUBECONFIG=$work/state/kubeconfig
for _ in $(seq 60); do kubectl get node ci-1 >/dev/null 2>&1 && break; sleep 5; done
kubectl wait --for=condition=Ready node/ci-1 --timeout=300s
# k3s applies its addons (CoreDNS among them) just after the node is Ready: wait for the deployment to exist first.
for _ in $(seq 60); do kubectl -n kube-system get deployment/coredns >/dev/null 2>&1 && break; sleep 2; done
kubectl -n kube-system rollout status deployment/coredns --timeout=300s
sudo test -s /var/lib/rancher/k3s/server/logs/audit.log || fail "no API audit log"
sudo k3s secrets-encrypt status | grep -q "Encryption Status: Enabled" || fail "secrets are not encrypted at rest"
ip link show flannel-wg >/dev/null || fail "the pod network is not on WireGuard"

step "platform"
KUBECONFIG_OVERRIDE=$KUBECONFIG deploy/hetzner/cluster.sh "$work/cluster.env" platform
kubectl -n ingress-nginx get svc ingress-nginx-controller -o jsonpath='{.spec.ports[*].nodePort}' | grep -q 30443 \
    || fail "the ingress is not on node port 30443"
# The ingress answers the load balancer's PROXY protocol on the node ports (404: no tenant host yet).
code=$(curl -s -o /dev/null -w '%{http_code}' --haproxy-protocol -H 'Host: nobody.example' http://127.0.0.1:30080/ || true)
[ "$code" = 404 ] || fail "ingress through the node port: HTTP $code"
kubectl get clusterissuer letsencrypt letsencrypt-staging >/dev/null
pg_pod=$(kubectl -n cairn-platform get pod -l cnpg.io/cluster=cairn-pg,cnpg.io/instanceRole=primary -o name)
[ -n "$pg_pod" ] || fail "no PostgreSQL primary"
kubectl -n monitoring get prometheus >/dev/null

pressure
step "tenant: tenant.sh create (databases, namespace, services, isolation), release rendered"
python3 deploy/ops/licence.py issue --key deploy/licence/dev-private-key.pem --kid cairn-dev --customer "CI" \
    --tenant ci-acme --days 30 >"$work/ci.licence"
cat >"$work/helm-stub" <<'EOF'
#!/bin/sh
echo "$@" >"$HELM_STUB_LOG"
EOF
chmod +x "$work/helm-stub"
# shellcheck disable=SC1090
source <(grep '^export CAIRN_' deploy/hetzner/cluster.env.example)
export CAIRN_HOST_DOMAIN=cairn-ci.dev CAIRN_TENANT_SEARCH_STORAGE=2Gi CAIRN_TENANT_KAFKA_STORAGE=2Gi
HELM="$work/helm-stub" HELM_STUB_LOG="$work/helm-install.args" \
    deploy/hosting/tenant.sh create ci-acme --licence "$work/ci.licence" --values deploy/hetzner/platform-values.yaml
grep -q "core.search.host=opensearch.cairn-ci-acme.svc" "$work/helm-install.args" || fail "search host not passed"
grep -q "core.kafka.bootstrap=kafka.cairn-ci-acme.svc:9092" "$work/helm-install.args" || fail "Kafka not passed"
grep -q "cluster-issuer=letsencrypt" "$work/helm-install.args" || fail "TLS issuer not passed"
[ "$(kubectl get ns cairn-ci-acme -o jsonpath='{.metadata.labels.pod-security\.kubernetes\.io/enforce}')" = baseline ] \
    || fail "Pod Security not enforced"
# The release's manifests, rendered by the real chart and checked by the API server (schemas, CRDs,
# quota, Pod Security: any warning fails).
deploy/hosting/tenant.sh create ci-acme --licence "$work/ci.licence" --values deploy/hetzner/platform-values.yaml --dry-run \
    >"$work/release.yaml"
# Workloads are only warned about (Pods are refused): warn at the enforced level for this check.
kubectl label namespace cairn-ci-acme pod-security.kubernetes.io/warn=baseline --overwrite >/dev/null
kubectl -n cairn-ci-acme apply --dry-run=server -f "$work/release.yaml" >/dev/null 2>"$work/release.warnings" \
    || { cat "$work/release.warnings"; fail "the release does not apply"; }
kubectl label namespace cairn-ci-acme pod-security.kubernetes.io/warn=restricted --overwrite >/dev/null
if grep -i "violate PodSecurity" "$work/release.warnings"; then fail "the release breaks Pod Security baseline"; fi
psql_admin() { kubectl -n cairn-platform exec -i "$pg_pod" -c postgres -- psql -U postgres -tA "$@"; }
[ "$(psql_admin -d cairn_ci_acme -c "select extname from pg_extension where extname='vector'")" = vector ] \
    || fail "pgvector missing in cairn_ci_acme"
psql_admin -c "select has_database_privilege('public', 'cairn_ci_acme', 'connect')" | grep -q f \
    || fail "every role may connect to the tenant's database"

pressure
step "isolation"
pw=$(kubectl -n cairn-ci-acme get secret cairn-core-secrets -o jsonpath='{.data.db-password}' | base64 -d)
probe() {   # namespace labels name command...: a short-lived pod; its exit status
    local ns=$1 labels=$2 name=$3 phase=""; shift 3
    local label_json; label_json=$(tr ',' '\n' <<<"$labels" | sed 's/^\(.*\)=\(.*\)$/"\1": "\2"/' | paste -sd, -)
    # Retried for a while: the network policy engine admits a new pod's address a moment after it starts,
    # so a first refusal proves nothing either way; blocked must stay blocked throughout.
    local cmd="for i in \$(seq 10); do ( $* ) && exit 0; sleep 2; done; exit 1"
    jq -n --arg name "$name" --arg pw "$pw" --arg cmd "$cmd" --argjson labels "{$label_json}" '{
        apiVersion: "v1", kind: "Pod",
        metadata: { name: $name, labels: $labels },
        spec: { restartPolicy: "Never", containers: [{
            name: "probe", image: "postgres:16-alpine", command: ["sh", "-c", $cmd],
            env: [{ name: "PGPASSWORD", value: $pw }],
            resources: { requests: { cpu: "50m", memory: "64Mi" }, limits: { memory: "128Mi" } },
            securityContext: { allowPrivilegeEscalation: false, runAsNonRoot: true, runAsUser: 70,
                               capabilities: { drop: ["ALL"] }, seccompProfile: { type: "RuntimeDefault" } } }] } }' |
        kubectl -n "$ns" apply -f - >/dev/null
    for _ in $(seq 90); do
        phase=$(kubectl -n "$ns" get pod "$name" -o jsonpath='{.status.phase}' 2>/dev/null || true)
        case "$phase" in Succeeded|Failed) break ;; esac
        sleep 2
    done
    [ "$phase" = Succeeded ] || kubectl -n "$ns" logs "$name" 2>/dev/null | tail -12 >&2 || true
    kubectl -n "$ns" delete pod "$name" --wait=false >/dev/null
    [ "$phase" = Succeeded ]
}
release="app.kubernetes.io/name=cairn,app.kubernetes.io/instance=cairn-ci-acme"
search="wget -q -T 5 -O- http://opensearch.cairn-ci-acme.svc:9200/_cluster/health | grep -q status"
# A TCP connection to Kafka (BusyBox nc: 0 once connected; 1 when refused or dropped).
kafka="nc -w 5 kafka.cairn-ci-acme.svc 9092 </dev/null >/dev/null"
pg="psql 'host=cairn-pg-rw.cairn-platform.svc user=cairn_ci_acme dbname=cairn_ci_acme sslmode=require connect_timeout=5' -c 'select 1'"
diagnose() {
    echo "--- endpoints, policies and pods in cairn-ci-acme" >&2
    kubectl -n cairn-ci-acme get endpoints -o wide >&2 || true
    kubectl -n cairn-ci-acme get networkpolicy -o yaml >&2 || true
    kubectl -n cairn-ci-acme get pods -o wide --show-labels >&2 || true
}
# The release's paths, from one pod: each step says when it got through.
kafka_pod=$(kubectl -n cairn-ci-acme get pod kafka-0 -o jsonpath='{.status.podIP}')
probe cairn-ci-acme "$release" r-paths "$search && echo search-ok && nc -w 5 $kafka_pod 9092 </dev/null >/dev/null && echo kafka-pod-ok && $kafka && echo kafka-service-ok && $pg >/dev/null && echo postgres-ok" \
    || { diagnose; fail "the release cannot reach its OpenSearch, Kafka and database (see which got through above)"; }
kubectl create namespace cairn-other >/dev/null && kubectl label namespace cairn-other cairn.io/tenant=other >/dev/null
! probe cairn-other "app.kubernetes.io/name=cairn,app.kubernetes.io/instance=cairn-other" o-search "$search" \
    || fail "another tenant reaches this tenant's OpenSearch"
! probe cairn-other "app.kubernetes.io/name=cairn,app.kubernetes.io/instance=cairn-other" o-kafka "$kafka" \
    || fail "another tenant reaches this tenant's Kafka"
! probe cairn-ci-acme "spark-role=driver" s-search "$search" || fail "the tenant's Spark pods reach its OpenSearch"
kubectl create namespace not-a-tenant >/dev/null
! probe not-a-tenant "app=x" n-pg "$pg" || fail "a namespace that is no tenant reaches PostgreSQL"

pressure
step "tenant.sh delete --drop-data"
HELM="$work/helm-stub" HELM_STUB_LOG=/dev/null deploy/hosting/tenant.sh delete ci-acme --drop-data
[ -z "$(psql_admin -c "select 1 from pg_database where datname in ('cairn_ci_acme','cairn_catalog_ci_acme')")" ] \
    || fail "the tenant's databases are still there"
echo; echo "Hetzner setup: all checks passed"
