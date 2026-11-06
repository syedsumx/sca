"""A tenant's replicated OpenSearch and Kafka (queue row 19): the settings tenant.sh renders keep the tenant
working when one node is lost, and refuse writes rather than lose them when two are.

The manifest is rendered by tenant.sh itself (CAIRN_TENANT_SERVICES_HA=1); each StatefulSet's container (image,
command, environment) is then run three times in Docker under the names its pods have in Kubernetes
(kafka-0.kafka-headless.<ns>.svc …), so this checks the real settings, not a copy of them. Scheduling, volumes and
the NetworkPolicy are Kubernetes' part: deploy/hetzner/ci/k3s-smoke.sh runs the same manifest on k3s."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import uuid

import pytest
import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NS = "cairn-hatest"


def _docker() -> bool:
    return shutil.which("docker") is not None and subprocess.run(["docker", "info"], capture_output=True).returncode == 0


pytestmark = pytest.mark.skipif(not _docker(), reason="needs docker")


def render(**env) -> list[dict]:
    out = subprocess.run(["deploy/hosting/tenant.sh", "render-services", NS.removeprefix("cairn-")], cwd=ROOT,
                         env={**os.environ, "CAIRN_TENANT_SERVICES": "1", "CAIRN_TENANT_SERVICES_HA": "1",
                              "CAIRN_TENANT_SERVICES_SPREAD": "ScheduleAnyway", **env},
                         capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    return [d for d in yaml.safe_load_all(out.stdout) if d]


def container(docs: list[dict], name: str) -> dict:
    sts = next(d for d in docs if d["kind"] == "StatefulSet" and d["metadata"]["name"] == name)
    assert sts["spec"]["replicas"] == 3
    return sts["spec"]["template"]["spec"]["containers"][0]


class Cluster:
    """Three containers of one StatefulSet's pod template, on a network of their own, named as the pods are."""

    def __init__(self, name: str, spec: dict, data_dir: str, extra: tuple = ()):
        self.name, self.spec, self.data_dir, self.extra = name, spec, data_dir, list(extra)
        self.net = f"cairn-ha-{uuid.uuid4().hex[:8]}"
        self.ids = {}
        subprocess.run(["docker", "network", "create", self.net], check=True, capture_output=True)

    def start(self, i: int) -> None:
        pod = f"{self.name}-{i}"
        args = ["docker", "run", "-d", "--rm", "--name", f"{self.net}-{pod}", "--network", self.net, "--hostname", pod,
                "--network-alias", pod, "--network-alias", f"{pod}.{self.name}-headless.{NS}.svc",
                "--network-alias", f"{self.name}-headless", "--user", "1000:1000",
                "--tmpfs", f"{self.data_dir}:uid=1000,gid=1000", "--memory", "1500m", *self.extra]
        for e in self.spec.get("env", []):
            value = pod if "valueFrom" in e else e["value"]          # the pod name, from metadata.name
            args += ["-e", f"{e['name']}={value}"]
        if self.spec.get("command"):
            args += ["--entrypoint", self.spec["command"][0]]
        args.append(self.spec["image"])
        args += self.spec.get("command", [])[1:]
        self.ids[i] = subprocess.run(args, check=True, capture_output=True, text=True).stdout.strip()

    def stop(self, i: int) -> None:
        subprocess.run(["docker", "kill", f"{self.net}-{self.name}-{i}"], capture_output=True)

    def exec(self, i: int, *cmd: str, timeout: int = 120) -> subprocess.CompletedProcess:
        return subprocess.run(["docker", "exec", f"{self.net}-{self.name}-{i}", *cmd], capture_output=True, text=True,
                              timeout=timeout)

    def close(self) -> None:
        for i in range(3):
            self.stop(i)
        time.sleep(1)
        subprocess.run(["docker", "network", "rm", self.net], capture_output=True)


def wait(check, what: str, seconds: int = 180):
    end = time.time() + seconds
    while True:
        try:
            got = check()
            if got:
                return got
        except Exception:  # noqa: BLE001 - not up yet
            pass
        assert time.time() < end, f"{what} did not happen in {seconds} s"
        time.sleep(2)


@pytest.fixture
def kafka():
    spec = container(render(CAIRN_TENANT_KAFKA_HEAP="256m"), "kafka")
    c = Cluster("kafka", spec, "/var/lib/kafka/data")
    try:
        for i in range(3):
            c.start(i)
        yield c
    finally:
        c.close()


KT = "/opt/kafka/bin"
BOOT = ",".join(f"kafka-{i}.kafka-headless.{NS}.svc:9092" for i in range(3))


def test_kafka_survives_a_lost_broker_and_refuses_writes_it_could_lose(kafka):
    k = kafka
    quorum = wait(lambda: (r := k.exec(1, f"{KT}/kafka-metadata-quorum.sh", "--bootstrap-server", BOOT, "describe",
                                       "--status")).returncode == 0 and "CurrentVoters" in r.stdout and r.stdout,
                  "the controller quorum")
    assert "[0,1,2]" in quorum.replace(" ", "") or all(f'"id": {i}' in quorum or f'"id":{i}' in quorum for i in range(3))
    # A topic created the way Cairn's are (no explicit replication): three copies, two needed in sync.
    assert k.exec(1, f"{KT}/kafka-topics.sh", "--bootstrap-server", BOOT, "--create", "--topic", "events",
                  "--partitions", "3").returncode == 0
    desc = k.exec(1, f"{KT}/kafka-topics.sh", "--bootstrap-server", BOOT, "--describe", "--topic", "events").stdout
    assert "ReplicationFactor: 3" in desc and "min.insync.replicas=2" in desc, desc

    def produce(i, text):
        # Bounded inside the container (exit 124 when cut off): with brokers gone the client can wait on metadata
        # and on names that no longer resolve well past its own timeouts.
        return k.exec(i, "bash", "-c", f"echo '{text}' | timeout 100 {KT}/kafka-console-producer.sh --bootstrap-server "
                                       f"{BOOT} --topic events --producer-property acks=all --producer-property "
                                       "delivery.timeout.ms=20000 --producer-property request.timeout.ms=10000 "
                                       "--producer-property max.block.ms=15000", timeout=150)

    def consume(i):
        r = k.exec(i, f"{KT}/kafka-console-consumer.sh", "--bootstrap-server", BOOT, "--topic", "events",
                   "--from-beginning", "--timeout-ms", "15000", timeout=90)
        return sorted(r.stdout.split())

    assert produce(1, "before").returncode == 0
    # Consumers are running before a node is ever lost: the first one makes Kafka create __consumer_offsets (three
    # copies, which needs all three brokers). Created after a loss it could not be, and no group would find its
    # coordinator.
    assert wait(lambda: consume(2) == ["before"], "a consumer with every broker up", 120)
    # One broker gone (its node lost): writes still need two copies and get them; nothing written is lost.
    k.stop(0)
    wait(lambda: produce(1, "after").returncode == 0, "a write with one broker down", 120)
    assert consume(2) == ["after", "before"]
    # Two gone: the write is not acknowledged rather than kept as the only copy. The console producer still exits
    # 0 on a refused record, so the refusal is read from what it reports; cut off without an acknowledgement
    # (124) is a refusal too.
    k.stop(2)
    lone = produce(1, "unsafe")
    assert (lone.returncode == 124 or "NotEnoughReplicas" in lone.stderr or "Expiring 1 record" in lone.stderr
            or "TimeoutException" in lone.stderr), (lone.returncode, lone.stderr)


def _file_limit() -> int:
    import resource

    return resource.getrlimit(resource.RLIMIT_NOFILE)[1]


@pytest.fixture
def opensearch():
    # A cluster node checks it may open 65,535 files; Kubernetes' container runtimes allow about a million.
    if 0 <= _file_limit() < 65535:
        pytest.skip(f"this host allows {_file_limit()} open files; an OpenSearch cluster node needs 65,535")
    spec = container(render(CAIRN_TENANT_SEARCH_HEAP="384m"), "opensearch")
    c = Cluster("opensearch", spec, "/usr/share/opensearch/data", ("--ulimit", "nofile=65536:65536"))
    try:
        for i in range(3):
            c.start(i)
        yield c
    finally:
        c.close()


def test_opensearch_survives_a_lost_node_with_every_document(opensearch):
    o = opensearch

    def curl(i, path, method="GET", body=None):
        cmd = ["curl", "-s", "-X", method, f"http://localhost:9200{path}", "-H", "Content-Type: application/json"]
        if body is not None:
            cmd += ["-d", json.dumps(body)]
        r = o.exec(i, *cmd)
        return json.loads(r.stdout) if r.returncode == 0 and r.stdout.strip().startswith("{") else None

    health = wait(lambda: (h := curl(1, "/_cluster/health")) and h.get("number_of_nodes") == 3 and h, "a three-node cluster")
    assert health["status"] in ("green", "yellow")
    # An index made as Cairn Core makes them (one replica, its default), with documents in it.
    assert curl(1, "/cairn_datasetindex_v2", "PUT", {"settings": {"number_of_shards": 3, "number_of_replicas": 1}})["acknowledged"]
    for n in range(20):
        curl(1, f"/cairn_datasetindex_v2/_doc/{n}", "PUT", {"name": f"table_{n}"})
    curl(1, "/cairn_datasetindex_v2/_refresh", "POST")
    wait(lambda: curl(1, "/_cluster/health")["status"] == "green", "green with every replica placed")
    o.stop(0)
    after = wait(lambda: (h := curl(1, "/_cluster/health")) and h.get("number_of_nodes") == 2 and h, "the cluster without node 0")
    assert after["status"] in ("yellow", "green")                       # every primary still there (never red)
    wait(lambda: curl(2, "/cairn_datasetindex_v2/_count")["count"] == 20, "every document readable")
    assert curl(2, "/cairn_datasetindex_v2/_doc/new", "PUT", {"name": "written while a node was down"})["result"] == "created"
    o.start(0)                                                         # the node comes back and rejoins
    wait(lambda: (h := curl(1, "/_cluster/health")) and h.get("number_of_nodes") == 3 and h["status"] == "green",
         "green again with node 0 back", 240)
