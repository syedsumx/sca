"""Cryptography for the agent ↔ gateway protocol.

* **Identity / integrity:** Ed25519 request signatures over
  ``METHOD \\n PATH \\n TIMESTAMP \\n NONCE \\n SHA256(BODY)``.
* **Confidentiality (E2E, survives TLS termination):** sealed boxes built from an
  ephemeral X25519 key agreement → HKDF-SHA256 → AES-256-GCM with caller-supplied AAD
  (binds ciphertext to ``agent_id | run_id | seq`` so batches cannot be replayed into
  another run or reordered).
"""

from __future__ import annotations

import base64
import gzip
import io
import hashlib
import json
import os
import secrets
import time
from dataclasses import dataclass
from typing import Optional

from cryptography.exceptions import InvalidSignature, InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

SEAL_INFO = b"aegis-seal-v1"
HEADER_AGENT = "X-Aegis-Agent"
HEADER_TS = "X-Aegis-Timestamp"
HEADER_NONCE = "X-Aegis-Nonce"
HEADER_SIG = "X-Aegis-Signature"


class CryptoError(Exception):
    pass


def b64e(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def b64d(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def _raw_public(key) -> bytes:
    return key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def _raw_private(key) -> bytes:
    return key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                             serialization.NoEncryption())


@dataclass
class Identity:
    """An agent's (or the gateway's) long-lived key material."""

    signing_key: Ed25519PrivateKey
    encryption_key: X25519PrivateKey

    @classmethod
    def generate(cls) -> "Identity":
        return cls(Ed25519PrivateKey.generate(), X25519PrivateKey.generate())

    @property
    def signing_public(self) -> str:
        return b64e(_raw_public(self.signing_key.public_key()))

    @property
    def encryption_public(self) -> str:
        return b64e(_raw_public(self.encryption_key.public_key()))

    def to_dict(self) -> dict:
        return {"signing_key": b64e(_raw_private(self.signing_key)),
                "encryption_key": b64e(_raw_private(self.encryption_key))}

    @classmethod
    def from_dict(cls, d: dict) -> "Identity":
        return cls(Ed25519PrivateKey.from_private_bytes(b64d(d["signing_key"])),
                   X25519PrivateKey.from_private_bytes(b64d(d["encryption_key"])))

    def save(self, path: str, extra: Optional[dict] = None) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        tmp = f"{path}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump({**self.to_dict(), **(extra or {})}, fh)
        os.replace(tmp, path)

    @classmethod
    def load(cls, path: str) -> tuple["Identity", dict]:
        with open(path) as fh:
            d = json.load(fh)
        extra = {k: v for k, v in d.items() if k not in ("signing_key", "encryption_key")}
        return cls.from_dict(d), extra


# --------------------------------------------------------------------------- signatures
def signing_string(method: str, path: str, ts: str, nonce: str, body: bytes) -> bytes:
    return "\n".join([method.upper(), path, ts, nonce, hashlib.sha256(body).hexdigest()]).encode()


def sign_request(identity: Identity, agent_id: str, method: str, path: str, body: bytes) -> dict[str, str]:
    ts = str(int(time.time()))
    nonce = secrets.token_urlsafe(16)
    sig = identity.signing_key.sign(signing_string(method, path, ts, nonce, body))
    return {HEADER_AGENT: agent_id, HEADER_TS: ts, HEADER_NONCE: nonce, HEADER_SIG: b64e(sig)}


def verify_signature(public_key_b64: str, method: str, path: str, ts: str, nonce: str, body: bytes,
                     signature_b64: str) -> None:
    try:
        Ed25519PublicKey.from_public_bytes(b64d(public_key_b64)).verify(
            b64d(signature_b64), signing_string(method, path, ts, nonce, body))
    except (InvalidSignature, ValueError) as e:
        raise CryptoError("invalid request signature") from e


def sign_bytes(identity: Identity, data: bytes) -> str:
    return b64e(identity.signing_key.sign(data))


def verify_bytes(public_key_b64: str, data: bytes, signature_b64: str) -> None:
    try:
        Ed25519PublicKey.from_public_bytes(b64d(public_key_b64)).verify(b64d(signature_b64), data)
    except (InvalidSignature, ValueError) as e:
        raise CryptoError("invalid signature") from e


# --------------------------------------------------------------------------- sealed boxes
def _derive(shared: bytes, epk: bytes, recipient_pub: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=SEAL_INFO + epk + recipient_pub).derive(shared)


def seal(recipient_public_b64: str, plaintext: bytes, aad: bytes, compress: bool = True) -> dict:
    recipient_pub = b64d(recipient_public_b64)
    eph = X25519PrivateKey.generate()
    epk = _raw_public(eph.public_key())
    key = _derive(eph.exchange(X25519PublicKey.from_public_bytes(recipient_pub)), epk, recipient_pub)
    nonce = os.urandom(12)
    data = gzip.compress(plaintext) if compress else plaintext
    ct = AESGCM(key).encrypt(nonce, data, aad)
    return {"v": 1, "epk": b64e(epk), "nonce": b64e(nonce), "ct": b64e(ct), "gz": compress}


def open_sealed(recipient: X25519PrivateKey, sealed: dict, aad: bytes, max_plaintext: int = 64 << 20) -> bytes:
    if sealed.get("v") != 1:
        raise CryptoError("unsupported envelope version")
    try:
        epk = b64d(sealed["epk"])
        recipient_pub = _raw_public(recipient.public_key())
        key = _derive(recipient.exchange(X25519PublicKey.from_public_bytes(epk)), epk, recipient_pub)
        data = AESGCM(key).decrypt(b64d(sealed["nonce"]), b64d(sealed["ct"]), aad)
    except (InvalidTag, KeyError, ValueError) as e:
        raise CryptoError("decryption failed (wrong key, tampered ciphertext or AAD mismatch)") from e
    if sealed.get("gz"):
        # Bounded decompression guards against zip bombs.
        d = gzip.GzipFile(fileobj=io.BytesIO(data))
        out = d.read(max_plaintext + 1)
        if len(out) > max_plaintext:
            raise CryptoError("decompressed payload too large")
        return out
    return data


def aad_for(*parts: str) -> bytes:
    return "|".join(parts).encode()


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()
