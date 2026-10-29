"""In-VPC secret resolution for recipes (``${NAME}`` references).

Resolution order is configurable; secrets never leave the agent process. Supported
backends: environment variables, a mounted directory (Kubernetes secrets / CSI),
HashiCorp Vault KV v2, AWS Secrets Manager, Google Cloud Secret Manager and Azure Key Vault.

The cloud backends use the agent's own identity: on GCP, workload identity or the VM's service
account (the metadata server), or a token file; on Azure, a managed identity, workload identity
federation (``AZURE_FEDERATED_TOKEN_FILE``), or a client secret.
"""

from __future__ import annotations

import base64
import os
import re
import time
from typing import Any, Optional, Protocol

import httpx

_REF = re.compile(r"\$\{([A-Za-z0-9_]+)\}")


class SecretNotFound(KeyError):
    pass


class SecretBackend(Protocol):
    def get(self, name: str) -> Optional[str]: ...


class EnvBackend:
    def __init__(self, prefix: str = ""):
        self.prefix = prefix

    def get(self, name: str) -> Optional[str]:
        return os.environ.get(self.prefix + name)


class DirectoryBackend:
    def __init__(self, path: str = "/var/run/secrets/cairn"):
        self.path = path

    def get(self, name: str) -> Optional[str]:
        p = os.path.join(self.path, name)
        if not os.path.isfile(p):
            return None
        with open(p) as fh:
            return fh.read().rstrip("\n")


class VaultBackend:
    """KV v2: secret ``NAME`` is read from ``<mount>/data/<path>`` key ``NAME``."""

    def __init__(self, addr: str, token: Optional[str] = None, mount: str = "secret", path: str = "cairn",
                 token_file: Optional[str] = None, timeout: float = 10.0):
        self.addr = addr.rstrip("/")
        self.mount = mount
        self.path = path
        if token is None and token_file and os.path.exists(token_file):
            with open(token_file) as fh:
                token = fh.read().strip()
        self.token = token or os.environ.get("VAULT_TOKEN")
        self._client = httpx.Client(timeout=timeout)
        self._cache: Optional[dict] = None

    def get(self, name: str) -> Optional[str]:
        if self._cache is None:
            resp = self._client.get(f"{self.addr}/v1/{self.mount}/data/{self.path}",
                                    headers={"X-Vault-Token": self.token or ""})
            resp.raise_for_status()
            self._cache = resp.json().get("data", {}).get("data", {})
        value = self._cache.get(name)
        return None if value is None else str(value)


class AwsSecretsManagerBackend:
    def __init__(self, prefix: str = "cairn/", region: Optional[str] = None):
        import boto3  # optional dependency, imported lazily

        self.prefix = prefix
        self._client = boto3.client("secretsmanager", region_name=region)

    def get(self, name: str) -> Optional[str]:
        try:
            return self._client.get_secret_value(SecretId=self.prefix + name)["SecretString"]
        except self._client.exceptions.ResourceNotFoundException:
            return None


class GcpSecretManagerBackend:
    """``NAME`` is read from ``projects/<project>/secrets/<prefix>NAME/versions/latest``."""

    METADATA = "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token"

    def __init__(self, project: str, prefix: str = "", token_file: Optional[str] = None, timeout: float = 10.0,
                 http: Optional[httpx.Client] = None):
        self.project = project
        self.prefix = prefix
        self.token_file = token_file
        self._http = http or httpx.Client(timeout=timeout)
        self._token: tuple[float, str] = (0.0, "")

    def _bearer(self) -> str:
        if self.token_file and os.path.exists(self.token_file):
            with open(self.token_file) as fh:
                return fh.read().strip()
        if os.environ.get("GOOGLE_OAUTH_ACCESS_TOKEN"):
            return os.environ["GOOGLE_OAUTH_ACCESS_TOKEN"]
        if self._token[0] > time.time() + 60:
            return self._token[1]
        resp = self._http.get(self.METADATA, headers={"Metadata-Flavor": "Google"})
        resp.raise_for_status()
        body = resp.json()
        self._token = (time.time() + int(body.get("expires_in", 300)), body["access_token"])
        return self._token[1]

    def get(self, name: str) -> Optional[str]:
        url = (f"https://secretmanager.googleapis.com/v1/projects/{self.project}/secrets/"
               f"{self.prefix}{name}/versions/latest:access")
        resp = self._http.get(url, headers={"Authorization": f"Bearer {self._bearer()}"})
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return base64.b64decode(resp.json()["payload"]["data"]).decode()


class AzureKeyVaultBackend:
    """``NAME`` is read from ``<vault_url>/secrets/<prefix><name>``; Key Vault names allow only letters,
    digits and dashes, so ``_`` becomes ``-`` (``FINANCE_PG_PASSWORD`` → ``FINANCE-PG-PASSWORD``)."""

    IMDS = "http://169.254.169.254/metadata/identity/oauth2/token"
    SCOPE = "https://vault.azure.net"

    def __init__(self, vault_url: str, prefix: str = "", tenant_id: Optional[str] = None,
                 client_id: Optional[str] = None, client_secret: Optional[str] = None, timeout: float = 10.0,
                 http: Optional[httpx.Client] = None):
        self.vault_url = vault_url.rstrip("/")
        self.prefix = prefix
        self.tenant_id = tenant_id or os.environ.get("AZURE_TENANT_ID")
        self.client_id = client_id or os.environ.get("AZURE_CLIENT_ID")
        self.client_secret = client_secret or os.environ.get("AZURE_CLIENT_SECRET")
        self._http = http or httpx.Client(timeout=timeout)
        self._token: tuple[float, str] = (0.0, "")

    def _bearer(self) -> str:
        if self._token[0] > time.time() + 60:
            return self._token[1]
        federated = os.environ.get("AZURE_FEDERATED_TOKEN_FILE")
        if self.tenant_id and self.client_id and (self.client_secret or federated):
            data = {"grant_type": "client_credentials", "client_id": self.client_id, "scope": f"{self.SCOPE}/.default"}
            if self.client_secret:
                data["client_secret"] = self.client_secret
            else:
                with open(federated) as fh:                  # workload identity federation (AKS)
                    data["client_assertion"] = fh.read().strip()
                data["client_assertion_type"] = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
            resp = self._http.post(f"https://login.microsoftonline.com/{self.tenant_id}/oauth2/v2.0/token", data=data)
        else:                                                # a managed identity
            params = {"api-version": "2018-02-01", "resource": self.SCOPE}
            if self.client_id:
                params["client_id"] = self.client_id
            resp = self._http.get(self.IMDS, params=params, headers={"Metadata": "true"})
        resp.raise_for_status()
        body = resp.json()
        self._token = (time.time() + int(body.get("expires_in", 300)), body["access_token"])
        return self._token[1]

    def get(self, name: str) -> Optional[str]:
        key = (self.prefix + name).replace("_", "-")
        resp = self._http.get(f"{self.vault_url}/secrets/{key}", params={"api-version": "7.4"},
                              headers={"Authorization": f"Bearer {self._bearer()}"})
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json().get("value")


class SecretResolver:
    def __init__(self, backends: list[SecretBackend]):
        self.backends = backends

    @classmethod
    def from_config(cls, cfg: list[dict]) -> "SecretResolver":
        backends: list[SecretBackend] = []
        for b in cfg or [{"type": "env"}]:
            t = b.get("type")
            if t == "env":
                backends.append(EnvBackend(b.get("prefix", "")))
            elif t == "directory":
                backends.append(DirectoryBackend(b.get("path", "/var/run/secrets/cairn")))
            elif t == "vault":
                backends.append(VaultBackend(b["addr"], b.get("token"), b.get("mount", "secret"), b.get("path", "cairn"),
                                             b.get("token_file")))
            elif t == "aws_secrets_manager":
                backends.append(AwsSecretsManagerBackend(b.get("prefix", "cairn/"), b.get("region")))
            elif t == "gcp_secret_manager":
                backends.append(GcpSecretManagerBackend(b["project"], b.get("prefix", ""), b.get("token_file")))
            elif t == "azure_key_vault":
                backends.append(AzureKeyVaultBackend(b["vault_url"], b.get("prefix", ""), b.get("tenant_id"),
                                                     b.get("client_id"), b.get("client_secret")))
            else:
                raise ValueError(f"unknown secret backend {t!r}")
        return cls(backends)

    def lookup(self, name: str) -> str:
        for backend in self.backends:
            value = backend.get(name)
            if value is not None:
                return value
        raise SecretNotFound(name)

    def resolve(self, obj: Any, seen: Optional[set] = None) -> Any:
        """Replace ``${NAME}`` references; every value used is added to ``seen`` (for log scrubbing)."""
        if isinstance(obj, str):
            def sub(m):
                value = self.lookup(m.group(1))
                if seen is not None:
                    seen.add(value)
                return value
            return _REF.sub(sub, obj)
        if isinstance(obj, list):
            return [self.resolve(v, seen) for v in obj]
        if isinstance(obj, dict):
            return {k: self.resolve(v, seen) for k, v in obj.items()}
        return obj
