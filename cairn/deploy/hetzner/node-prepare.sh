#!/usr/bin/env bash
# Prepares one Hetzner dedicated server (Ubuntu 24.04, installed with installimage.conf) to be a node of
# the Cairn cluster. Run as root on the node; cluster.sh copies it there and runs it with the settings below.
#
#   VLAN_ID=4000 PRIVATE_IP=10.10.0.11 PRIVATE_PREFIX=24 ADMIN_CIDRS="198.51.100.7/32" \
#   LB_SOURCE_CIDRS="203.0.113.50/32" ./node-prepare.sh            # applies
#   ... ./node-prepare.sh --render DIR                               # only writes the files to DIR
#
# What it sets up:
#   - the vSwitch VLAN interface (MTU 1400) carrying all traffic between nodes (Kubernetes, etcd and the
#     WireGuard tunnel of the pod network)
#   - a host firewall (nftables, its own table, so Kubernetes' rules are never touched): SSH and the
#     Kubernetes API only from ADMIN_CIDRS; the ingress node ports only from the load balancer
#     (LB_SOURCE_CIDRS); everything between nodes on the VLAN; nothing else from the internet
#   - the kernel settings k3s (protect-kernel-defaults) and OpenSearch need; no swap
#   - SSH keys only; automatic security updates, without automatic reboots (reboot nodes one at a time
#     after draining them: see README.md)
set -euo pipefail

render=""
if [ "${1:-}" = "--render" ]; then render=${2:?--render DIR}; mkdir -p "$render"; fi
die() { echo "node-prepare: $*" >&2; exit 1; }

: "${VLAN_ID:?set VLAN_ID (the vSwitch VLAN, 4000-4091)}"
: "${PRIVATE_IP:?set PRIVATE_IP (this node on the vSwitch, e.g. 10.10.0.11)}"
: "${ADMIN_CIDRS:?set ADMIN_CIDRS (where SSH and kubectl come from)}"
PRIVATE_PREFIX=${PRIVATE_PREFIX:-24}
LB_SOURCE_CIDRS=${LB_SOURCE_CIDRS:-}
NODE_PORTS=${NODE_PORTS:-30080,30443}
[[ "$VLAN_ID" =~ ^[0-9]+$ ]] && [ "$VLAN_ID" -ge 4000 ] && [ "$VLAN_ID" -le 4091 ] || die "VLAN_ID must be 4000-4091"
[[ "$PRIVATE_IP" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "PRIVATE_IP: an IPv4 address"
cidrs() { sed 's/[ ,]\+/\n/g' <<<"$1" | sed '/^$/d' | paste -sd, - | sed 's/,/, /g'; }
admin=$(cidrs "$ADMIN_CIDRS")
lb=$(cidrs "$LB_SOURCE_CIDRS")
for c in ${admin//,/ } ${lb//,/ }; do
    [[ "$c" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+(/[0-9]+)?$ ]] || die "not an IPv4 address or CIDR: $c"
done
[ "$admin" != "0.0.0.0/0" ] || die "ADMIN_CIDRS 0.0.0.0/0 would open SSH and the Kubernetes API to everyone"

if [ -n "$render" ]; then
    PUBLIC_IF=${PUBLIC_IF:-eth0}
else
    [ "$(id -u)" = 0 ] || die "run as root"
    PUBLIC_IF=${PUBLIC_IF:-$(ip -o route show default | awk '{print $5; exit}')}
    [ -n "$PUBLIC_IF" ] || die "no default route: set PUBLIC_IF"
fi
VLAN_IF="vlan$VLAN_ID"
out() { if [ -n "$render" ]; then echo "$render/$(basename "$1")"; else mkdir -p "$(dirname "$1")"; echo "$1"; fi; }

# ---- the vSwitch VLAN ---------------------------------------------------------------------------------
cat >"$(out /etc/netplan/60-cairn-vswitch.yaml)" <<EOF
# Cairn: the Hetzner vSwitch (VLAN $VLAN_ID): traffic between the cluster's nodes.
network:
  version: 2
  vlans:
    $VLAN_IF:
      id: $VLAN_ID
      link: $PUBLIC_IF
      mtu: 1400
      addresses: [$PRIVATE_IP/$PRIVATE_PREFIX]
EOF

# ---- host firewall ------------------------------------------------------------------------------------
# Its own table, replaced as a whole: never "flush ruleset", which would drop Kubernetes' own rules.
lb_rule="        # No load balancer yet (LB_SOURCE_CIDRS): the ingress node ports are closed."
[ -z "$lb" ] || lb_rule="        iifname \"$PUBLIC_IF\" ip saddr { $lb } tcp dport { $NODE_PORTS } accept"
cat >"$(out /etc/nftables.conf)" <<EOF
#!/usr/sbin/nft -f
# Cairn host firewall (deploy/hetzner/node-prepare.sh). Kubernetes keeps its rules in its own tables.
table inet cairn_host
delete table inet cairn_host
table inet cairn_host {
    chain prerouting {
        # Before Kubernetes rewrites node-port traffic (dstnat, priority -100): from the internet, only
        # the load balancer reaches the ingress node ports, and no other node port at all.
        type filter hook prerouting priority -150; policy accept;
$lb_rule
        iifname "$PUBLIC_IF" tcp dport 30000-32767 drop
        iifname "$PUBLIC_IF" udp dport 30000-32767 drop
    }
    chain input {
        type filter hook input priority 0; policy drop;
        ct state established,related accept
        ct state invalid drop
        iif lo accept
        iifname "$VLAN_IF" accept
        iifname { "cni0", "flannel-wg", "flannel-wg-v6", "flannel.1" } accept
        ip protocol icmp icmp type { echo-request, destination-unreachable, time-exceeded } limit rate 20/second accept
        ip6 nexthdr icmpv6 accept
        ip saddr { $admin } tcp dport { 22, 6443 } accept
    }
}
EOF

# ---- kernel, SSH, updates ------------------------------------------------------------------------------
cat >"$(out /etc/sysctl.d/90-cairn-kubernetes.conf)" <<'EOF'
# Cairn: what k3s with protect-kernel-defaults expects, and what OpenSearch needs.
vm.overcommit_memory = 1
vm.panic_on_oom = 0
kernel.panic = 10
kernel.panic_on_oops = 1
kernel.keys.root_maxkeys = 1000000
kernel.keys.root_maxbytes = 25000000
vm.max_map_count = 262144
fs.inotify.max_user_instances = 1024
fs.inotify.max_user_watches = 1048576
net.ipv4.ip_forward = 1
net.ipv4.conf.all.rp_filter = 2
EOF
cat >"$(out /etc/ssh/sshd_config.d/90-cairn-ssh.conf)" <<'EOF'
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin prohibit-password
EOF
cat >"$(out /etc/apt/apt.conf.d/52cairn-upgrades)" <<'EOF'
// Cairn: security updates every day; reboots are done by hand, one drained node at a time.
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
Unattended-Upgrade::Automatic-Reboot "false";
EOF

if [ -n "$render" ]; then echo "rendered to $render" >&2; exit 0; fi

export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q nftables wireguard-tools unattended-upgrades curl jq >/dev/null
chmod 600 /etc/netplan/60-cairn-vswitch.yaml
netplan apply
nft -c -f /etc/nftables.conf
systemctl enable --now nftables >/dev/null
nft -f /etc/nftables.conf
sysctl --system >/dev/null
swapoff -a
sed -i '/\sswap\s/s/^/#/' /etc/fstab
sshd -t && systemctl reload ssh
echo "node $PRIVATE_IP on $VLAN_IF ($PUBLIC_IF): ready" >&2
