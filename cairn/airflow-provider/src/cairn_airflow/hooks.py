"""The ``cairn`` connection: Cairn's API and a pipeline user's Cairn token.

* **host** — Cairn's API, e.g. ``https://cairn.example.com/cairn/api`` (a full URL; or schema + host + port)
* **password** — the pipeline user's Cairn token (a personal access token of e.g. ``svc-airflow``)
* **extra** — ``trino_host``, ``trino_port`` (8443), ``trino_catalog`` (lakehouse), ``ca_file`` (a CA for
  Cairn's and Trino's certificates; default: the system's)

Defined like any Airflow connection, e.g. ``AIRFLOW_CONN_CAIRN_DEFAULT='{"conn_type": "cairn", "host":
"https://cairn.example.com/cairn/api", "password": "...", "extra": {"trino_host": "cairn-trino"}}'``, or
from a secrets backend.
"""

from __future__ import annotations

from typing import Any, Optional

import httpx

try:  # Airflow 3
    from airflow.sdk.bases.hook import BaseHook
except ImportError:  # pragma: no cover - older 3.x layouts
    from airflow.hooks.base import BaseHook


class CairnError(Exception):
    pass


class CairnHook(BaseHook):
    conn_name_attr = "cairn_conn_id"
    default_conn_name = "cairn_default"
    conn_type = "cairn"
    hook_name = "Cairn"

    @classmethod
    def get_ui_field_behaviour(cls) -> dict[str, Any]:
        return {"hidden_fields": ["schema", "login", "port"],
                "relabeling": {"host": "Cairn API URL", "password": "Cairn token"},
                "placeholders": {"host": "https://cairn.example.com/cairn/api",
                                 "extra": '{"trino_host": "cairn-trino", "trino_port": 8443, "trino_catalog": "lakehouse"}'}}

    def __init__(self, cairn_conn_id: str = default_conn_name, **kwargs):
        super().__init__(**kwargs)
        self.cairn_conn_id = cairn_conn_id
        self._conn = None

    @property
    def conn(self):
        if self._conn is None:
            self._conn = self.get_connection(self.cairn_conn_id)
        return self._conn

    @property
    def extra(self) -> dict:
        return self.conn.extra_dejson or {}

    @property
    def url(self) -> str:
        host = (self.conn.host or "").rstrip("/")
        if not host:
            raise CairnError(f"connection {self.cairn_conn_id}: set host to Cairn's API URL")
        if "://" not in host:
            scheme = self.conn.schema or "https"
            host = f"{scheme}://{host}" + (f":{self.conn.port}" if self.conn.port else "")
        return host

    @property
    def token(self) -> str:
        if not self.conn.password:
            raise CairnError(f"connection {self.cairn_conn_id}: set password to the pipeline user's Cairn token")
        return self.conn.password

    @property
    def verify(self):
        return self.extra.get("ca_file") or True

    def trino_token(self, ttl: int = 3600) -> dict:
        """A short-lived Trino token for the connection's Cairn user."""
        with httpx.Client(timeout=30, verify=self.verify) as c:
            r = c.post(f"{self.url}/v1/trino/token", json={"ttl_seconds": ttl},
                       headers={"Authorization": f"Bearer {self.token}"})
        if r.status_code != 200:
            raise CairnError(f"Cairn refused a Trino token ({r.status_code}): {r.text[:300]}")
        return r.json()

    def trino_connection(self, schema: Optional[str] = None, ttl: int = 3600, source: str = "airflow-cairn"):
        """A Trino DB-API connection signed in as the connection's Cairn user (TLS, a fresh token)."""
        import requests
        import trino
        from trino.auth import JWTAuthentication

        tt = self.trino_token(ttl)
        # Its own session: requests would let REQUESTS_CA_BUNDLE override the connection's CA file.
        session = requests.Session()
        session.trust_env = False
        session.verify = self.verify
        conn = trino.dbapi.connect(
            host=self.extra.get("trino_host", "cairn-trino"), port=int(self.extra.get("trino_port", 8443)),
            user=tt["user"], catalog=self.extra.get("trino_catalog", "lakehouse"), schema=schema,
            http_scheme="https", auth=JWTAuthentication(tt["token"]), source=source, http_session=session)
        return conn, tt["user"]

    def env(self) -> dict[str, str]:
        """The environment ``cairn dbt`` signs in with."""
        out = {"CAIRN_URL": self.url, "CAIRN_TOKEN": self.token,
               "CAIRN_TRINO_HOST": str(self.extra.get("trino_host", "cairn-trino")),
               "CAIRN_TRINO_PORT": str(self.extra.get("trino_port", 8443)),
               "CAIRN_TRINO_CATALOG": str(self.extra.get("trino_catalog", "lakehouse"))}
        if self.extra.get("ca_file"):
            out["CAIRN_CA_FILE"] = self.extra["ca_file"]
        return out

    def test_connection(self):
        try:
            tt = self.trino_token(60)
            return True, f"Cairn signs this connection in to Trino as {tt['user']}"
        except Exception as e:  # noqa: BLE001 - shown in Airflow's UI
            return False, str(e)
