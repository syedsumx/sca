"""The gateway's keys behind a key management service (``CAIRN_GATEWAY_KEY_KMS``).

The gateway's identity (an Ed25519 signing key and the X25519 key agents seal their batches to) is kept on disk
only wrapped by the operator's key manager, the same ones Cairn Store uses for customer-managed keys:

* ``vault-transit``: HashiCorp Vault's (or OpenBao's) transit engine, ``transit/encrypt/<key>`` and
  ``transit/decrypt/<key>``, with a token read from ``token_file`` on every call;
* ``aws-kms``: AWS KMS (or anything that speaks its API), ``Encrypt`` and ``Decrypt`` signed with SigV4, with the
  wrapped file's purpose as the encryption context (KMS checks it on every decrypt; CloudTrail records it).

The configuration is JSON (or ``@/path`` to a file holding it), shaped like a store keyring entry::

    {"type": "vault-transit", "addr": "https://vault:8200", "key": "cairn-gateway",
     "token_file": "/etc/cairn-gateway-kms/vault-token", "cache_seconds": 300}
    {"type": "aws-kms", "region": "eu-west-1", "key": "alias/cairn-gateway",
     "access_key": "AKIA...", "secret_key_file": "/etc/cairn-gateway-kms/secret", "cache_seconds": 300}

The key material is in the gateway's memory only. Every ``cache_seconds`` it is unwrapped again, so revoking the
gateway's access (or disabling or deleting the key) stops it within that time: requests that need the keys are then
refused (503), never served with keys the key manager no longer vouches for. It fails closed throughout: a wrapped
identity without a configuration, a configuration that does not load, or a key manager that refuses stops the
gateway at start."""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import json
import os
from typing import Optional, Protocol
from urllib.parse import urlsplit

import httpx

CONTEXT = "cairn-gateway-identity"


class KmsError(Exception):
    """The key manager refused, could not be reached, or is configured in a way that cannot work."""


class KeyWrapper(Protocol):
    kind: str
    key: str

    def encrypt(self, plaintext: bytes, context: str) -> str: ...

    def decrypt(self, ciphertext: str, context: str) -> bytes: ...


def _read(path: str, what: str) -> str:
    try:
        with open(path) as fh:
            value = fh.read().strip()
    except OSError as e:
        raise KmsError(f"{what}: {e}") from e
    if not value:
        raise KmsError(f"{what} {path} is empty")
    return value


def _bound(context: str, plaintext: bytes) -> bytes:
    """The context is bound into what is encrypted, so a wrapped value moved to another purpose does not open."""
    return context.encode() + b"\n" + plaintext


def _unbound(context: str, data: bytes) -> bytes:
    head = context.encode() + b"\n"
    if not data.startswith(head):
        raise KmsError("the wrapped value is not for this purpose")
    return data[len(head):]


class VaultTransit:
    kind = "vault-transit"

    def __init__(self, addr: str = "", key: str = "", token_file: str = "", mount: str = "transit", namespace: str = "",
                 ca_file: str = "", timeout: float = 10.0):
        if not (addr and key and token_file):
            raise KmsError("vault-transit needs addr, key and token_file")
        if not addr.startswith(("https://", "http://")):
            raise KmsError("vault-transit addr: an http(s) URL")
        self.addr, self.key, self.token_file = addr.rstrip("/"), key, token_file
        self.mount, self.namespace = mount.strip("/") or "transit", namespace
        self.http = httpx.Client(timeout=timeout, verify=ca_file or True)

    def _call(self, op: str, body: dict) -> dict:
        headers = {"X-Vault-Token": _read(self.token_file, "the Vault token file")}
        if self.namespace:
            headers["X-Vault-Namespace"] = self.namespace
        try:
            r = self.http.post(f"{self.addr}/v1/{self.mount}/{op}/{self.key}", json=body, headers=headers)
        except httpx.HTTPError as e:
            raise KmsError(f"Vault is not reachable: {e}") from e
        if r.status_code != 200:
            try:
                why = "; ".join(r.json().get("errors") or []) or r.text[:300]
            except ValueError:
                why = r.text[:300]
            raise KmsError(f"Vault refused {op} with {self.key}: {r.status_code} {why}")
        return r.json()["data"]

    def encrypt(self, plaintext: bytes, context: str) -> str:
        return self._call("encrypt", {"plaintext": base64.b64encode(_bound(context, plaintext)).decode()})["ciphertext"]

    def decrypt(self, ciphertext: str, context: str) -> bytes:
        return _unbound(context, base64.b64decode(self._call("decrypt", {"ciphertext": ciphertext})["plaintext"]))


class AwsKms:
    kind = "aws-kms"

    def __init__(self, region: str = "", key: str = "", addr: str = "", access_key: str = "", secret_key_file: str = "",
                 session_token_file: str = "", timeout: float = 10.0):
        if not (region and key):
            raise KmsError("aws-kms needs region and key")
        if bool(access_key) != bool(secret_key_file):
            raise KmsError("aws-kms: access_key and secret_key_file go together")
        self.region, self.key = region, key
        self.addr = (addr or f"https://kms.{region}.amazonaws.com").rstrip("/")
        self.access_key, self.secret_key_file, self.session_token_file = access_key, secret_key_file, session_token_file
        self.http = httpx.Client(timeout=timeout)

    def _credentials(self) -> tuple[str, str, str]:
        if self.access_key:
            token = _read(self.session_token_file, "the session token file") if self.session_token_file else ""
            return self.access_key, _read(self.secret_key_file, "the secret key file"), token
        ak, sk = os.environ.get("AWS_ACCESS_KEY_ID", ""), os.environ.get("AWS_SECRET_ACCESS_KEY", "")
        if not (ak and sk):
            raise KmsError("aws-kms: no credentials (access_key and secret_key_file, or the AWS_* environment)")
        return ak, sk, os.environ.get("AWS_SESSION_TOKEN", "")

    def _call(self, target: str, body: dict) -> dict:
        ak, sk, token = self._credentials()
        data = json.dumps(body).encode()
        now = dt.datetime.now(dt.timezone.utc)
        amz_date, date = now.strftime("%Y%m%dT%H%M%SZ"), now.strftime("%Y%m%d")
        headers = {"content-type": "application/x-amz-json-1.1", "host": urlsplit(self.addr).netloc,
                   "x-amz-date": amz_date, "x-amz-target": f"TrentService.{target}"}
        if token:
            headers["x-amz-security-token"] = token
        signed = ";".join(sorted(headers))
        canonical = "\n".join(["POST", "/", "", "".join(f"{k}:{headers[k]}\n" for k in sorted(headers)), signed,
                               hashlib.sha256(data).hexdigest()])
        scope = f"{date}/{self.region}/kms/aws4_request"
        to_sign = f"AWS4-HMAC-SHA256\n{amz_date}\n{scope}\n{hashlib.sha256(canonical.encode()).hexdigest()}"
        k = f"AWS4{sk}".encode()
        for part in (date, self.region, "kms", "aws4_request"):
            k = hmac.new(k, part.encode(), hashlib.sha256).digest()
        sig = hmac.new(k, to_sign.encode(), hashlib.sha256).hexdigest()
        headers["authorization"] = f"AWS4-HMAC-SHA256 Credential={ak}/{scope}, SignedHeaders={signed}, Signature={sig}"
        try:
            r = self.http.post(self.addr + "/", content=data, headers=headers)
        except httpx.HTTPError as e:
            raise KmsError(f"KMS is not reachable: {e}") from e
        if r.status_code != 200:
            try:
                j = r.json()
                why = f"{j.get('__type', '')} {j.get('message') or j.get('Message') or ''}".strip()
            except ValueError:
                why = r.text[:300]
            raise KmsError(f"KMS refused {target} with {self.key}: {r.status_code} {why}")
        return r.json()

    def encrypt(self, plaintext: bytes, context: str) -> str:
        return self._call("Encrypt", {"KeyId": self.key, "Plaintext": base64.b64encode(plaintext).decode(),
                                      "EncryptionContext": {"cairn": context}})["CiphertextBlob"]

    def decrypt(self, ciphertext: str, context: str) -> bytes:
        out = self._call("Decrypt", {"KeyId": self.key, "CiphertextBlob": ciphertext,
                                     "EncryptionContext": {"cairn": context}})
        return base64.b64decode(out["Plaintext"])


def from_config(cfg: dict) -> KeyWrapper:
    if not isinstance(cfg, dict):
        raise KmsError("the KMS configuration is a JSON object")
    kind = cfg.get("type")
    allowed = {"vault-transit": {"type", "addr", "key", "token_file", "mount", "namespace", "ca_file", "cache_seconds"},
               "aws-kms": {"type", "region", "key", "addr", "access_key", "secret_key_file", "session_token_file",
                           "cache_seconds"}}
    if kind not in allowed:
        raise KmsError(f"KMS type: one of {', '.join(sorted(allowed))} (got {kind!r})")
    extra = set(cfg) - allowed[kind]
    if extra:
        raise KmsError(f"{kind}: unknown settings {', '.join(sorted(extra))}")
    args = {k: v for k, v in cfg.items() if k not in ("type", "cache_seconds")}
    if any(not isinstance(v, str) for v in args.values()):
        raise KmsError(f"{kind}: every setting but cache_seconds is a string")
    return VaultTransit(**args) if kind == "vault-transit" else AwsKms(**args)


def from_env() -> tuple[Optional[KeyWrapper], float]:
    """``CAIRN_GATEWAY_KEY_KMS``: the wrapper, and how long unwrapped keys stay in memory (``cache_seconds``, 300)."""
    raw = os.environ.get("CAIRN_GATEWAY_KEY_KMS", "").strip()
    if not raw:
        return None, 0.0
    if raw.startswith("@"):
        raw = _read(raw[1:], "CAIRN_GATEWAY_KEY_KMS")
    try:
        cfg = json.loads(raw)
    except ValueError as e:
        raise KmsError(f"CAIRN_GATEWAY_KEY_KMS is not JSON: {e}") from e
    cache = cfg.get("cache_seconds", 300) if isinstance(cfg, dict) else 300
    if isinstance(cache, bool) or not isinstance(cache, (int, float)) or not 0 <= cache <= 86_400:
        raise KmsError("cache_seconds: 0 to 86,400")
    return from_config(cfg), float(cache)
