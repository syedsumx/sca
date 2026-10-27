"""In-VPC secret resolution for recipes (``${NAME}`` references).

Resolution order is configurable; secrets never leave the agent process. Supported
backends: environment variables, a mounted directory (Kubernetes secrets / CSI),
HashiCorp Vault KV v2, and AWS Secrets Manager.
"""

from __future__ import annotations

import os
import re
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
