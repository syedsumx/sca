"""Starts a real cairn-store binary for the conformance suite.

    CAIRN_STORE_BIN=/path/to/cairn-store pytest store/conformance

Without CAIRN_STORE_BIN the binary is built with `go build`. With CAIRN_STORE_ENDPOINT the suite runs
against a store that is already running (a container, say), which must know the keys below
(credentials file: `python conftest.py` prints it); the crash tests, which restart the store, are skipped."""

import json
import os
import socket
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

import boto3
import pytest
from botocore.config import Config

ADMIN = ("cairnadmin", "cairn-admin-secret")
READER = ("cairnreader", "cairn-reader-secret")
SCOPED = ("cairnlake", "cairn-lake-secret")  # may only touch buckets matching "lake-*"
REGION = "us-east-1"
ROOT = Path(__file__).resolve().parents[1]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def binary(tmp_path_factory) -> str:
    if os.environ.get("CAIRN_STORE_BIN"):
        return os.environ["CAIRN_STORE_BIN"]
    out = tmp_path_factory.mktemp("bin") / "cairn-store"
    subprocess.run(["go", "build", "-o", str(out), "./cmd/cairn-store"], cwd=ROOT, check=True)
    return str(out)


class Store:
    def __init__(self, binary: str, data: str, port: int, fsync: bool = False):
        self.binary, self.data, self.port, self.fsync = binary, data, port, fsync
        self.endpoint = f"http://127.0.0.1:{port}"
        self.proc = None

    def start(self):
        creds = Path(self.data).parent / f"creds-{self.port}.json"
        creds.write_text(json.dumps(credentials_file()))
        env = {**os.environ, "CAIRN_STORE_ACCESS_KEY": ADMIN[0], "CAIRN_STORE_SECRET_KEY": ADMIN[1],
               "CAIRN_STORE_CREDENTIALS_FILE": str(creds), "CAIRN_STORE_DATA": self.data,
               "CAIRN_STORE_ADDR": f"127.0.0.1:{self.port}", "CAIRN_STORE_FSYNC": "true" if self.fsync else "false",
               "CAIRN_STORE_LOG_LEVEL": "warn"}
        self.proc = subprocess.Popen([self.binary], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(100):
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=0.2):
                    return self
            except OSError:
                time.sleep(0.05)
        raise RuntimeError("cairn-store did not start")

    def stop(self, kill: bool = False):
        if self.proc and self.proc.poll() is None:
            (self.proc.kill if kill else self.proc.terminate)()
            self.proc.wait(10)

    def client(self, key=ADMIN, **cfg):
        s3cfg = {"addressing_style": "path", **cfg.pop("s3", {})}
        return boto3.client("s3", endpoint_url=self.endpoint, aws_access_key_id=key[0], aws_secret_access_key=key[1],
                            region_name=REGION, config=Config(s3=s3cfg, retries={"max_attempts": 0}, **cfg))


def credentials_file() -> list[dict]:
    return [{"access_key": READER[0], "secret_key": READER[1], "read_only": True},
            {"access_key": SCOPED[0], "secret_key": SCOPED[1], "buckets": ["lake-*"]}]


class RunningStore(Store):
    def __init__(self, endpoint: str):
        self.endpoint, self.proc = endpoint.rstrip("/"), None

    def start(self):
        return self

    def stop(self, kill: bool = False):
        pass


@pytest.fixture(scope="session")
def store(tmp_path_factory, request):
    if os.environ.get("CAIRN_STORE_ENDPOINT"):
        yield RunningStore(os.environ["CAIRN_STORE_ENDPOINT"])
        return
    binary = request.getfixturevalue("binary")
    data = str(tmp_path_factory.mktemp("data") / "store")
    st = Store(binary, data, _free_port()).start()
    yield st
    st.stop()


@pytest.fixture
def s3(store):
    return store.client()


@pytest.fixture
def bucket(s3):
    name = "t-" + uuid.uuid4().hex[:16]
    s3.create_bucket(Bucket=name)
    return name


@pytest.fixture
def fresh_store(request):
    """A store of its own, with fsync on (crash tests restart it)."""
    if os.environ.get("CAIRN_STORE_ENDPOINT"):
        pytest.skip("needs a store this suite can kill and restart")
    binary = request.getfixturevalue("binary")
    with tempfile.TemporaryDirectory() as d:
        st = Store(binary, os.path.join(d, "store"), _free_port(), fsync=True)
        yield st
        st.stop(kill=True)


if __name__ == "__main__":
    print(json.dumps(credentials_file(), indent=2))
