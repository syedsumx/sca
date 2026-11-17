#!/usr/bin/env bash
# Builds and runs the Cairn hosting cluster on Hetzner dedicated servers, from your own machine.
# Settings: a cluster.env file (copy cluster.env.example; keep it outside git, chmod 600).
#
#   deploy/hetzner/cluster.sh <cluster.env> prepare      # each node: VLAN, firewall, kernel, SSH (node-prepare.sh)
#   deploy/hetzner/cluster.sh <cluster.env> k3s          # k3s on every node: 3 control-plane nodes, embedded etcd
#   deploy/hetzner/cluster.sh <cluster.env> kubeconfig   # writes $STATE_DIR/kubeconfig (admin; keep it safe)
#   deploy/hetzner/cluster.sh <cluster.env> buckets      # the backup buckets in Hetzner Object Storage
#   deploy/hetzner/cluster.sh <cluster.env> platform     # shared services (platform.sh)
#   deploy/hetzner/cluster.sh <cluster.env> status
#   deploy/hetzner/cluster.sh <cluster.env> render-k3s N # node N's /etc/rancher/k3s/config.yaml, to stdout
#   deploy/hetzner/cluster.sh <cluster.env> k3s-installer # downloads k3s's install script, checks it, prints its path
#
# Needs ssh and scp (root on the nodes, by key), and for buckets the AWS CLI; platform needs helm and kubectl.
set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd)
env_file=${1:?usage: cluster.sh <cluster.env> <command>}
cmd=${2:?usage: cluster.sh <cluster.env> <command>}
die() { echo "cluster: $*" >&2; exit 1; }
[ -f "$env_file" ] || die "no settings file $env_file (copy cluster.env.example)"
# shellcheck disable=SC1090
source "$env_file"
STATE_DIR=${STATE_DIR:-$(dirname "$env_file")/.cluster-$CLUSTER_NAME}
K3S_DEFAULT_VERSION=v1.33.3+k3s1
K3S_VERSION=${K3S_VERSION:-$K3S_DEFAULT_VERSION}
# k3s's install script is taken from the release's own tag and must match this checksum: it runs as root on
# every node, and it is what verifies the k3s binary (against the release's sha256sum file). For another
# K3S_VERSION, read https://raw.githubusercontent.com/k3s-io/k3s/<version>/install.sh and set its sha256.
[ "$K3S_VERSION" != "$K3S_DEFAULT_VERSION" ] ||
    K3S_INSTALL_SHA256=${K3S_INSTALL_SHA256:-9ca7930c31179d83bc13de20078fd8ad3e1ee00875b31f39a7e524ca4ef7d9de}
VLAN_ID=${VLAN_ID:-4000}
PRIVATE_PREFIX=${PRIVATE_PREFIX:-24}
SSH_USER=${SSH_USER:-root}
umask 077
mkdir -p "$STATE_DIR"

[ "${#NODES[@]}" -ge 1 ] || die "NODES is empty"
node() { read -r n_name n_public n_private <<<"${NODES[$1]}"; }
ssh_opts=(-o StrictHostKeyChecking=accept-new -o ConnectTimeout=15)
[ -z "${SSH_KEY:-}" ] || ssh_opts+=(-i "$SSH_KEY")
on() { local ip=$1; shift; ssh "${ssh_opts[@]}" "$SSH_USER@$ip" "$@"; }
put() { scp -q "${ssh_opts[@]}" "$1" "$SSH_USER@$2:$3"; }

token() {
    [ -s "$STATE_DIR/k3s-token" ] || openssl rand -hex 32 >"$STATE_DIR/k3s-token"
    cat "$STATE_DIR/k3s-token"
}

render_k3s() {   # node index -> k3s config
    local i=$1 first_private
    node 0; first_private=$n_private
    node "$i"
    cat <<EOF
# Cairn on Hetzner: k3s on $n_name (deploy/hetzner/cluster.sh). Contains secrets: mode 0600.
token: "$(token)"
EOF
    if [ "$i" = 0 ]; then echo "cluster-init: true"; else echo "server: https://$first_private:6443"; fi
    cat <<EOF
node-name: $n_name
node-ip: $n_private
node-external-ip: $n_public
advertise-address: $n_private
flannel-iface: vlan$VLAN_ID
# Pod traffic between nodes is encrypted (WireGuard): the vSwitch is private, not encrypted.
flannel-backend: wireguard-native
tls-san:
  - $n_public
  - $n_private
EOF
    [ -z "${API_HOST:-}" ] || echo "  - $API_HOST"
    cat <<EOF
disable:
  - traefik      # ingress-nginx (platform.sh) serves Cairn's ingresses
  - servicelb    # the Hetzner load balancer reaches the ingress on its node ports
secrets-encryption: true
protect-kernel-defaults: true
write-kubeconfig-mode: "0600"
kube-apiserver-arg:
  - audit-log-path=/var/lib/rancher/k3s/server/logs/audit.log
  - audit-policy-file=/etc/rancher/k3s/audit-policy.yaml
  - audit-log-maxage=30
  - audit-log-maxbackup=10
  - audit-log-maxsize=100
kubelet-arg:
  - max-pods=150
  - system-reserved=cpu=500m,memory=2Gi
  - kube-reserved=cpu=500m,memory=1Gi
  - eviction-hard=memory.available<1Gi,nodefs.available<10%
etcd-snapshot-schedule-cron: "0 */6 * * *"
etcd-snapshot-retention: 28
EOF
    if [ -n "${OBJECT_STORAGE_ACCESS_KEY:-}" ]; then
        cat <<EOF
etcd-s3: true
etcd-s3-endpoint: ${OBJECT_STORAGE_ENDPOINT#https://}
etcd-s3-region: ${OBJECT_STORAGE_REGION}
etcd-s3-bucket: ${BUCKET_PREFIX}-etcd
etcd-s3-folder: $n_name
etcd-s3-access-key: "${OBJECT_STORAGE_ACCESS_KEY}"
etcd-s3-secret-key: "${OBJECT_STORAGE_SECRET_KEY}"
EOF
    fi
}

audit_policy() {
    cat <<'EOF'
# Who did what through the Kubernetes API; secrets' contents are never logged.
apiVersion: audit.k8s.io/v1
kind: Policy
omitStages: [RequestReceived]
rules:
  - level: None
    users: ["system:kube-proxy"]
  - level: None
    resources: [{ group: "", resources: ["events"] }, { group: "coordination.k8s.io", resources: ["leases"] }]
  - level: Metadata
    resources: [{ group: "", resources: ["secrets", "configmaps"] }, { group: "authentication.k8s.io", resources: ["tokenreviews"] }]
  - level: None
    verbs: ["get", "list", "watch"]
  - level: Request
EOF
}

prepare() {
    : "${ADMIN_CIDRS:?set ADMIN_CIDRS}"
    for i in "${!NODES[@]}"; do
        node "$i"
        echo "== $n_name ($n_public): prepare" >&2
        put "$here/node-prepare.sh" "$n_public" /root/cairn-node-prepare.sh
        on "$n_public" "VLAN_ID=$VLAN_ID PRIVATE_IP=$n_private PRIVATE_PREFIX=$PRIVATE_PREFIX \
            ADMIN_CIDRS='$ADMIN_CIDRS' LB_SOURCE_CIDRS='${LB_SOURCE_CIDRS:-}' bash /root/cairn-node-prepare.sh"
    done
    node 0; local first=$n_private
    for i in "${!NODES[@]}"; do   # every node reaches the others over the vSwitch
        node "$i"
        on "$n_public" "ping -c 2 -W 2 $first >/dev/null" || die "$n_name cannot reach $first on the vSwitch (VLAN $VLAN_ID)"
    done
    echo "all nodes prepared; they reach each other on VLAN $VLAN_ID" >&2
}

k3s_installer() {   # -> the path of k3s's install script for K3S_VERSION, checked against K3S_INSTALL_SHA256
    local f="$STATE_DIR/k3s-install-$K3S_VERSION.sh" got
    [ -n "${K3S_INSTALL_SHA256:-}" ] ||
        die "K3S_VERSION $K3S_VERSION: set K3S_INSTALL_SHA256, the sha256 of https://raw.githubusercontent.com/k3s-io/k3s/${K3S_VERSION//+/%2B}/install.sh"
    [[ "$K3S_INSTALL_SHA256" =~ ^[0-9a-f]{64}$ ]] || die "K3S_INSTALL_SHA256: 64 hex digits"
    if [ ! -s "$f" ] || [ "$(sha256sum <"$f" | cut -d' ' -f1)" != "$K3S_INSTALL_SHA256" ]; then
        curl -sSfL --retry 5 --retry-all-errors --retry-delay 10 -o "$f.part" \
            "https://raw.githubusercontent.com/k3s-io/k3s/${K3S_VERSION//+/%2B}/install.sh" ||
            die "could not download k3s's install script for $K3S_VERSION"
        got=$(sha256sum <"$f.part" | cut -d' ' -f1)
        [ "$got" = "$K3S_INSTALL_SHA256" ] ||
            { rm -f "$f.part"; die "k3s's install script for $K3S_VERSION has sha256 $got, not K3S_INSTALL_SHA256 $K3S_INSTALL_SHA256"; }
        mv "$f.part" "$f"
    fi
    echo "$f"
}

install_k3s() {
    local installer; installer=$(k3s_installer)
    for i in "${!NODES[@]}"; do
        node "$i"
        echo "== $n_name: k3s $K3S_VERSION" >&2
        render_k3s "$i" >"$STATE_DIR/k3s-$n_name.yaml"
        audit_policy >"$STATE_DIR/audit-policy.yaml"
        local want; want=$(cat "$STATE_DIR/k3s-$n_name.yaml" "$STATE_DIR/audit-policy.yaml" | sha256sum | cut -d' ' -f1)
        if on "$n_public" "systemctl is-active -q k3s && k3s --version | grep -qF '$K3S_VERSION' && \
                [ \"\$(cat /etc/rancher/k3s/config.yaml /etc/rancher/k3s/audit-policy.yaml | sha256sum | cut -d' ' -f1)\" = $want ]"; then
            echo "   already at $K3S_VERSION with this configuration: left as it is" >&2
            continue
        fi
        on "$n_public" "mkdir -p /etc/rancher/k3s && chmod 700 /etc/rancher/k3s"
        put "$STATE_DIR/k3s-$n_name.yaml" "$n_public" /etc/rancher/k3s/config.yaml
        put "$STATE_DIR/audit-policy.yaml" "$n_public" /etc/rancher/k3s/audit-policy.yaml
        put "$installer" "$n_public" /root/cairn-k3s-install.sh
        on "$n_public" "chmod 600 /etc/rancher/k3s/config.yaml && [ \"\$(sha256sum </root/cairn-k3s-install.sh | cut -d' ' -f1)\" = $K3S_INSTALL_SHA256 ] && \
            INSTALL_K3S_VERSION='$K3S_VERSION' INSTALL_K3S_EXEC=server sh /root/cairn-k3s-install.sh >/dev/null"
        on "$n_public" "for t in \$(seq 60); do k3s kubectl get node $n_name >/dev/null 2>&1 && break; sleep 5; done; \
            k3s kubectl wait --for=condition=Ready node/$n_name --timeout=300s"
    done
    rm -f "$STATE_DIR"/k3s-*.yaml
    kubeconfig
}

kubeconfig() {
    node 0
    local server=${API_HOST:-$n_public}
    on "$n_public" cat /etc/rancher/k3s/k3s.yaml | sed "s#https://127.0.0.1:6443#https://$server:6443#; s#: default\$#: $CLUSTER_NAME#" \
        >"$STATE_DIR/kubeconfig"
    chmod 600 "$STATE_DIR/kubeconfig"
    echo "kubeconfig: $STATE_DIR/kubeconfig (cluster admin: keep it safe)" >&2
}

buckets() {
    : "${OBJECT_STORAGE_ACCESS_KEY:?set OBJECT_STORAGE_ACCESS_KEY}" "${BUCKET_PREFIX:?set BUCKET_PREFIX}"
    export AWS_ACCESS_KEY_ID=$OBJECT_STORAGE_ACCESS_KEY AWS_SECRET_ACCESS_KEY=$OBJECT_STORAGE_SECRET_KEY
    export AWS_DEFAULT_REGION=$OBJECT_STORAGE_REGION
    local b
    for b in etcd postgres tenants; do
        if aws --endpoint-url "$OBJECT_STORAGE_ENDPOINT" s3api head-bucket --bucket "$BUCKET_PREFIX-$b" 2>/dev/null; then
            echo "bucket $BUCKET_PREFIX-$b: exists" >&2
        else
            aws --endpoint-url "$OBJECT_STORAGE_ENDPOINT" s3api create-bucket --bucket "$BUCKET_PREFIX-$b" >/dev/null
            echo "bucket $BUCKET_PREFIX-$b: created" >&2
        fi
        # Versioning: a deleted or overwritten backup can still be recovered.
        aws --endpoint-url "$OBJECT_STORAGE_ENDPOINT" s3api put-bucket-versioning --bucket "$BUCKET_PREFIX-$b" \
            --versioning-configuration Status=Enabled
    done
}

status() {
    export KUBECONFIG=$STATE_DIR/kubeconfig
    kubectl get nodes -o wide
    kubectl -n cairn-platform get cluster.postgresql.cnpg.io 2>/dev/null || true
    kubectl get pods -A --field-selector=status.phase!=Running,status.phase!=Succeeded 2>/dev/null || true
}

case "$cmd" in
    prepare) prepare ;;
    k3s) install_k3s ;;
    kubeconfig) kubeconfig ;;
    buckets) buckets ;;
    platform) KUBECONFIG=${KUBECONFIG_OVERRIDE:-$STATE_DIR/kubeconfig} "$here/platform.sh" "$env_file" ;;
    status) status ;;
    render-k3s) render_k3s "${3:?node index}" ;;
    k3s-installer) k3s_installer ;;
    render-audit-policy) audit_policy ;;
    *) sed -n '2,14p' "$0"; exit 64 ;;
esac
