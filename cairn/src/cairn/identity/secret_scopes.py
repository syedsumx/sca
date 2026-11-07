"""Secret scopes: API keys, passwords and tokens that people's notebooks, jobs and pipelines read as the person.

A **scope** (``/v1/secret-scopes``) holds secrets and an access list: people or groups with ``READ`` (read values),
``WRITE`` (also add, change and remove secrets) or ``MANAGE`` (also change the access list and delete the scope).
Whoever creates a scope manages it. An operator (``MANAGE_SYSTEM_OPERATIONS``) may manage any scope, but reads a
value only after granting themselves ``READ``, which is audited like any change of the list.

* **Values are encrypted** (AES-256-GCM, each bound to its scope and key) with ``CAIRN_SECRETS_KEY`` (32 random
  bytes, base64; ``_FILE`` works). For a rotation, the new key goes in ``CAIRN_SECRETS_KEY`` and the old one in
  ``CAIRN_SECRETS_KEY_PREVIOUS``; ``POST /v1/secret-scopes/rotate`` (an operator) re-encrypts every value with the
  new key. Without a key the feature is off (503), never unencrypted.
* **Values are never listed, logged or audited.** Listing a scope gives keys, who changed them and when.
  ``GET …/secrets/<key>`` gives the value to a reader (``Cache-Control: no-store``) and is audited as
  ``secret.read`` (who, which scope and key).
* From a notebook or job: ``cairn_lakehouse.secret("scope", "key")`` (the person's own token, CAIRN_URL).
"""

from __future__ import annotations

import base64
import os
import re
import time
from typing import Any, Callable, Optional

from sqlalchemy import BigInteger, Column, PrimaryKeyConstraint, String, Table, Text, and_, delete, func, insert, select, update

from starlette.requests import Request  # module level: FastAPI resolves the handlers' annotations here

from cairn.common.db import metadata

LEVELS = {"READ": 1, "WRITE": 2, "MANAGE": 3}
NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
PRINCIPAL = re.compile(r"^urn:li:(corpuser|corpGroup):[^\s]{1,250}$")
MAX_VALUE = 128 * 1024
MAX_SECRETS = 1000

secret_scopes = Table(
    "cairn_secret_scopes", metadata,
    Column("name", String(128), primary_key=True),
    Column("owner", String(256), nullable=False),
    Column("description", Text, nullable=False, default=""),
    Column("created_ms", BigInteger, nullable=False),
)
secret_acls = Table(
    "cairn_secret_acls", metadata,
    Column("scope", String(128), nullable=False),
    Column("principal", String(256), nullable=False),
    Column("permission", String(8), nullable=False),
    PrimaryKeyConstraint("scope", "principal"),
)
secrets_table = Table(
    "cairn_secrets", metadata,
    Column("scope", String(128), nullable=False),
    Column("key", String(128), nullable=False),
    Column("ciphertext", Text, nullable=False),            # base64(nonce | AES-GCM ciphertext and tag)
    Column("key_id", String(16), nullable=False),          # which master key encrypted it
    Column("updated_by", String(256), nullable=False),
    Column("updated_ms", BigInteger, nullable=False),
    PrimaryKeyConstraint("scope", "key"),
)


class SecretError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _now() -> int:
    return int(time.time() * 1000)


def _master(name: str) -> Optional[bytes]:
    raw = os.environ.get(name, "").strip()
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
    import hashlib

    return hashlib.sha256(b"cairn-secrets" + key).hexdigest()[:16]


class SecretScopes:
    def __init__(self, engine, groups_of: Callable[[str], list[str]], is_operator: Callable[[str], bool], audit=None):
        self.engine, self.groups_of, self.is_operator, self.audit = engine, groups_of, is_operator, audit
        self.key = _master("CAIRN_SECRETS_KEY")
        self.previous = _master("CAIRN_SECRETS_KEY_PREVIOUS")
        self.step_up = None      # (principal, request, action): a passkey check (cairn.identity.passkeys)

    @property
    def enabled(self) -> bool:
        return self.key is not None

    def _need_key(self) -> bytes:
        if self.key is None:
            raise SecretError("secret scopes are not configured: set CAIRN_SECRETS_KEY (32 random bytes, base64)", 503)
        return self.key

    # ---- encryption -----------------------------------------------------------------------------
    @staticmethod
    def _aad(scope: str, key: str) -> bytes:
        return f"cairn-secret|{scope}|{key}".encode()

    def _seal(self, scope: str, key: str, value: str) -> tuple[str, str]:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        master = self._need_key()
        nonce = os.urandom(12)
        ct = AESGCM(master).encrypt(nonce, value.encode(), self._aad(scope, key))
        return base64.b64encode(nonce + ct).decode(), _key_id(master)

    def _open(self, scope: str, key: str, ciphertext: str, key_id: str) -> str:
        from cryptography.exceptions import InvalidTag
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        self._need_key()
        master = next((k for k in (self.key, self.previous) if k is not None and _key_id(k) == key_id), None)
        if master is None:
            raise SecretError("this secret was encrypted with a key Cairn no longer has", 500)
        raw = base64.b64decode(ciphertext)
        try:
            return AESGCM(master).decrypt(raw[:12], raw[12:], self._aad(scope, key)).decode()
        except InvalidTag:
            raise SecretError("this secret does not decrypt (tampered with, or moved)", 500)

    # ---- access ---------------------------------------------------------------------------------
    def _scope(self, name: str) -> dict:
        with self.engine.connect() as conn:
            r = conn.execute(select(secret_scopes).where(secret_scopes.c.name == name)).first()
        if r is None:
            raise SecretError("no such secret scope", 404)
        return dict(r._mapping)

    def level(self, actor: str, scope: str) -> int:
        """The actor's permission on the scope (0: none), from the list, by them or their groups."""
        me = {actor, *self.groups_of(actor)}
        with self.engine.connect() as conn:
            rows = conn.execute(select(secret_acls.c.principal, secret_acls.c.permission)
                                .where(secret_acls.c.scope == scope)).all()
        return max([LEVELS[p] for who, p in rows if who in me] or [0])

    def _require(self, actor: str, scope: str, need: str, operator_may: bool = False) -> dict:
        s = self._scope(scope)
        if self.level(actor, scope) >= LEVELS[need] or (operator_may and self.is_operator(actor)):
            return s
        # A scope one may not use is as if it did not exist.
        raise SecretError("no such secret scope", 404) if self.level(actor, scope) == 0 else \
            SecretError(f"this needs {need} on the scope", 403)

    # ---- scopes ---------------------------------------------------------------------------------
    def create(self, actor: str, body: dict) -> dict:
        self._need_key()
        name = str((body or {}).get("name") or "")
        if not NAME.match(name):
            raise SecretError("name: letters, digits, . _ - (up to 128)")
        with self.engine.begin() as conn:
            if conn.execute(select(secret_scopes.c.name).where(secret_scopes.c.name == name)).first():
                raise SecretError(f"a scope {name} exists", 409)
            conn.execute(insert(secret_scopes).values(name=name, owner=actor, created_ms=_now(),
                                                      description=str(body.get("description") or "")[:1000]))
            conn.execute(insert(secret_acls).values(scope=name, principal=actor, permission="MANAGE"))
        self._audit(actor, "secret.scope_created", name, {})
        return self.get(actor, name)

    def list(self, actor: str) -> list[dict]:
        with self.engine.connect() as conn:
            names = [r[0] for r in conn.execute(select(secret_scopes.c.name).order_by(secret_scopes.c.name))]
        op = self.is_operator(actor)
        out = []
        for n in names:
            lvl = self.level(actor, n)
            if lvl or op:
                out.append({**self._scope(n), "permission": next((k for k, v in LEVELS.items() if v == lvl), None)})
        return out

    def get(self, actor: str, scope: str) -> dict:
        s = self._require(actor, scope, "READ", operator_may=True)
        with self.engine.connect() as conn:
            acl = [dict(r._mapping) for r in conn.execute(select(secret_acls.c.principal, secret_acls.c.permission)
                                                          .where(secret_acls.c.scope == scope)
                                                          .order_by(secret_acls.c.principal))]
            keys = [{"key": r.key, "updated_by": r.updated_by, "updated_ms": r.updated_ms}
                    for r in conn.execute(select(secrets_table.c.key, secrets_table.c.updated_by, secrets_table.c.updated_ms)
                                          .where(secrets_table.c.scope == scope).order_by(secrets_table.c.key))]
        return {**s, "acl": acl, "secrets": keys}

    def set_acl(self, actor: str, scope: str, principal: str, permission: Optional[str]) -> dict:
        self._require(actor, scope, "MANAGE", operator_may=True)
        if not PRINCIPAL.match(principal or ""):
            raise SecretError("principal: urn:li:corpuser:… or urn:li:corpGroup:…")
        if permission is not None and permission not in LEVELS:
            raise SecretError(f"permission: one of {', '.join(LEVELS)} (or none to remove)")
        with self.engine.begin() as conn:
            conn.execute(delete(secret_acls).where(and_(secret_acls.c.scope == scope, secret_acls.c.principal == principal)))
            if permission:
                conn.execute(insert(secret_acls).values(scope=scope, principal=principal, permission=permission))
            managers = conn.execute(select(secret_acls.c.principal).where(and_(
                secret_acls.c.scope == scope, secret_acls.c.permission == "MANAGE"))).all()
            if not managers:
                raise SecretError("a scope keeps at least one manager", 400)
        self._audit(actor, "secret.acl_changed", scope, {"principal": principal, "permission": permission})
        return self.get(actor, scope)

    def delete_scope(self, actor: str, scope: str) -> None:
        self._require(actor, scope, "MANAGE", operator_may=True)
        with self.engine.begin() as conn:
            conn.execute(delete(secrets_table).where(secrets_table.c.scope == scope))
            conn.execute(delete(secret_acls).where(secret_acls.c.scope == scope))
            conn.execute(delete(secret_scopes).where(secret_scopes.c.name == scope))
        self._audit(actor, "secret.scope_deleted", scope, {})

    # ---- secrets --------------------------------------------------------------------------------
    def put(self, actor: str, scope: str, key: str, value: Any) -> dict:
        self._require(actor, scope, "WRITE")
        if not NAME.match(key or ""):
            raise SecretError("key: letters, digits, . _ - (up to 128)")
        if not isinstance(value, str) or not value or len(value.encode()) > MAX_VALUE:
            raise SecretError(f"value: text, 1 byte to {MAX_VALUE // 1024} KB")
        ct, kid = self._seal(scope, key, value)
        now = _now()
        with self.engine.begin() as conn:
            exists = conn.execute(select(secrets_table.c.key).where(and_(secrets_table.c.scope == scope,
                                                                         secrets_table.c.key == key))).first()
            if exists:
                conn.execute(update(secrets_table).where(and_(secrets_table.c.scope == scope, secrets_table.c.key == key))
                             .values(ciphertext=ct, key_id=kid, updated_by=actor, updated_ms=now))
            else:
                n = conn.execute(select(func.count()).select_from(secrets_table)
                                 .where(secrets_table.c.scope == scope)).scalar_one()
                if n >= MAX_SECRETS:
                    raise SecretError(f"a scope holds at most {MAX_SECRETS} secrets")
                conn.execute(insert(secrets_table).values(scope=scope, key=key, ciphertext=ct, key_id=kid,
                                                          updated_by=actor, updated_ms=now))
        self._audit(actor, "secret.written", scope, {"key": key})
        return {"scope": scope, "key": key, "updated_ms": now}

    def read(self, actor: str, scope: str, key: str) -> str:
        self._require(actor, scope, "READ")
        with self.engine.connect() as conn:
            r = conn.execute(select(secrets_table).where(and_(secrets_table.c.scope == scope,
                                                              secrets_table.c.key == key))).first()
        if r is None:
            raise SecretError("no such secret", 404)
        value = self._open(scope, key, r.ciphertext, r.key_id)
        self._audit(actor, "secret.read", scope, {"key": key})
        return value

    def remove(self, actor: str, scope: str, key: str) -> None:
        self._require(actor, scope, "WRITE")
        with self.engine.begin() as conn:
            if not conn.execute(delete(secrets_table).where(and_(secrets_table.c.scope == scope,
                                                                 secrets_table.c.key == key))).rowcount:
                raise SecretError("no such secret", 404)
        self._audit(actor, "secret.removed", scope, {"key": key})

    def rotate(self, actor: str) -> dict:
        """Re-encrypts every value with CAIRN_SECRETS_KEY (the old one in CAIRN_SECRETS_KEY_PREVIOUS)."""
        if not self.is_operator(actor):
            raise SecretError("rotating the secrets key needs MANAGE_SYSTEM_OPERATIONS", 403)
        current = _key_id(self._need_key())
        n = 0
        with self.engine.connect() as conn:
            rows = conn.execute(select(secrets_table).where(secrets_table.c.key_id != current)).all()
        for r in rows:
            value = self._open(r.scope, r.key, r.ciphertext, r.key_id)
            ct, kid = self._seal(r.scope, r.key, value)
            with self.engine.begin() as conn:      # only if nobody changed it meanwhile (else it is already current)
                n += conn.execute(update(secrets_table).where(and_(
                    secrets_table.c.scope == r.scope, secrets_table.c.key == r.key,
                    secrets_table.c.ciphertext == r.ciphertext)).values(ciphertext=ct, key_id=kid)).rowcount
        self._audit(actor, "secret.key_rotated", None, {"secrets": n})
        return {"reencrypted": n}

    def _audit(self, actor: str, action: str, scope: Optional[str], details: dict) -> None:
        if self.audit is not None:
            self.audit.record("identity", action, actor, f"secret-scope:{scope}" if scope else None, details)


def build_secret_scopes_router(ss: SecretScopes, current_principal) -> Any:
    from fastapi import APIRouter, Body, Depends, HTTPException
    from fastapi.responses import JSONResponse

    router = APIRouter(prefix="/v1/secret-scopes", tags=["identity"])

    def call(fn, p=None):
        # Secrets are read by people, their notebooks and jobs; an extension or an AI agent acting for someone
        # is not given them (it could carry them anywhere).
        if p is not None and p.via in ("extension", "agent"):
            raise HTTPException(403, "secret scopes are not open to extensions or agents")
        try:
            return fn()
        except SecretError as e:
            raise HTTPException(e.status, str(e))

    @router.get("")
    def list_(p=Depends(current_principal)):
        return call(lambda: ss.list(p.urn), p)

    @router.post("", status_code=201)
    def create(body: dict = Body(...), p=Depends(current_principal)):
        return call(lambda: ss.create(p.urn, body), p)

    @router.post("/rotate")
    def rotate(p=Depends(current_principal)):
        return call(lambda: ss.rotate(p.urn), p)

    @router.get("/{scope}")
    def get(scope: str, p=Depends(current_principal)):
        return call(lambda: ss.get(p.urn, scope), p)

    @router.delete("/{scope}", status_code=204)
    def delete_(scope: str, p=Depends(current_principal)):
        call(lambda: ss.delete_scope(p.urn, scope), p)

    @router.put("/{scope}/acl")
    def acl(scope: str, body: dict = Body(...), p=Depends(current_principal)):
        return call(lambda: ss.set_acl(p.urn, scope, str(body.get("principal") or ""), body.get("permission")), p)

    @router.put("/{scope}/secrets/{key}")
    def put(scope: str, key: str, body: dict = Body(...), p=Depends(current_principal)):
        return call(lambda: ss.put(p.urn, scope, key, body.get("value")), p)

    @router.get("/{scope}/secrets/{key}")
    def read(scope: str, key: str, request: Request, p=Depends(current_principal)):
        if ss.step_up is not None:
            from cairn.identity.passkeys import PasskeyError

            try:
                ss.step_up(p, request, "secret.read")
            except PasskeyError as e:
                raise HTTPException(e.status, str(e), headers=e.headers)
        value = call(lambda: ss.read(p.urn, scope, key), p)
        return JSONResponse({"scope": scope, "key": key, "value": value}, headers={"Cache-Control": "no-store"})

    @router.delete("/{scope}/secrets/{key}", status_code=204)
    def remove(scope: str, key: str, p=Depends(current_principal)):
        call(lambda: ss.remove(p.urn, scope, key), p)

    return router
