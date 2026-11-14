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


PG = os.environ.get("CAIRN_CATALOG_TEST_DATABASE_URL", "")


def fresh_database() -> str:
    """A new, empty PostgreSQL database for one stack (CAIRN_CATALOG_TEST_DATABASE_URL names the server)."""
    import psycopg

    name = "cairn_catalog_" + uuid.uuid4().hex[:10]
    with psycopg.connect(PG, autocommit=True) as c:
        c.execute(f"CREATE DATABASE {name}")
    base, _, query = PG.partition("?")
    return f"{base.rsplit('/', 1)[0]}/{name}" + (f"?{query}" if query else "")


class Stack:
    """A store and catalog; with CAIRN_CATALOG_TEST_DATABASE_URL the catalog keeps its state in a fresh
    PostgreSQL database, and `replicas` catalogs share it."""

    def __init__(self, tmp: str, store_env: dict | None = None, catalog_env: dict | None = None, replicas: int = 1):
        self.tmp = tmp
        self.store_env, self.catalog_env = store_env or {}, dict(catalog_env or {})
        if PG and "CAIRN_CATALOG_DATABASE_URL" not in self.catalog_env:
            self.catalog_env["CAIRN_CATALOG_DATABASE_URL"] = fresh_database()
        assert replicas == 1 or "CAIRN_CATALOG_DATABASE_URL" in self.catalog_env, "replicas share PostgreSQL"
        self.store_port, self.catalog_ports = _port(), [_port() for _ in range(replicas)]
        self.catalog_port = self.catalog_ports[0]
        self.store_url = f"http://127.0.0.1:{self.store_port}"
        self.uris = [f"http://127.0.0.1:{p}" for p in self.catalog_ports]
        self.uri = self.uris[0]
        self.procs = []
        self.replicas = []

    def _store(self):
        return subprocess.Popen([_bin("CAIRN_STORE_BIN", "store")], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env={
            **os.environ, "CAIRN_STORE_ACCESS_KEY": STORE_KEY[0], "CAIRN_STORE_SECRET_KEY": STORE_KEY[1],
            "CAIRN_STORE_DATA": f"{self.tmp}/store", "CAIRN_STORE_ADDR": f"127.0.0.1:{self.store_port}",
            "CAIRN_STORE_BUCKETS": "lakehouse", "CAIRN_STORE_FSYNC": "false", "CAIRN_STORE_LOG_LEVEL": "warn",
            **self.store_env})

    def restart_store(self, kill: bool = True, down: float = 0):
        """The store killed (SIGKILL: no clean shutdown), left down ``down`` seconds, started again on its data."""
        old = self.procs[0]
        old.kill() if kill else old.terminate()
        old.wait(15)
        time.sleep(down)
        self.procs[0] = self._store()
        _wait(self.store_port)

    def start(self):
        store = self._store()
        self.procs.append(store)
        _wait(self.store_port)
        tokens = Path(self.tmp, "tokens.json")
        tokens.write_text('[{"token": "%s", "name": "sales", "namespaces": ["sales", "sales.*"]},'
                          ' {"token": "%s", "name": "reader", "read_only": true}]' % (SALES_TOKEN, READER_TOKEN))
        for i, port in enumerate(self.catalog_ports):
            catalog = subprocess.Popen([_bin("CAIRN_CATALOG_BIN", "catalog")], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env={
                **os.environ, "CAIRN_CATALOG_ADDR": f"127.0.0.1:{port}", "CAIRN_CATALOG_DATA": f"{self.tmp}/catalog{i or ''}",
                "CAIRN_CATALOG_TOKEN": ADMIN_TOKEN, "CAIRN_CATALOG_TOKENS_FILE": str(tokens),
                "CAIRN_CATALOG_S3_ENDPOINT": self.store_url, "CAIRN_CATALOG_S3_ACCESS_KEY": STORE_KEY[0],
                "CAIRN_CATALOG_S3_SECRET_KEY": STORE_KEY[1], "CAIRN_CATALOG_WAREHOUSE": "s3://lakehouse/warehouse",
                "CAIRN_CATALOG_FSYNC": "false", "CAIRN_CATALOG_LOG_LEVEL": "warn", **self.catalog_env})
            self.procs.append(catalog)
            self.replicas.append(catalog)
        for port in self.catalog_ports:
            _wait(port)
        return self

    def stop(self):
        for p in reversed(self.procs):
            p.terminate()
            p.wait(15)

    def catalog(self, token: str = ADMIN_TOKEN, name: str = "cairn", replica: int = 0):
        from pyiceberg.catalog.rest import RestCatalog
        return RestCatalog(f"{name}{replica or ''}", uri=self.uris[replica], token=token)


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
