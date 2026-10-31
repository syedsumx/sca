"""Starts Cairn Store and Cairn Catalog binaries for the catalog conformance suite.

    CAIRN_STORE_BIN=... CAIRN_CATALOG_BIN=... pytest catalog/conformance

Without the variables the binaries are built with `cargo build --release`."""

import os
import socket
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
STORE_KEY = ("cairn-catalog", "catalog-store-secret")
ADMIN_TOKEN = "admin-" + "x" * 24
SALES_TOKEN = "sales-" + "y" * 24   # may only touch namespaces matching "sales*"
READER_TOKEN = "reader-" + "z" * 24


def _port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _bin(env: str, crate: str) -> str:
    if os.environ.get(env):
        return os.environ[env]
    subprocess.run(["cargo", "build", "--release", "--locked"], cwd=ROOT / crate, check=True)
    target = Path(os.environ.get("CARGO_TARGET_DIR", ROOT / crate / "target"))
    return str(target / "release" / f"cairn-{crate}")


def _wait(port: int):
    for _ in range(200):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.05)
    raise RuntimeError(f"nothing listening on {port}")


class Stack:
    def __init__(self, tmp: str):
        self.tmp = tmp
        self.store_port, self.catalog_port = _port(), _port()
        self.store_url = f"http://127.0.0.1:{self.store_port}"
        self.uri = f"http://127.0.0.1:{self.catalog_port}"
        self.procs = []

    def start(self):
        store = subprocess.Popen([_bin("CAIRN_STORE_BIN", "store")], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env={
            **os.environ, "CAIRN_STORE_ACCESS_KEY": STORE_KEY[0], "CAIRN_STORE_SECRET_KEY": STORE_KEY[1],
            "CAIRN_STORE_DATA": f"{self.tmp}/store", "CAIRN_STORE_ADDR": f"127.0.0.1:{self.store_port}",
            "CAIRN_STORE_BUCKETS": "lakehouse", "CAIRN_STORE_FSYNC": "false", "CAIRN_STORE_LOG_LEVEL": "warn"})
        self.procs.append(store)
        _wait(self.store_port)
        tokens = Path(self.tmp, "tokens.json")
        tokens.write_text('[{"token": "%s", "name": "sales", "namespaces": ["sales", "sales.*"]},'
                          ' {"token": "%s", "name": "reader", "read_only": true}]' % (SALES_TOKEN, READER_TOKEN))
        catalog = subprocess.Popen([_bin("CAIRN_CATALOG_BIN", "catalog")], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env={
            **os.environ, "CAIRN_CATALOG_ADDR": f"127.0.0.1:{self.catalog_port}", "CAIRN_CATALOG_DATA": f"{self.tmp}/catalog",
            "CAIRN_CATALOG_TOKEN": ADMIN_TOKEN, "CAIRN_CATALOG_TOKENS_FILE": str(tokens),
            "CAIRN_CATALOG_S3_ENDPOINT": self.store_url, "CAIRN_CATALOG_S3_ACCESS_KEY": STORE_KEY[0],
            "CAIRN_CATALOG_S3_SECRET_KEY": STORE_KEY[1], "CAIRN_CATALOG_WAREHOUSE": "s3://lakehouse/warehouse",
            "CAIRN_CATALOG_FSYNC": "false", "CAIRN_CATALOG_LOG_LEVEL": "warn"})
        self.procs.append(catalog)
        _wait(self.catalog_port)
        return self

    def stop(self):
        for p in reversed(self.procs):
            p.terminate()
            p.wait(15)

    def catalog(self, token: str = ADMIN_TOKEN, name: str = "cairn"):
        from pyiceberg.catalog.rest import RestCatalog
        return RestCatalog(name, uri=self.uri, token=token)


@pytest.fixture(scope="session")
def stack():
    with tempfile.TemporaryDirectory() as d:
        st = Stack(d).start()
        yield st
        st.stop()


@pytest.fixture
def cat(stack):
    return stack.catalog()


@pytest.fixture
def ns(cat):
    name = "ns_" + uuid.uuid4().hex[:8]
    cat.create_namespace(name)
    return name
