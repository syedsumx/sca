"""Hadoop's S3A file system (hadoop-aws 3.4, as used by Hadoop, Spark and Hive) against the store: the Java
check in store/hadoop-check (mkdirs, small and multipart writes, positioned reads, listings, file and directory
renames, content summary, recursive delete). Build it first: `cd store/hadoop-check && ./gradlew installDist`."""

import os
import subprocess
from pathlib import Path

import pytest

from conftest import ADMIN, SCOPED

CHECK = Path(os.environ.get("CAIRN_HADOOP_CHECK") or Path(__file__).resolve().parents[1]
             / "hadoop-check/build/install/cairn-store-hadoop-check/bin/cairn-store-hadoop-check")

pytestmark = pytest.mark.skipif(not CHECK.exists(), reason="build store/hadoop-check first (./gradlew installDist)")


def run(store, key, bucket):
    return subprocess.run([str(CHECK), store.endpoint, key[0], key[1], bucket], capture_output=True, text=True, timeout=300)


def test_s3a_filesystem(store, s3, bucket):
    r = run(store, ADMIN, bucket)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "PASSED" in r.stdout and "ok rename a directory" in r.stdout
    assert s3.list_objects_v2(Bucket=bucket).get("KeyCount", 0) == 0  # the check cleans up after itself


def test_s3a_respects_key_scope(store, bucket):
    r = run(store, SCOPED, bucket)  # SCOPED may only use lake-* buckets
    out = r.stdout + r.stderr
    assert r.returncode != 0 and ("AccessDenied" in out or "403" in out), out
