"""OpenLineage to Cairn without a stored key.

Airflow's OpenLineage HTTP transport authenticates with a fixed API key by default. This token provider
gives it the pipeline user's Cairn token instead: exchanged from the pod's workload token (a Kubernetes
projected service-account token) and renewed before it expires::

    AIRFLOW__OPENLINEAGE__TRANSPORT='{"type": "http", "url": "https://cairn.example.com/cairn/api",
      "endpoint": "openlineage/api/v1/lineage",
      "auth": {"type": "cairn_airflow.lineage.CairnTokenProvider",
               "workload_token_file": "/var/run/secrets/cairn/token"}}'

Settings: ``workload_token_file``; ``cairn_url`` (default: CAIRN_URL); ``ca_file`` (default:
CAIRN_CA_FILE). Without a workload token file it uses ``token_env`` (CAIRN_TOKEN).
"""

from __future__ import annotations

import os
import threading
import time

import httpx

try:  # the OpenLineage client's base class, when it is installed (in Airflow it is)
    from openlineage.client.transport.http import TokenProvider
except Exception:  # noqa: BLE001
    class TokenProvider:  # type: ignore[no-redef]
        def __init__(self, config: dict):
            self.config = config


class CairnTokenProvider(TokenProvider):
    def __init__(self, config: dict):
        super().__init__(config)
        self.workload_token_file = config.get("workload_token_file", "")
        self.cairn_url = (config.get("cairn_url") or os.environ.get("CAIRN_URL", "")).rstrip("/")
        self.ca_file = config.get("ca_file") or os.environ.get("CAIRN_CA_FILE", "")
        self.token_env = config.get("token_env", "CAIRN_TOKEN")
        self._token, self._until = "", 0.0
        self._lock = threading.Lock()

    def _exchange(self) -> tuple[str, float]:
        with open(self.workload_token_file) as f:
            subject = f.read().strip()
        with httpx.Client(timeout=30, verify=self.ca_file or True, trust_env=False) as c:
            r = c.post(f"{self.cairn_url}/v1/auth/workload-token", data={
                "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange", "subject_token": subject,
                "subject_token_type": "urn:ietf:params:oauth:token-type:jwt"})
        r.raise_for_status()
        body = r.json()
        return body["access_token"], time.time() + int(body.get("expires_in", 300)) * 0.8

    def get_bearer(self) -> str:
        if not self.workload_token_file:
            return f"Bearer {os.environ.get(self.token_env, '')}"
        with self._lock:
            if time.time() >= self._until:
                self._token, self._until = self._exchange()
            return f"Bearer {self._token}"
