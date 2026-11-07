"""Reversible tokenisation: sensitive values replaced by tokens where data lands, and given back only to people
allowed to, for a stated purpose, audited.

A **token domain** (``POST /governance/tokens/domains``, ``MANAGE_POLICIES``) is a kind of value (``email``,
``iban``, ``patient_id``) with its own keys. Pipes and streams tokenise columns as the data lands
(``"tokenize": {"email": "email"}``: column to domain), so the lakehouse holds tokens, never the values, and every
engine, share, export and copy carries only tokens.

* **Tokens are deterministic and keyed:** ``tok_`` and 26 base32 characters of HMAC-SHA256 under the domain's key.
  The same value is always the same token, so joins, counts and group-bys still work. Without the key a token
  cannot be guessed from a candidate value (unlike a plain hash of an e-mail address).
* **The vault** keeps each value encrypted (AES-256-GCM under the domain's own key, bound to the domain and the
  token) so it can be given back. The domain keys are random, kept wrapped by the master key
  (``CAIRN_TOKENS_KEY``, 32 bytes base64; ``_FILE`` works). ``POST /governance/tokens/rotate`` re-wraps them under a
  new master key (the old one in ``CAIRN_TOKENS_KEY_PREVIOUS``): tokens do not change. Without a key the feature is
  off (503); values are never kept unencrypted.
* **Tokenising** through the API (``POST …/domains/<d>/tokenize``) needs ``TOKENIZE`` on the domain
  (``urn:li:tokenDomain:<d>``); pipes and streams tokenise as their owner set them up.
* **Detokenising** (``POST …/domains/<d>/detokenize`` with ``tokens`` and a ``purpose``) needs ``DETOKENIZE`` on the
  domain. Every call is audited with who, the purpose and how many (never values or tokens), and is limited to
  ``CAIRN_TOKENS_MAX_BATCH`` (1,000) tokens. A token the vault does not hold gives null.
* **Forgetting** (``POST …/domains/<d>/forget`` with ``values`` or ``tokens``, ``MANAGE_POLICIES``) deletes their
  entries from the vault: the tokens stay in the tables but can never be turned back into the values (erasure by
  crypto-shredding, for subject requests). Audited with the count.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import time
from typing import Any, Callable, Iterable, Optional

from sqlalchemy import BigInteger, Column, PrimaryKeyConstraint, String, Table, Text, delete, select, update

from cairn.common.db import metadata

DOMAIN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
TOKEN = re.compile(r"^tok_[a-z2-7]{26}$")
MAX_VALUE = 4096

token_domains = Table(
    "cairn_token_domains", metadata,
    Column("name", String(64), primary_key=True),
    Column("description", Text, nullable=False, default=""),
    Column("wrapped_key", Text, nullable=False),        # base64(nonce | AES-GCM(master, hmac key | data key))
    Column("key_id", String(16), nullable=False),       # which master key wrapped it
    Column("created_by", String(256), nullable=False),
    Column("created_ms", BigInteger, nullable=False),
)
token_vault = Table(
    "cairn_token_vault", metadata,
    Column("domain", String(64), nullable=False),
    Column("token", String(32), nullable=False),
    Column("ciphertext", Text, nullable=False),         # base64(nonce | AES-GCM(data key, value)), bound to domain+token
    Column("created_ms", BigInteger, nullable=False),
    PrimaryKeyConstraint("domain", "token"),
)


class TokenError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _now() -> int:
    return int(time.time() * 1000)


def _master(name: str) -> Optional[bytes]:
    raw = os.environ.get(name, "").strip()
    path = os.environ.get(f"{name}_FILE", "").strip()
    if not raw and path:
        with open(path, encoding="utf-8") as f:
            raw = f.read().strip()
    if not raw:
        return None
    try:
        key = base64.b64decode(raw, validate=True)
    except ValueError:
        raise RuntimeError(f"{name} is not base64")
    if len(key) != 32:
        raise RuntimeError(f"{name} must be 32 bytes (base64 of 32 random bytes)")
    return key


def _key_id(key: bytes) -> str:
    return hashlib.sha256(b"cairn-tokens" + key).hexdigest()[:16]


def _value(v: Any) -> Optional[str]:
    """The text a value is tokenised as (None stays None)."""
    if v is None:
        return None
    s = v if isinstance(v, str) else json.dumps(v, default=str) if isinstance(v, (dict, list)) else str(v)
    if len(s.encode()) > MAX_VALUE:
        raise TokenError(f"a value to tokenise is over {MAX_VALUE} bytes")
    return s


class Tokens:
    def __init__(self, engine, audit=None, authorize: Optional[Callable[[str, str, str], bool]] = None):
        """``authorize(actor, privilege, urn)``: the PDP's decision (fail closed when absent)."""
        self.engine, self.audit, self.authorize = engine, audit, authorize
        self.key = _master("CAIRN_TOKENS_KEY")
        self.previous = _master("CAIRN_TOKENS_KEY_PREVIOUS")
        self.max_batch = int(os.environ.get("CAIRN_TOKENS_MAX_BATCH", "1000"))
        self._keys: dict[str, tuple[bytes, bytes]] = {}

    @property
    def enabled(self) -> bool:
        return self.key is not None

    def _need_key(self) -> bytes:
        if self.key is None:
            raise TokenError("tokenisation is not configured: set CAIRN_TOKENS_KEY (32 random bytes, base64)", 503)
        return self.key

    @staticmethod
    def urn(domain: str) -> str:
        return f"urn:li:tokenDomain:{domain}"

    def _may(self, actor: str, privilege: str, domain: str) -> None:
        ok = False
        if self.authorize is not None:
            try:
                ok = bool(self.authorize(actor, privilege, self.urn(domain)))
            except Exception:  # noqa: BLE001 - fail closed
                ok = False
        if not ok:
            raise TokenError(f"{privilege} on token domain {domain} is needed", 403)

    def _audit(self, action: str, actor: str, details: dict, conn=None) -> None:
        if self.audit is not None:
            self.audit.record("tokens", action, actor, None, details, conn=conn)

    # ---- keys -------------------------------------------------------------------------------------
    @staticmethod
    def _aesgcm(key: bytes):
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        return AESGCM(key)

    def _wrap(self, name: str, keys: bytes, master: bytes) -> tuple[str, str]:
        nonce = os.urandom(12)
        ct = self._aesgcm(master).encrypt(nonce, keys, f"cairn-token-domain|{name}".encode())
        return base64.b64encode(nonce + ct).decode(), _key_id(master)

    def _unwrap(self, name: str, wrapped: str, key_id: str) -> bytes:
        from cryptography.exceptions import InvalidTag

        self._need_key()
        master = next((k for k in (self.key, self.previous) if k is not None and _key_id(k) == key_id), None)
        if master is None:
            raise TokenError(f"token domain {name} is wrapped with a key Cairn no longer has", 500)
        raw = base64.b64decode(wrapped)
        try:
            return self._aesgcm(master).decrypt(raw[:12], raw[12:], f"cairn-token-domain|{name}".encode())
        except InvalidTag:
            raise TokenError(f"token domain {name}'s keys do not decrypt (tampered with, or moved)", 500)

    def _domain_keys(self, name: str) -> tuple[bytes, bytes]:
        if name in self._keys:
            return self._keys[name]
        with self.engine.connect() as conn:
            r = conn.execute(select(token_domains).where(token_domains.c.name == name)).first()
        if r is None:
            raise TokenError(f"no token domain {name}", 404)
        keys = self._unwrap(name, r.wrapped_key, r.key_id)
        self._keys[name] = (keys[:32], keys[32:])
        return self._keys[name]

    # ---- domains ----------------------------------------------------------------------------------
    def create_domain(self, actor: str, name: str, description: str = "") -> dict:
        master = self._need_key()
        if not DOMAIN.match(name or ""):
            raise TokenError("name: lowercase letters, digits and _ (a letter first)")
        wrapped, kid = self._wrap(name, os.urandom(64), master)
        from sqlalchemy.exc import IntegrityError

        row = {"name": name, "description": str(description or "")[:2000], "wrapped_key": wrapped, "key_id": kid,
               "created_by": actor, "created_ms": _now()}
        try:
            with self.engine.begin() as conn:
                conn.execute(token_domains.insert().values(**row))
                self._audit("token.domain_created", actor, {"domain": name}, conn)
        except IntegrityError:
            raise TokenError(f"a token domain {name} exists", 409)
        return self._public(row)

    @staticmethod
    def _public(r) -> dict:
        m = r if isinstance(r, dict) else dict(r._mapping)
        return {k: m[k] for k in ("name", "description", "created_by", "created_ms")}

    def domains(self) -> list[dict]:
        with self.engine.connect() as conn:
            rows = conn.execute(select(token_domains).order_by(token_domains.c.name)).all()
        return [self._public(r) for r in rows]

    def rotate(self, actor: str) -> dict:
        """Re-wraps every domain's keys under the current master key (tokens and the vault are unchanged)."""
        master = self._need_key()
        n = 0
        with self.engine.begin() as conn:
            for r in conn.execute(select(token_domains)).all():
                if r.key_id == _key_id(master):
                    continue
                wrapped, kid = self._wrap(r.name, self._unwrap(r.name, r.wrapped_key, r.key_id), master)
                conn.execute(update(token_domains).where(token_domains.c.name == r.name)
                             .values(wrapped_key=wrapped, key_id=kid))
                n += 1
            self._audit("token.keys_rotated", actor, {"domains": n}, conn)
        self._keys.clear()
        return {"rewrapped": n}

    # ---- tokens -----------------------------------------------------------------------------------
    def token_of(self, domain: str, value: str) -> str:
        mac = hmac.new(self._domain_keys(domain)[0], value.encode(), hashlib.sha256).digest()
        return "tok_" + base64.b32encode(mac).decode().lower()[:26]

    def tokenize(self, domain: str, values: Iterable[Any]) -> list[Optional[str]]:
        """Tokens for the values (None for None), keeping each new value in the vault. For pipes and streams
        (their owner set them up); people go through ``tokenize_as``."""
        self._need_key()
        texts = [_value(v) for v in values]
        _, data_key = self._domain_keys(domain)
        tokens = [None if t is None else self.token_of(domain, t) for t in texts]
        new = {tok: t for tok, t in zip(tokens, texts) if tok is not None}
        if not new:
            return tokens
        from sqlalchemy.exc import IntegrityError

        for _ in range(3):                                 # another writer may add the same values at once
            with self.engine.connect() as conn:
                have = set()
                todo = list(new)
                for i in range(0, len(todo), 500):
                    have |= {r.token for r in conn.execute(select(token_vault.c.token).where(
                        token_vault.c.domain == domain, token_vault.c.token.in_(todo[i:i + 500])))}
            missing = [tok for tok in new if tok not in have]
            if not missing:
                return tokens
            aes, now = self._aesgcm(data_key), _now()
            rows = []
            for tok in missing:
                nonce = os.urandom(12)
                ct = aes.encrypt(nonce, new[tok].encode(), f"{domain}|{tok}".encode())
                rows.append({"domain": domain, "token": tok, "ciphertext": base64.b64encode(nonce + ct).decode(),
                             "created_ms": now})
            try:
                with self.engine.begin() as conn:
                    conn.execute(token_vault.insert(), rows)
                return tokens
            except IntegrityError:
                continue
        raise TokenError("the vault kept changing while tokenising; try again", 503)

    def tokenize_as(self, actor: str, domain: str, values: list) -> list[Optional[str]]:
        self._may(actor, "TOKENIZE", domain)
        if not isinstance(values, list) or not 1 <= len(values) <= self.max_batch:
            raise TokenError(f"values: 1 to {self.max_batch}")
        out = self.tokenize(domain, values)
        self._audit("token.tokenized", actor, {"domain": domain, "count": len(values)})
        return out

    def detokenize(self, actor: str, domain: str, tokens: list, purpose: str) -> list[Optional[str]]:
        """The values of the tokens (null for a token the vault does not hold), for a stated purpose."""
        self._need_key()
        self._may(actor, "DETOKENIZE", domain)
        purpose = str(purpose or "").strip()
        if not 8 <= len(purpose) <= 500:
            raise TokenError("purpose: why the values are needed (8 to 500 characters); it is audited")
        if not isinstance(tokens, list) or not 1 <= len(tokens) <= self.max_batch:
            raise TokenError(f"tokens: 1 to {self.max_batch}")
        if not all(isinstance(t, str) and TOKEN.match(t) for t in tokens):
            raise TokenError("tokens: tok_ and 26 base32 characters each")
        from cryptography.exceptions import InvalidTag

        _, data_key = self._domain_keys(domain)
        aes = self._aesgcm(data_key)
        found: dict[str, str] = {}
        wanted = sorted(set(tokens))
        with self.engine.connect() as conn:
            for i in range(0, len(wanted), 500):
                for r in conn.execute(select(token_vault).where(token_vault.c.domain == domain,
                                                                token_vault.c.token.in_(wanted[i:i + 500]))):
                    raw = base64.b64decode(r.ciphertext)
                    try:
                        found[r.token] = aes.decrypt(raw[:12], raw[12:], f"{domain}|{r.token}".encode()).decode()
                    except InvalidTag:
                        raise TokenError("a vault entry does not decrypt (tampered with, or moved)", 500)
        self._audit("token.detokenized", actor, {"domain": domain, "purpose": purpose, "count": len(tokens),
                                                 "found": sum(1 for t in tokens if t in found)})
        return [found.get(t) for t in tokens]

    def forget(self, actor: str, domain: str, values: Optional[list] = None, tokens: Optional[list] = None) -> dict:
        """Deletes the vault entries of values or tokens: their tokens can never be given back again."""
        self._need_key()
        if bool(values) == bool(tokens):
            raise TokenError("give values or tokens (one of them)")
        items = values or tokens
        if not isinstance(items, list) or len(items) > self.max_batch:
            raise TokenError(f"at most {self.max_batch} at a time")
        if values:
            gone = sorted({self.token_of(domain, t) for t in (_value(v) for v in values) if t is not None})
        else:
            if not all(isinstance(t, str) and TOKEN.match(t) for t in tokens):
                raise TokenError("tokens: tok_ and 26 base32 characters each")
            self._domain_keys(domain)
            gone = sorted(set(tokens))
        with self.engine.begin() as conn:
            n = conn.execute(delete(token_vault).where(token_vault.c.domain == domain,
                                                       token_vault.c.token.in_(gone))).rowcount
            self._audit("token.forgotten", actor, {"domain": domain, "count": n}, conn)
        return {"forgotten": n}

    # ---- columns (pipes and streams) ----------------------------------------------------------------
    def check_columns(self, spec: Any) -> dict:
        """A pipe's or stream's ``tokenize``: {column: domain}, each domain existing."""
        if spec in (None, {}):
            return {}
        if not isinstance(spec, dict) or not all(isinstance(c, str) and isinstance(d, str) for c, d in spec.items()):
            raise TokenError("tokenize: {column: token domain}")
        self._need_key()
        for d in set(spec.values()):
            self._domain_keys(d)
        return dict(sorted(spec.items()))

    def tokenize_table(self, data, spec: dict):
        """An Arrow table with the columns in ``spec`` replaced by their tokens (as text)."""
        import pyarrow as pa

        for column, domain in spec.items():
            if column not in data.column_names:
                continue
            tokens = self.tokenize(domain, data.column(column).to_pylist())
            i = data.column_names.index(column)
            data = data.set_column(i, pa.field(column, pa.string()), pa.array(tokens, pa.string()))
        return data

    def tokenize_rows(self, rows: list[dict], spec: dict) -> list[dict]:
        for column, domain in spec.items():
            at = [i for i, r in enumerate(rows) if column in r]
            if at:
                tokens = self.tokenize(domain, [rows[i][column] for i in at])
                for i, t in zip(at, tokens):
                    rows[i] = {**rows[i], column: t}
        return rows


def build_tokens_router(tokens: Tokens, current_principal, authorize) -> Any:
    from fastapi import APIRouter, Body, Depends, HTTPException
    from fastapi.responses import JSONResponse

    router = APIRouter(prefix="/governance/tokens", tags=["governance"])

    def call(fn, *a, **kw):
        try:
            return fn(*a, **kw)
        except TokenError as e:
            raise HTTPException(e.status, str(e))

    @router.get("/domains")
    def domains(p=Depends(current_principal)):
        return {"domains": call(tokens.domains)}

    @router.post("/domains", status_code=201)
    def create(name: str = Body(...), description: str = Body(""), p=Depends(current_principal)):
        authorize(p, "MANAGE_POLICIES", None)
        return call(tokens.create_domain, p.urn, name, description)

    @router.post("/domains/{domain}/tokenize")
    def tokenize(domain: str, values: list = Body(..., embed=True), p=Depends(current_principal)):
        return {"tokens": call(tokens.tokenize_as, p.urn, domain, values)}

    @router.post("/domains/{domain}/detokenize")
    def detokenize(domain: str, tokens_: list = Body(..., alias="tokens"),
                   purpose: str = Body(...), p=Depends(current_principal)):
        values = call(tokens.detokenize, p.urn, domain, tokens_, purpose)
        return JSONResponse({"values": values}, headers={"Cache-Control": "no-store"})

    @router.post("/domains/{domain}/forget")
    def forget(domain: str, values: Optional[list] = Body(None), tokens_: Optional[list] = Body(None, alias="tokens"),
               p=Depends(current_principal)):
        authorize(p, "MANAGE_POLICIES", None)
        return call(tokens.forget, p.urn, domain, values, tokens_)

    @router.post("/rotate")
    def rotate(p=Depends(current_principal)):
        authorize(p, "MANAGE_POLICIES", None)
        return call(tokens.rotate, p.urn)

    return router
