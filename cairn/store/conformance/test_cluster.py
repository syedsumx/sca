"""Clusters: several nodes on one PostgreSQL metadata database, each blob kept as copies (or erasure-coded shards)
on distinct nodes. Writes on any node are read on every node; reads fail over when a node dies; repair re-creates
lost copies; too few nodes fail writes closed; nodes are drained, rebalanced onto and removed. Needs
CAIRN_STORE_TEST_META (an admin URL of a PostgreSQL server)."""

import base64
import hashlib
import hmac
import json
import os
import subprocess
import time

import pytest
import requests
from botocore.exceptions import ClientError

from conftest import ADMIN, READER, Store, _free_port
from test_keys import admin
from test_meta_postgres import ADMIN_URL, new_database

pytestmark = pytest.mark.skipif(not ADMIN_URL, reason="needs CAIRN_STORE_TEST_META (PostgreSQL)")

SECRET = "cluster-secret-0123456789"
MASTER = base64.b64encode(b"c" * 32).decode()
FAST = {"CAIRN_STORE_CLUSTER_HEARTBEAT": "0.3", "CAIRN_STORE_CLUSTER_DEAD_AFTER": "1.5",
        "CAIRN_STORE_CLUSTER_REPAIR_INTERVAL": "1"}


class Cluster:
    def __init__(self, binary, tmp_path, meta, **env):
        self.binary, self.tmp, self.meta, self.env = binary, tmp_path, meta, env
        self.nodes: dict[str, Store] = {}

    def add(self, name, start=True, **env):
        port = _free_port()
        st = Store(self.binary, str(self.tmp / name), port, env={
            "CAIRN_STORE_META": self.meta, "CAIRN_STORE_CLUSTER_NODE": name,
            "CAIRN_STORE_CLUSTER_URL": f"http://127.0.0.1:{port}", "CAIRN_STORE_CLUSTER_SECRET": SECRET,
            "CAIRN_STORE_MASTER_KEY": MASTER, **FAST, **self.env, **env})
        self.nodes[name] = st
        return st.start() if start else st

    def __getitem__(self, name) -> Store:
        return self.nodes[name]

    def stop(self):
        for st in self.nodes.values():
            if st.proc and st.proc.poll() is None:
                st.stop()


@pytest.fixture
def make_cluster(binary, tmp_path):
    made = []

    def make(n, **env):
        c = Cluster(binary, tmp_path, new_database(), **env)
        for i in range(n):
            c.add(f"n{i}")
        made.append(c)
        return c

    yield make
    for c in made:
        c.stop()


def kill(st: Store):
    st.proc.kill()
    st.proc.wait()


def blob_ids(meta, bucket):
    """Each current object's blob ids, from the shared index."""
    import psycopg

    with psycopg.connect(meta) as c:
        rows = c.execute("SELECT v FROM cairn_store_kv WHERE tbl = 'objects'").fetchall()
    out = {}
    for (v,) in rows:
        o = json.loads(bytes(v))
        out[o["key"]] = [s["b"] for s in o["segments"]]
    return out


def holders(blob):
    return [h.split("@")[0] for h in blob.split(";")[1:]]


def wait_for(cond, timeout=30, what="condition"):
    end = time.time() + timeout
    while time.time() < end:
        v = cond()
        if v:
            return v
        time.sleep(0.3)
    raise AssertionError(f"timed out waiting for {what}")


def status(st):
    code, s = admin(st, "GET", "cluster")
    assert code == 200, s
    return s


def test_writes_on_any_node_are_read_on_every_node(make_cluster):
    c = make_cluster(3)
    s0, s1, s2 = (c[n].client() for n in ("n0", "n1", "n2"))
    s0.create_bucket(Bucket="shared")
    bodies = {f"k{i}": os.urandom(1000 + i * 997) for i in range(12)}
    for i, (k, b) in enumerate(bodies.items()):
        [s0, s1, s2][i % 3].put_object(Bucket="shared", Key=k, Body=b)
    for s in (s0, s1, s2):
        for k, b in bodies.items():
            assert s.get_object(Bucket="shared", Key=k)["Body"].read() == b
        assert [o["Key"] for o in s.list_objects_v2(Bucket="shared")["Contents"]] == sorted(bodies)
    # Each blob: two copies on distinct nodes, the writing node's own disk first.
    ids = blob_ids(c.meta, "shared")
    for blob, in ids.values():
        assert blob.startswith("r:") and len(set(holders(blob))) == 2, blob
    # Multipart (parts written on one node, completed on another) and server-side copy.
    big = os.urandom(11 << 20)
    up = s1.create_multipart_upload(Bucket="shared", Key="big")["UploadId"]
    parts = [{"PartNumber": n + 1, "ETag": s1.upload_part(Bucket="shared", Key="big", UploadId=up, PartNumber=n + 1,
                                                          Body=big[n * (6 << 20):(n + 1) * (6 << 20)])["ETag"]} for n in range(2)]
    s2.complete_multipart_upload(Bucket="shared", Key="big", UploadId=up, MultipartUpload={"Parts": parts})
    assert s0.get_object(Bucket="shared", Key="big")["Body"].read() == big
    assert s2.get_object(Bucket="shared", Key="big", Range="bytes=6291400-6291500")["Body"].read() == big[6291400:6291501]
    s2.copy_object(Bucket="shared", Key="copied", CopySource={"Bucket": "shared", "Key": "k3"})
    assert s0.get_object(Bucket="shared", Key="copied")["Body"].read() == bodies["k3"]
    # Encryption at rest works across nodes (the same keyring on each).
    s0.put_object(Bucket="shared", Key="secret", Body=b"classified", ServerSideEncryption="AES256")
    assert s2.get_object(Bucket="shared", Key="secret")["Body"].read() == b"classified"
    # Keys created on one node work on the others once they reload (a heartbeat).
    _, k = admin(c["n0"], "POST", "keys", {"buckets": ["shared"]})
    wait_for(lambda: _works(c["n2"], k), what="key on another node")
    # Deletes remove every copy (collected in the background by the lease holder).
    s1.delete_object(Bucket="shared", Key="k0")
    gone = ids["k0"][0]
    wait_for(lambda: _copies_gone(c, gone), what="copies deleted")
    st = status(c["n1"])
    assert sorted(n["name"] for n in st["nodes"]) == ["n0", "n1", "n2"] and all(n["alive"] for n in st["nodes"])
    assert st["blobs"]["degraded"] == 0 and st["blobs"]["lost"] == 0 and st["copies"] == 2
    m = requests.get(c["n0"].endpoint + "/-/metrics", timeout=5).text
    assert 'cairn_store_cluster_nodes{state="alive"} 3' in m


def _works(st, key):
    try:
        st.client((key["access_key"], key["secret_key"])).list_objects_v2(Bucket="shared")
        return True
    except ClientError:
        return False


def _copies_gone(c, blob):
    for part in blob.split(";")[1:]:
        node, lid = part.split("@")
        if os.path.exists(os.path.join(c[node].data, "blobs", lid[:2], lid[2:4], lid)):
            return False
    return True


def test_a_dead_node_fails_over_and_is_repaired(make_cluster):
    c = make_cluster(3)
    s0 = c["n0"].client()
    s0.create_bucket(Bucket="ha-test")
    bodies = {f"o{i}": os.urandom(5000 + i) for i in range(20)}
    for k, b in bodies.items():
        c[f"n{len(k) % 3}"].client().put_object(Bucket="ha-test", Key=k, Body=b)
    kill(c["n1"])
    # Reads on the survivors fail over to the other copy at once.
    s2 = c["n2"].client()
    for k, b in bodies.items():
        assert s2.get_object(Bucket="ha-test", Key=k)["Body"].read() == b
    # Writes go on (two nodes left, two copies).
    s0.put_object(Bucket="ha-test", Key="during", Body=b"while n1 is down")
    # Once n1 counts as dead, repair puts a second copy of everything n1 held on the remaining nodes.
    def healed():
        ids = blob_ids(c.meta, "ha-test")
        return all("n1" not in holders(b[0]) and len(set(holders(b[0]))) == 2 for b in ids.values())

    wait_for(healed, timeout=40, what="repair")
    st = status(c["n0"])
    assert st["blobs"]["degraded"] == 0 and st["blobs"]["lost"] == 0
    assert {n["name"]: n["alive"] for n in st["nodes"]}["n1"] is False
    kill(c["n0"])  # now only n2 has... every blob still has its copy on n2 (n0 and n2 hold all)
    for k, b in bodies.items():
        assert s2.get_object(Bucket="ha-test", Key=k)["Body"].read() == b
    # n1 comes back: it rejoins and serves again.
    c["n1"].start()
    assert c["n1"].client().get_object(Bucket="ha-test", Key="during")["Body"].read() == b"while n1 is down"


def test_too_few_nodes_fail_writes_closed(make_cluster):
    c = make_cluster(2)
    s0 = c["n0"].client()
    s0.create_bucket(Bucket="closed")
    s0.put_object(Bucket="closed", Key="ok", Body=b"two copies")
    kill(c["n1"])
    with pytest.raises(ClientError) as ei:
        s0.put_object(Bucket="closed", Key="no", Body=b"only one node")
    assert ei.value.response["Error"]["Code"] == "ServiceUnavailable"
    assert ei.value.response["ResponseMetadata"]["HTTPStatusCode"] == 503
    with pytest.raises(ClientError):
        s0.head_object(Bucket="closed", Key="no")  # nothing half-written became visible
    assert s0.get_object(Bucket="closed", Key="ok")["Body"].read() == b"two copies"


def test_erasure_coding_reconstructs_and_repairs(make_cluster):
    c = make_cluster(4, CAIRN_STORE_CLUSTER_EC="2+1", CAIRN_STORE_CLUSTER_EC_ABOVE="100000")
    s0 = c["n0"].client()
    s0.create_bucket(Bucket="ec-test")
    big = os.urandom(3 * (1 << 20) + 12345)  # three stripes of 2 x 1 MiB units, the last one partial
    small = os.urandom(5000)
    s0.put_object(Bucket="ec-test", Key="big", Body=big)
    s0.put_object(Bucket="ec-test", Key="small", Body=small)
    ids = blob_ids(c.meta, "ec-test")
    blob = ids["big"][0]
    assert blob.startswith("e:2+1:") and len(set(holders(blob))) == 3, blob
    assert ids["small"][0].startswith("r:")  # below EC_ABOVE: copies
    # Kill a node holding a data shard (not the one we read from): reads reconstruct from parity.
    data_holder = next(h for h in holders(blob)[:2] if h != "n3") if "n3" in holders(blob) else holders(blob)[0]
    reader = next(n for n in ("n0", "n1", "n2", "n3") if n != data_holder)
    kill(c[data_holder])
    s = c[reader].client()
    assert hashlib.sha256(s.get_object(Bucket="ec-test", Key="big")["Body"].read()).digest() == hashlib.sha256(big).digest()
    for a, b in [(0, 99), (1048570, 1048600), (2097150, 3145740), (len(big) - 10, len(big) - 1)]:
        assert s.get_object(Bucket="ec-test", Key="big", Range=f"bytes={a}-{b}")["Body"].read() == big[a:b + 1], (a, b)
    m = requests.get(c[reader].endpoint + "/-/metrics", timeout=5).text
    assert "cairn_store_cluster_reconstructions_total 0" not in m
    # Repair re-creates the lost shard on the free node.
    wait_for(lambda: data_holder not in holders(blob_ids(c.meta, "ec-test")["big"][0]), timeout=40, what="shard repair")
    new = blob_ids(c.meta, "ec-test")["big"][0]
    assert len(set(holders(new))) == 3
    assert s.get_object(Bucket="ec-test", Key="big")["Body"].read() == big


def test_drain_rebalance_and_remove(make_cluster):
    c = make_cluster(2, CAIRN_STORE_CLUSTER_REPAIR_INTERVAL="3600")
    s0 = c["n0"].client()
    s0.create_bucket(Bucket="bal")
    for i in range(30):
        s0.put_object(Bucket="bal", Key=f"f{i}", Body=os.urandom(4000))
    # A new node starts empty; rebalancing moves copies onto it.
    c.add("n2")
    wait_for(lambda: len([n for n in status(c["n0"])["nodes"] if n["alive"]]) == 3, what="n2 joined")
    code, r = admin(c["n0"], "POST", "cluster/rebalance", {})
    assert code == 200 and r["moved"] > 0, r
    per = {n["name"]: n["copies"] for n in status(c["n0"])["nodes"]}
    assert per["n2"] >= 15 and abs(per["n0"] - per["n1"]) <= 2, per
    for i in range(30):
        assert c["n2"].client().head_object(Bucket="bal", Key=f"f{i}")
    # Draining a node moves its copies off; nothing new goes there.
    env = {**os.environ, "CAIRN_STORE_ENDPOINT": c["n1"].endpoint, "CAIRN_STORE_ACCESS_KEY": ADMIN[0], "CAIRN_STORE_SECRET_KEY": ADMIN[1]}

    def cli(*args):
        p = subprocess.run([c["n1"].binary, "admin", *args], env=env, capture_output=True, text=True, timeout=60)
        return p.returncode, json.loads(p.stdout) if p.stdout.strip() else None

    assert cli("cluster-drain", "n0")[1]["state"] == "draining"
    c["n1"].client().put_object(Bucket="bal", Key="after-drain", Body=b"x" * 3000)
    assert "n0" not in holders(blob_ids(c.meta, "bal")["after-drain"][0])
    rc, rep = cli("cluster-repair")
    assert rc == 0 and rep["repaired"] > 0 and rep["lost"] == 0, rep
    assert {n["name"]: n["copies"] for n in cli("cluster")[1]["nodes"]}["n0"] == 0
    assert all("n0" not in holders(b[0]) for b in blob_ids(c.meta, "bal").values())
    # Removing it: the node stops, and refuses to rejoin until it is activated again.
    assert cli("cluster-remove", "n0")[1]["state"] == "removed"
    c["n0"].proc.wait(timeout=10)
    with pytest.raises(RuntimeError):
        c["n0"].start()
    assert cli("cluster-activate", "n0")[1]["state"] == "active"
    c["n0"].start()
    assert c["n0"].client().get_object(Bucket="bal", Key="f3")["Body"].read()
    assert cli("cluster-drain", "nope")[0] != 0
    assert admin(c["n0"], "GET", "cluster", key=READER)[0] == 403
    actions = [e["action"] for e in admin(c["n0"], "GET", "audit?after=0&limit=10000")[1]["entries"] if e["action"].startswith("cluster.")]
    assert actions == ["cluster.rebalance", "cluster.node.drain", "cluster.repair", "cluster.node.remove", "cluster.node.activate"]


def test_internal_api_needs_the_cluster_secret(make_cluster):
    c = make_cluster(2)
    ep = c["n0"].endpoint
    assert requests.get(f"{ep}/-/cluster/v1/ping", timeout=5).status_code == 403
    ts = int(time.time())

    def sig(secret, method="GET", path="/-/cluster/v1/ping", t=ts):
        return hmac.new(secret.encode(), f"{method}\n{path}\n{t}\nn1".encode(), "sha256").hexdigest()

    ok = requests.get(f"{ep}/-/cluster/v1/ping", headers={"x-cairn-cluster": f"n1 {ts} {sig(SECRET)}"}, timeout=5)
    assert ok.status_code == 200 and ok.json()["node"] == "n0"
    bad = requests.get(f"{ep}/-/cluster/v1/ping", headers={"x-cairn-cluster": f"n1 {ts} {sig('wrong-secret-0123456')}"}, timeout=5)
    assert bad.status_code == 403
    old = ts - 3600
    stale = requests.get(f"{ep}/-/cluster/v1/ping", headers={"x-cairn-cluster": f"n1 {old} {sig(SECRET, t=old)}"}, timeout=5)
    assert stale.status_code == 403
    # A signature is bound to its method and path.
    other = requests.delete(f"{ep}/-/cluster/v1/blobs/00ff", headers={"x-cairn-cluster": f"n1 {ts} {sig(SECRET)}"}, timeout=5)
    assert other.status_code == 403
    # S3 credentials do not open the internal API, and the S3 API is unchanged.
    assert c["n0"].client().list_buckets()["Buckets"] == []


def test_misconfiguration_is_refused(make_cluster, binary, tmp_path):
    c = make_cluster(1, CAIRN_STORE_CLUSTER_COPIES="1")
    c["n0"].client().create_bucket(Bucket="one")
    c["n0"].client().put_object(Bucket="one", Key="k", Body=b"v")
    c["n0"].stop()
    base = {"CAIRN_STORE_META": c.meta}
    # The cluster's data directory, opened as a single store: refused (its recovery would discard the copies).
    with pytest.raises(RuntimeError):
        Store(binary, c["n0"].data, _free_port(), env=base).start()
    out = subprocess.run([binary, "fsck", "-data", c["n0"].data], env={**os.environ, **base}, capture_output=True, timeout=60)
    assert out.returncode != 0
    out = subprocess.run([binary, "fsck", "-data", c["n0"].data], env={**os.environ, **base, "CAIRN_STORE_CLUSTER_NODE": "n0"},
                         capture_output=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout)["unreferenced_blobs_removed"] == 0
    # Under another node's name: refused.
    bad = [
        {"CAIRN_STORE_CLUSTER_NODE": "n9"},  # this directory is n0
        {"CAIRN_STORE_CLUSTER_NODE": "n0", "CAIRN_STORE_META": ""},  # no shared metadata
        {"CAIRN_STORE_CLUSTER_NODE": "n0", "CAIRN_STORE_CLUSTER_SECRET": "short"},
        {"CAIRN_STORE_CLUSTER_NODE": "n0", "CAIRN_STORE_PACK_BELOW": "4096"},
        {"CAIRN_STORE_CLUSTER_NODE": "n0", "CAIRN_STORE_CLUSTER_EC": "1+0"},
        {"CAIRN_STORE_CLUSTER_NODE": "Bad_Name"},
    ]
    for extra in bad:
        st = Store(binary, c["n0"].data, _free_port(), env={**c["n0"].extra_env, **extra})
        with pytest.raises(RuntimeError):
            st.start()
            if st.proc.wait(timeout=5) != 0:
                raise RuntimeError("refused")
    c["n0"].start()
    assert c["n0"].client().get_object(Bucket="one", Key="k")["Body"].read() == b"v"


def test_a_single_store_joins_a_cluster(binary, tmp_path):
    meta = new_database()
    data = str(tmp_path / "solo")
    solo = Store(binary, data, _free_port(), env={"CAIRN_STORE_META": meta, "CAIRN_STORE_MASTER_KEY": MASTER}).start()
    s = solo.client()
    s.create_bucket(Bucket="old")
    bodies = {f"a{i}": os.urandom(100 + i * 3000) for i in range(10)}  # small ones are packed, larger ones files
    for k, b in bodies.items():
        s.put_object(Bucket="old", Key=k, Body=b)
    solo.stop()
    c = Cluster(binary, tmp_path, meta)
    try:
        port = solo.port
        c.nodes["n0"] = Store(binary, data, port, env={
            "CAIRN_STORE_META": meta, "CAIRN_STORE_CLUSTER_NODE": "n0", "CAIRN_STORE_CLUSTER_URL": f"http://127.0.0.1:{port}",
            "CAIRN_STORE_CLUSTER_SECRET": SECRET, "CAIRN_STORE_MASTER_KEY": MASTER, **FAST})
        c.add("n1")
        c["n0"].start()
        # Its blobs are placed on the cluster (two copies) and readable from the new node.
        wait_for(lambda: all(b[0].startswith("r:") for b in blob_ids(meta, "old").values()), timeout=30, what="adoption")
        for k, b in bodies.items():
            assert c["n1"].client().get_object(Bucket="old", Key=k)["Body"].read() == b
        assert not os.listdir(os.path.join(data, "packs")) if os.path.isdir(os.path.join(data, "packs")) else True
    finally:
        c.stop()
