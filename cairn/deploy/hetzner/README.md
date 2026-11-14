# Cairn on Hetzner

A hosting cluster for Cairn tenants on Hetzner dedicated servers. It has three nodes running k3s with a high-availability control plane. The shared services are ingress, TLS, PostgreSQL with backups, and monitoring. Each tenant gets its own namespace, OpenSearch and Kafka, and is isolated from the others.

```
Internet ── Hetzner Load Balancer (TCP 80/443, PROXY protocol) ── node ports 30080/30443
              │
   ┌──────────┼───────────────────────── vSwitch VLAN (private, MTU 1400) ───────────┐
   cairn-1    cairn-2    cairn-3          k3s servers (embedded etcd), WireGuard pod network
   ingress-nginx · cert-manager · CloudNativePG (primary + 2 replicas) · Prometheus/Grafana
   cairn-<tenant> namespaces: the Cairn release, its OpenSearch and Kafka, isolation policies
              │
   Hetzner Object Storage: etcd snapshots (every 6 h), PostgreSQL WAL + nightly base backups
```

## Files

| File | What it is |
|---|---|
| `cluster.env.example` | Every setting. Copy it outside the repository, because it holds secrets. |
| `installimage.conf` | Installs Ubuntu 24.04 with the two NVMe drives mirrored (RAID 1). |
| `robot.sh` | Creates the vSwitch and attaches the servers (Hetzner Robot API). |
| `terraform/` | The load balancer (Hetzner Cloud). |
| `node-prepare.sh` | Prepares one node: VLAN, firewall, kernel settings, SSH, updates. |
| `cluster.sh` | Runs everything from your machine: `prepare`, `k3s`, `kubeconfig`, `buckets`, `platform`, `status`. |
| `platform.sh`, `platform/` | The shared services, with pinned chart versions. |
| `platform-values.yaml` | Cairn release values for tenants on this cluster. |
| `ci/k3s-smoke.sh` | The CI test. It runs all of the above on a real machine (job `hetzner`). |

## What it costs

Prices are as of late 2026, excluding VAT. Check Hetzner's site before ordering.

| | Monthly |
|---|---|
| 3 × AX42 (8 cores, 64 GB, 2 × 512 GB NVMe), or AX102 (16 cores, 128 GB) for more tenants | about €170–230 (AX102: about €370) + one-off setup fees |
| Load balancer LB11 | about €7.50 |
| Object Storage (backups), first TB included | about €6.50 |
| vSwitch | free |

Three AX42 servers take 5–8 small tenants, and three AX102 servers take 15–20. Each tenant asks for about 14 GB of memory: Cairn's release takes about 12 GB, plus 2.3 GB for its OpenSearch and Kafka.

## Before you start

You need:

- **Hetzner accounts:** a Robot account (dedicated servers) and a Cloud project (load balancer, Object Storage).
- **Servers:** three servers in the same network zone. FSN1 and NBG1 are one zone; HEL1 is its own.
- **Domain:** a domain for tenants. Each tenant is served at `https://<tenant>.<domain>`.
- **Tools on your machine:** `ssh`, `kubectl`, `helm` 3, `terraform` ≥ 1.6, `jq`, `openssl`, Python 3 with `cryptography` and `sqlalchemy` (for `tenant.sh`), and the AWS CLI (for `buckets`).

## Setup

**1. Install the servers.** In Robot, boot each server into the rescue system. Copy `installimage.conf` to it and run:
```bash
installimage -a -c installimage.conf -n cairn-1    # cairn-2, cairn-3 on the others
reboot
```

**2. Connect them privately.** Create a Robot web service user (Robot → Settings), then:
```bash
export ROBOT_USER=... ROBOT_PASSWORD=...
deploy/hetzner/robot.sh vswitch-create cairn 4000          # prints the vSwitch id
deploy/hetzner/robot.sh vswitch-attach <id> 203.0.113.11 203.0.113.12 203.0.113.13
deploy/hetzner/robot.sh vswitch-show <id>                  # wait until every server is "ready"
```

**3. Fill in the settings.**
```bash
cp deploy/hetzner/cluster.env.example ~/cairn-hetzner/cluster.env && chmod 600 ~/cairn-hetzner/cluster.env
```
Fill in the nodes, `ADMIN_CIDRS` (your own address) and the Object Storage keys (Cloud Console → Object Storage). Make `BUCKET_PREFIX` your own, because bucket names are global.

**4. The load balancer.**
```bash
cd deploy/hetzner/terraform && cp terraform.tfvars.example terraform.tfvars   # the nodes' IPs
export HCLOUD_TOKEN=...      # Cloud Console → Security → API tokens (read & write)
terraform init && terraform apply
terraform output lb_ipv4
```
Then do two things:
- Set `LB_SOURCE_CIDRS="<lb_ipv4>/32"` in `cluster.env`.
- At your DNS provider, create an A record `*.<domain>` pointing to that address (and AAAA to `lb_ipv6` if you like).

**5. Build the cluster.**
```bash
E=~/cairn-hetzner/cluster.env
deploy/hetzner/cluster.sh $E prepare      # VLAN, firewall, kernel; checks the nodes reach each other
deploy/hetzner/cluster.sh $E buckets      # etcd and PostgreSQL backup buckets, versioned
deploy/hetzner/cluster.sh $E k3s          # one node at a time; writes the kubeconfig
deploy/hetzner/cluster.sh $E platform     # ingress, TLS, PostgreSQL, monitoring (about 10 minutes)
deploy/hetzner/cluster.sh $E status
```
The kubeconfig is in `~/cairn-hetzner/.cluster-cairn/kubeconfig` and gives full admin rights. The k3s join token is there too; keep both safe.

**6. The first tenant.** Run this from the `cairn/` directory:
```bash
source ~/cairn-hetzner/cluster.env             # exports the CAIRN_* settings for tenant.sh
export KUBECONFIG=~/cairn-hetzner/.cluster-cairn/kubeconfig
python deploy/ops/licence.py issue --key <your signing key> --kid <kid> --customer "Acme" --tenant acme \
    --edition standard --max-users 50 --days 365 > acme.licence
deploy/hosting/tenant.sh create acme --licence acme.licence --values deploy/hetzner/platform-values.yaml
```
Let's Encrypt issues `acme`'s certificate within a minute or two. To try things out without hitting Let's Encrypt's rate limits, use `CAIRN_TLS_ISSUER=letsencrypt-staging`.

## What a tenant gets

- **Namespace:** `cairn-<id>`, with a resource quota of 8 CPUs and 24 GB of requested memory. Pod Security is enforced at the `baseline` level.
- **Databases:** `cairn_<id>` and `cairn_catalog_<id>` on the shared PostgreSQL, owned by the role `cairn_<id>`. Other roles can't even connect to them.
- **OpenSearch and Kafka:** its own, in its namespace. Only its own Cairn release reaches them, not its Spark jobs or notebooks.
- **Isolation:** nothing in another tenant's namespace reaches this one. Inside the namespace, Cairn's own components are reached by the release, the ingress and Prometheus. Spark drivers and executors talk to each other. PostgreSQL accepts connections from tenant namespaces only, and each needs its own password.
- **Certificate:** issued and renewed by cert-manager.

## Running it

**Backups:**
- **etcd:** a snapshot every 6 hours, on each node and in `<prefix>-etcd`.
- **PostgreSQL:** WAL archived continuously, a base backup nightly, kept 30 days, in `<prefix>-postgres`.
- **Each tenant's lakehouse:** set up the Cairn Store's continuous backup to `<prefix>-tenants` ("Backups" in `docs/site/administration.md`).

**Restoring PostgreSQL:** delete the broken `cairn-pg` cluster. Create it again from `platform/postgres.yaml`, with `bootstrap.recovery` from the same `barmanObjectStore` in place of `bootstrap.initdb`, optionally with `recoveryTarget.targetTime` for a point in time. With the same name, tenants' connection settings stay valid. See the CloudNativePG docs on "Recovery".

**Restoring etcd:** run `k3s server --cluster-reset --cluster-reset-restore-path=<snapshot>` on one node, then rejoin the others. See the k3s docs on "Backup and restore".

**Rebooting a node** for kernel updates (security updates install themselves; reboots never happen on their own). Do one node at a time:
```bash
kubectl drain cairn-2 --ignore-daemonsets --delete-emptydir-data && ssh root@<node> reboot
kubectl uncordon cairn-2
```

**Adding a node:** install it as in steps 1–2 and add its line to `NODES`. Then run `prepare` and `k3s`; the nodes already in the cluster are left as they are. Finally add its IP to `terraform.tfvars` and run `terraform apply`.

**Upgrading:**
- **k3s:** set `K3S_VERSION` and run `cluster.sh k3s`, one node at a time.
- **Charts:** bump the versions at the top of `platform.sh`, read their upgrade notes, then run `cluster.sh platform`.
  Each chart is downloaded first, with up to five tries (chart repositories and GitHub answer 5xx now and
  then), and installed from the file; a download that keeps failing stops the run with the chart's name, and a
  rollout that fails is never retried.
- **Tenants:** `tenant.sh upgrade <id>`.

**Monitoring:** Grafana's admin password is in the `grafana-admin` secret in the `monitoring` namespace. Open Grafana with:
```bash
kubectl -n monitoring port-forward svc/monitoring-grafana 3000:80
```

## Known limits

- **ingress-nginx:** Kubernetes retired the project (best-effort maintenance ended in March 2026). It stays pinned here because Cairn's chart uses its annotations. Moving the chart to the Gateway API is the follow-up.
- **No automatic failover for a tenant's OpenSearch and Kafka:** each runs on one node's disk, so if that node is down the tenant waits for it. PostgreSQL fails over to a replica on another node; the Cairn Store does too when its cluster mode has replicas. If a node is lost for good, rebuild the tenant's search index from its database.
- **The load balancer's source address:** the firewall admits the ingress node ports only from `LB_SOURCE_CIDRS`. If the load balancer shows its targets as unhealthy, run `tcpdump -ni <interface> port 30443` on a node to see which address it connects from, and add it.
- **No customer-managed encryption keys:** tenants can't bring their own keys, because there is no Vault on this cluster. The store's own encryption applies.
