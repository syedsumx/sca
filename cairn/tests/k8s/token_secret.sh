#!/usr/bin/env bash
# Kubernetes path of the service-account token, against a real kube-apiserver (no kubelet needed):
# applies the chart's token objects (Secret, ServiceAccount, Role, RoleBinding, Job, Deployment), then runs
# the bootstrap and the keeper from cairn/platform with that service account's token, as the pods would,
# against the running compose stack's Cairn Core.
#
#   tests/k8s/token_secret.sh            (needs the compose stack up, docker, curl, openssl)
set -euo pipefail
cd "$(dirname "$0")/../.."
K8S_VERSION="${K8S_VERSION:-v1.31.2}"
ETCD_VERSION="${ETCD_VERSION:-v3.5.15}"
VERSION="${CAIRN_VERSION:-0.1.0}"
work="$(mktemp -d)"
trap 'docker rm -f cairn-k8s-api >/dev/null 2>&1 || true; rm -rf "$work"' EXIT
fail() { echo "FAIL: $*" >&2; exit 1; }

# ---- binaries
curl -fsSL -o "$work/kube-apiserver" "https://dl.k8s.io/release/${K8S_VERSION}/bin/linux/amd64/kube-apiserver"
echo "$(curl -fsSL "https://dl.k8s.io/release/${K8S_VERSION}/bin/linux/amd64/kube-apiserver.sha256")  $work/kube-apiserver" | sha256sum -c - >/dev/null
curl -fsSL -o "$work/kubectl" "https://dl.k8s.io/release/${K8S_VERSION}/bin/linux/amd64/kubectl"
echo "$(curl -fsSL "https://dl.k8s.io/release/${K8S_VERSION}/bin/linux/amd64/kubectl.sha256")  $work/kubectl" | sha256sum -c - >/dev/null
curl -fsSL "https://github.com/etcd-io/etcd/releases/download/${ETCD_VERSION}/etcd-${ETCD_VERSION}-linux-amd64.tar.gz" \
  | tar -xz -C "$work" --strip-components 1 "etcd-${ETCD_VERSION}-linux-amd64/etcd"
chmod +x "$work/kube-apiserver" "$work/kubectl" "$work/etcd"

# ---- PKI: CA, serving cert, admin client cert, service-account signing key
pki="$work/pki"; mkdir -p "$pki"
openssl req -x509 -newkey rsa:2048 -nodes -keyout "$pki/ca.key" -out "$pki/ca.crt" -days 1 -subj "/CN=test-ca" 2>/dev/null
openssl req -newkey rsa:2048 -nodes -keyout "$pki/api.key" -out "$pki/api.csr" -subj "/CN=kube-apiserver" 2>/dev/null
printf "subjectAltName=DNS:cairn-k8s-api,IP:127.0.0.1\n" > "$pki/ext.cnf"
openssl x509 -req -in "$pki/api.csr" -CA "$pki/ca.crt" -CAkey "$pki/ca.key" -CAcreateserial -out "$pki/api.crt" -days 1 -extfile "$pki/ext.cnf" 2>/dev/null
openssl req -newkey rsa:2048 -nodes -keyout "$pki/admin.key" -out "$pki/admin.csr" -subj "/O=system:masters/CN=admin" 2>/dev/null
openssl x509 -req -in "$pki/admin.csr" -CA "$pki/ca.crt" -CAkey "$pki/ca.key" -CAcreateserial -out "$pki/admin.crt" -days 1 2>/dev/null
openssl genrsa -out "$pki/sa.key" 2048 2>/dev/null && openssl rsa -in "$pki/sa.key" -pubout -out "$pki/sa.pub" 2>/dev/null
chmod -R a+r "$work"

# ---- etcd + kube-apiserver (RBAC on), on the stack network
docker run -d --name cairn-k8s-api --network cairn_catalog -p 127.0.0.1:16443:6443 -v "$work:/k8s:ro" \
  --entrypoint sh "${PY_IMAGE:-python:3.12-slim}" -c '/k8s/etcd --data-dir /tmp/etcd >/tmp/etcd.log 2>&1 & sleep 3;
  exec /k8s/kube-apiserver --etcd-servers=http://127.0.0.1:2379 --secure-port=6443 --bind-address=0.0.0.0 \
    --tls-cert-file=/k8s/pki/api.crt --tls-private-key-file=/k8s/pki/api.key --client-ca-file=/k8s/pki/ca.crt \
    --service-account-issuer=https://kubernetes.default.svc --service-account-key-file=/k8s/pki/sa.pub \
    --service-account-signing-key-file=/k8s/pki/sa.key --authorization-mode=RBAC --service-cluster-ip-range=10.96.0.0/16' >/dev/null
cat > "$work/kubeconfig" <<EOF
apiVersion: v1
kind: Config
clusters: [{name: t, cluster: {server: "https://127.0.0.1:16443", certificate-authority: "$pki/ca.crt"}}]
users: [{name: admin, user: {client-certificate: "$pki/admin.crt", client-key: "$pki/admin.key"}}]
contexts: [{name: t, context: {cluster: t, user: admin}}]
current-context: t
EOF
K=("$work/kubectl" --kubeconfig "$work/kubeconfig")
for _ in $(seq 1 60); do "${K[@]}" get --raw /readyz >/dev/null 2>&1 && break; sleep 2; done
"${K[@]}" get --raw /readyz >/dev/null || fail "kube-apiserver did not become ready"

# ---- chart objects
docker run --rm -v "$PWD:/w" -w /w "${HELM_IMAGE:-alpine/helm:3.16.4}" template cairn helm/cairn --namespace cairn \
  --show-only templates/core-token.yaml > "$work/core-token.yaml"
"${K[@]}" create namespace cairn >/dev/null
"${K[@]}" -n cairn create secret generic cairn-core-secrets --from-literal=admin-password=unused >/dev/null
"${K[@]}" -n cairn apply -f "$work/core-token.yaml" >/dev/null
as_sa=(--as system:serviceaccount:cairn:cairn-core-token)
[ "$("${K[@]}" -n cairn auth can-i patch secret/cairn-core-token "${as_sa[@]}")" = yes ] || fail "cannot patch own secret"
[ "$("${K[@]}" -n cairn auth can-i get secret/cairn-core-secrets "${as_sa[@]}" || true)" = no ] || fail "can read other secrets"
[ "$("${K[@]}" -n cairn auth can-i list secrets "${as_sa[@]}" || true)" = no ] || fail "can list secrets"

# ---- run bootstrap and keeper the way the pods do (service-account mount, in-cluster env)
sa="$work/sa"; mkdir -p "$sa"
"${K[@]}" -n cairn create token cairn-core-token --duration 1h > "$sa/token"
cp "$pki/ca.crt" "$sa/ca.crt"; printf cairn > "$sa/namespace"; chmod -R a+r "$sa"
pod() {
  docker run --rm --network cairn_catalog -v "$sa:/var/run/secrets/kubernetes.io/serviceaccount:ro" \
    -e KUBERNETES_SERVICE_HOST=cairn-k8s-api -e KUBERNETES_SERVICE_PORT=6443 \
    -e CAIRN_FRONTEND_URL=http://cairn-web:9002 -e CAIRN_CORE_URL=http://cairn-core:8080 -e CAIRN_BOOTSTRAP_RETRIES=5 \
    "$@" --entrypoint python "cairn/platform:${VERSION}" /opt/cairn/bootstrap/mint_core_token.py --k8s-secret cairn-core-token "${POD_ARGS[@]}"
}
secret_token() { "${K[@]}" -n cairn get secret cairn-core-token -o jsonpath='{.data.core_token}' | base64 -d; }
whoami() {
  curl -s localhost:8080/api/graphql -H "Authorization: Bearer $1" -H 'Content-Type: application/json' \
    -d '{"query":"{ me { corpUser { urn } } }"}' | grep -o 'urn:li:corpuser:[a-z-]*' || echo none
}
admin_pw="$(grep -E '^CAIRN_ADMIN_PASSWORD=' .env | cut -d= -f2-)"

POD_ARGS=(); pod -e CAIRN_ADMIN_PASSWORD="$admin_pw"
first="$(secret_token)"
[ "$(whoami "$first")" = urn:li:corpuser:cairn-service ] || fail "bootstrap token is not the service account's"
POD_ARGS=(); pod -e CAIRN_ADMIN_PASSWORD=wrong | grep -q "nothing to do" || fail "re-run was not idempotent"
POD_ARGS=(--keep --once --rotate-days 0 --grace-minutes 0); pod
POD_ARGS=(--keep --once --rotate-days 20 --grace-minutes 0); pod
second="$(secret_token)"
[ "$second" != "$first" ] || fail "keeper did not rotate"
[ "$(whoami "$second")" = urn:li:corpuser:cairn-service ] || fail "rotated token does not work"
[ "$(whoami "$first")" = none ] || fail "retired token was not revoked"
echo "k8s token path: OK (RBAC, bootstrap, idempotent re-run, rotation, revocation)"
