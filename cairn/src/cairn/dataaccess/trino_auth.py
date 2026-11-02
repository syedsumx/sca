"""Sign-in to Trino with Cairn: Cairn issues short-lived signed tokens (JWT, RS256) for its users and
publishes the public key (JWKS); Trino checks every token against it (``http-server.authentication.type=JWT``),
so the Trino user is whoever Cairn says it is, not whatever name a client claims.

* One signing key for every API replica, kept in Cairn's database, encrypted with the Trino access key
  (the database alone cannot forge a token).
* ``GET /v1/trino/jwks.json`` (public: it holds only public keys) is Trino's ``jwt.key-file``.
* ``POST /v1/trino/token`` gives the signed-in user a token for BI tools and the Trino CLI; Cairn's SQL
  editor mints one per query for the user it runs as.
"""

from __future__ import annotations

import base64
import json
import threading
import time
import uuid

from sqlalchemy import BigInteger, Column, String, Table, Text, select
from sqlalchemy.exc import IntegrityError

from cairn.common.db import metadata

trino_signing_keys = Table(
    "cairn_trino_signing_keys", metadata,
    Column("name", String(32), primary_key=True),       # "current"
    Column("kid", String(64), nullable=False),
    Column("private_pem", Text, nullable=False),         # PKCS#8, encrypted with the access key
    Column("created_ms", BigInteger, nullable=False),
)

AUDIENCE = "trino"
MAX_TTL = 12 * 3600


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _int_b64(n: int) -> str:
    return _b64(n.to_bytes((n.bit_length() + 7) // 8, "big"))


class TrinoTokens:
    def __init__(self, engine, passphrase: str, issuer: str, ttl: int = 3600):
        if not 60 <= ttl <= MAX_TTL:
            raise ValueError(f"the Trino token lifetime must be between 60 and {MAX_TTL} seconds")
        self.engine, self.issuer, self.ttl = engine, issuer, ttl
        self._pass = passphrase.encode()
        self._lock = threading.Lock()
        self._key = None
        self._kid = ""

    # ---- the key ------------------------------------------------------------------------------
    def _load(self):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa

        with self._lock:
            if self._key is not None:
                return self._key, self._kid
            q = select(trino_signing_keys.c.kid, trino_signing_keys.c.private_pem).where(trino_signing_keys.c.name == "current")
            with self.engine.connect() as conn:
                row = conn.execute(q).first()
            if row is None:
                key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
                pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                        serialization.BestAvailableEncryption(self._pass)).decode()
                kid = uuid.uuid4().hex
                try:
                    with self.engine.begin() as conn:
                        conn.execute(trino_signing_keys.insert().values(name="current", kid=kid, private_pem=pem,
                                                                        created_ms=int(time.time() * 1000)))
                    row = (kid, pem)
                except IntegrityError:  # another replica made it first: use theirs
                    with self.engine.connect() as conn:
                        row = conn.execute(q).first()
            try:
                self._key = serialization.load_pem_private_key(row[1].encode(), self._pass)
            except ValueError:
                raise RuntimeError("the Trino signing key cannot be read with this CAIRN_TRINO_ACCESS_KEY "
                                   "(it was made with another one)")
            self._kid = row[0]
            return self._key, self._kid

    def jwks(self) -> dict:
        key, kid = self._load()
        n = key.public_key().public_numbers()
        return {"keys": [{"kty": "RSA", "use": "sig", "alg": "RS256", "kid": kid, "n": _int_b64(n.n), "e": _int_b64(n.e)}]}

    # ---- tokens -------------------------------------------------------------------------------
    def mint(self, actor: str, ttl: int | None = None, audience: str = AUDIENCE, groups: tuple[str, ...] = (),
             extra: dict | None = None) -> dict:
        """A signed token for ``actor``. ``audience`` names the one service that accepts it (Trino by default;
        another OAuth client's tokens are refused by Trino); ``groups`` go in a ``groups`` claim."""
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding

        user = actor.removeprefix("urn:li:corpuser:")
        if not user or user == actor:
            raise ValueError("Trino tokens are for Cairn users")
        ttl = self.ttl if ttl is None else ttl
        if not 60 <= ttl <= MAX_TTL:
            raise ValueError(f"ttl must be between 60 and {MAX_TTL} seconds")
        key, kid = self._load()
        now = int(time.time())
        header = {"alg": "RS256", "typ": "JWT", "kid": kid}
        claims = {"iss": self.issuer, "aud": audience, "sub": user, "iat": now, "nbf": now - 30, "exp": now + ttl,
                  "jti": uuid.uuid4().hex}
        if groups:
            claims["groups"] = [g.removeprefix("urn:li:corpGroup:") for g in groups]
        for k, v in (extra or {}).items():
            claims.setdefault(k, v)  # never the standard claims
        signing = _b64(json.dumps(header, separators=(",", ":")).encode()) + "." + \
            _b64(json.dumps(claims, separators=(",", ":")).encode())
        sig = key.sign(signing.encode(), padding.PKCS1v15(), hashes.SHA256())
        return {"token": f"{signing}.{_b64(sig)}", "user": user, "expires_in": ttl, "expires_at": now + ttl,
                "token_id": claims["jti"]}

    def verify(self, token: str, audience: str, leeway: int = 30) -> dict:
        """The claims of a token Cairn signed for ``audience``; ValueError if it is not one (signature,
        issuer, audience, expiry, not-before)."""
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding

        try:
            h, c, sig = token.split(".")
            header = json.loads(_unb64(h))
            claims = json.loads(_unb64(c))
            signature = _unb64(sig)
        except (ValueError, TypeError) as e:
            raise ValueError("not a token Cairn issued") from e
        key, kid = self._load()
        if header.get("alg") != "RS256" or header.get("kid") != kid:
            raise ValueError("not signed with Cairn's current key")
        try:
            key.public_key().verify(signature, f"{h}.{c}".encode(), padding.PKCS1v15(), hashes.SHA256())
        except InvalidSignature as e:
            raise ValueError("bad signature") from e
        now = int(time.time())
        if claims.get("iss") != self.issuer:
            raise ValueError("wrong issuer")
        aud = claims.get("aud")
        if audience not in (aud if isinstance(aud, list) else [aud]):
            raise ValueError(f"not a token for {audience}")
        if not isinstance(claims.get("exp"), int) or claims["exp"] + leeway < now:
            raise ValueError("the token has expired")
        if isinstance(claims.get("nbf"), int) and claims["nbf"] - leeway > now:
            raise ValueError("the token is not valid yet")
        if not claims.get("sub"):
            raise ValueError("the token names no user")
        return claims


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))
