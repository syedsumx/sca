"""A secrets backend: Airflow's own operators on the lakehouse, signed in as a Cairn identity.

With ``AIRFLOW__SECRETS__BACKEND=cairn_airflow.secrets.CairnSecretsBackend``:

* ``trino_lakehouse`` (``trino_conn_id``) is a Trino connection with a fresh short-lived token for the
  pipeline user each time it is looked up: ``SQLExecuteQueryOperator(conn_id="trino_lakehouse", ...)``
  and ``TrinoOperator`` run under Cairn's policies, masks and row filters, with no stored password.
* ``cairn_default`` (``cairn_conn_id``) is the ``cairn`` connection, from the backend's settings.

Settings (``AIRFLOW__SECRETS__BACKEND_KWARGS``, JSON): ``cairn_url``; the identity, either
``workload_token_file`` (a projected service-account token, exchanged at Cairn) or ``token_env`` (the
name of an environment variable holding a Cairn token); ``ca_file``; ``trino_host``, ``trino_port``,
``trino_catalog``. Other connections are not this backend's: Airflow looks them up elsewhere.
"""

from __future__ import annotations

import json
import os
from typing import Optional

from airflow.secrets import BaseSecretsBackend


class CairnSecretsBackend(BaseSecretsBackend):
    def __init__(self, cairn_url: str = "", workload_token_file: str = "", token_env: str = "CAIRN_TOKEN",
                 ca_file: str = "", trino_host: str = "cairn-trino", trino_port: int = 8443,
                 trino_catalog: str = "lakehouse", trino_conn_id: str = "trino_lakehouse",
                 cairn_conn_id: str = "cairn_default", token_ttl: int = 3600, **kwargs):
        super().__init__(**kwargs)
        self.cairn_url = (cairn_url or os.environ.get("CAIRN_URL", "")).rstrip("/")
        self.workload_token_file, self.token_env = workload_token_file, token_env
        self.ca_file = ca_file or os.environ.get("CAIRN_CA_FILE", "")
        self.trino = {"host": trino_host, "port": int(trino_port), "catalog": trino_catalog}
        self.trino_conn_id, self.cairn_conn_id, self.token_ttl = trino_conn_id, cairn_conn_id, int(token_ttl)

    def _cairn_conn(self) -> dict:
        extra = {"trino_host": self.trino["host"], "trino_port": self.trino["port"],
                 "trino_catalog": self.trino["catalog"]}
        if self.ca_file:
            extra["ca_file"] = self.ca_file
        out = {"conn_type": "cairn", "host": self.cairn_url, "extra": extra}
        if self.workload_token_file:
            extra["workload_token_file"] = self.workload_token_file
        else:
            token = os.environ.get(self.token_env, "")
            if not token:
                raise ValueError(f"CairnSecretsBackend: set workload_token_file, or {self.token_env}")
            out["password"] = token
        return out

    def get_conn_value(self, conn_id: str) -> Optional[str]:
        if conn_id == self.cairn_conn_id:
            return json.dumps(self._cairn_conn())
        if conn_id != self.trino_conn_id:
            return None
        import httpx

        conn = self._cairn_conn()
        if self.workload_token_file:
            with open(self.workload_token_file) as f:
                subject = f.read().strip()
            with httpx.Client(timeout=30, verify=self.ca_file or True, trust_env=False) as c:
                r = c.post(f"{self.cairn_url}/v1/auth/workload-token", data={
                    "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange", "subject_token": subject,
                    "subject_token_type": "urn:ietf:params:oauth:token-type:jwt"})
                r.raise_for_status()
                cairn_token = r.json()["access_token"]
        else:
            cairn_token = conn["password"]
        with httpx.Client(timeout=30, verify=self.ca_file or True, trust_env=False) as c:
            r = c.post(f"{self.cairn_url}/v1/trino/token", json={"ttl_seconds": self.token_ttl},
                       headers={"Authorization": f"Bearer {cairn_token}"})
            r.raise_for_status()
            t = r.json()
        extra = {"auth": "jwt", "jwt__token": t["token"], "protocol": "https", "catalog": self.trino["catalog"],
                 "verify": self.ca_file or True, "source": "airflow-cairn"}
        return json.dumps({"conn_type": "trino", "host": self.trino["host"], "port": self.trino["port"],
                           "login": t["user"], "extra": extra})
