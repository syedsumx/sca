"""`cairn-store bench` against a real store (every phase measured, every byte checked, the run's bucket
cleaned up, refusals), and the Cairn Store dashboard and alert rules: every metric they use is one the
store exports (a running store with tiers configured; cluster metrics from the cluster's own exporter),
and the Helm copies match deploy/observability."""

import json
import os
import re
import subprocess

import requests

from conftest import ADMIN

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
OBS = os.path.join(ROOT, "deploy", "observability")
FILES = os.path.join(ROOT, "helm", "cairn", "files")


def bench(binary, st, *args, key=ADMIN):
    env = {**os.environ, "CAIRN_STORE_ENDPOINT": st.endpoint, "CAIRN_STORE_ACCESS_KEY": key[0], "CAIRN_STORE_SECRET_KEY": key[1]}
    return subprocess.run([binary, "bench", *args], env=env, capture_output=True, text=True, timeout=300)


def test_bench_runs_every_phase_and_cleans_up(custom_store, binary):
    st = custom_store({})
    out = bench(binary, st, "--objects", "300", "--size", "16KiB", "--concurrency", "8")
    assert out.returncode == 0, out.stderr + out.stdout
    rep = json.loads(out.stdout)
    assert [p["op"] for p in rep["phases"]] == ["put", "get", "list", "delete"]
    for p in rep["phases"]:
        assert p["errors"] == 0 and p["ops_per_second"] > 0
        assert 0 < p["latency_ms"]["p50"] <= p["latency_ms"]["p99"] <= p["latency_ms"]["max"]
    put, get, lst, _ = rep["phases"]
    assert put["ops"] == get["ops"] == 300 and lst["ops"] == 1  # 300 keys: one page
    assert abs(get["mib_per_second"] * get["seconds"] - 300 * 16 / 1024) < 0.01  # every byte read back
    assert rep["bucket_removed"] and not rep["objects_left"]
    assert "cairn-bench" not in [b["Name"] for b in st.client().list_buckets()["Buckets"]]
    metrics = requests.get(f"{st.endpoint}/-/metrics", timeout=10).text
    assert re.search(r'cairn_store_requests_total\{op="PutObject",status="200"\} 300\b', metrics)


def test_bench_keeps_what_it_is_told_and_fails_loudly(custom_store, binary):
    st = custom_store({})
    s3 = st.client()
    s3.create_bucket(Bucket="existing")
    s3.put_object(Bucket="existing", Key="mine.txt", Body=b"not touched")
    out = bench(binary, st, "--bucket", "existing", "--objects", "20", "--size", "1k", "--ops", "put,get")
    assert out.returncode == 0, out.stderr
    rep = json.loads(out.stdout)
    assert rep["objects_left"] and not rep["bucket_removed"]
    keys = [o["Key"] for o in s3.list_objects_v2(Bucket="existing")["Contents"]]
    assert "mine.txt" in keys and sum(k.startswith(rep["prefix"]) for k in keys) == 20
    for bad in (["--size", "lots"], ["--ops", "get"], ["--ops", "put,scan"], ["--concurrency", "0"], ["--nope"]):
        assert bench(binary, st, *bad).returncode == 2, bad
    wrong = bench(binary, st, "--objects", "5", key=(ADMIN[0], "not-the-secret"))
    assert wrong.returncode == 2 and "cannot use bucket" in wrong.stderr


def referenced(text):
    return set(re.findall(r"\bcairn_store_[a-z_]+", text))


def exported(text):
    return set(re.findall(r"^# TYPE (cairn_store_[a-z_]+) ", text, re.M))


def test_dashboard_and_alerts_use_only_exported_metrics(custom_store, tmp_path):
    tiers = tmp_path / "tiers.json"
    tiers.write_text(json.dumps([{"storage_class": "STANDARD_IA", "endpoint": "http://127.0.0.1:9", "bucket": "cold",
                                  "access_key": "k", "secret_key": "s"}]))
    st = custom_store({"CAIRN_STORE_TIERS_FILE": str(tiers)})
    have = exported(requests.get(f"{st.endpoint}/-/metrics", timeout=10).text)
    # Cluster metrics come from the cluster's exporter (a single store does not run it): its names.
    src = open(os.path.join(os.path.dirname(__file__), "..", "src", "s3", "cluster.rs")).read()
    have |= {"cairn_store_cluster_nodes"} | {f"cairn_store_cluster_{n}_total" for n in re.findall(r'\("([a-z_]+)", &m\.', src)}
    dash = json.load(open(os.path.join(OBS, "cairn-store.json")))
    exprs = " ".join(t["expr"] for p in dash["panels"] for t in p.get("targets", []))
    alerts = open(os.path.join(OBS, "cairn-store-alerts.yaml")).read()
    missing = (referenced(exprs) | referenced(alerts)) - have
    assert not missing, f"the dashboard or alerts use metrics the store does not export: {sorted(missing)}"
    assert len(dash["panels"]) > 20 and dash["uid"] == "cairn-store"
    for f in ("cairn-store.json", "cairn-store-alerts.yaml"):
        assert open(os.path.join(OBS, f)).read() == open(os.path.join(FILES, f)).read(), f
