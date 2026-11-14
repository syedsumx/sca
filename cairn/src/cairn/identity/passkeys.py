"""Passkeys and step-up: sensitive actions need a fresh passkey check, whatever the sign-in was.

People sign in through their identity provider (OIDC or SAML), where multi-factor sign-in is configured. On top of
that, Cairn asks for a **passkey** (WebAuthn: a device's or security key's own key, phishing-resistant) before the
actions that matter most, listed in ``CAIRN_STEP_UP_ACTIONS`` (default: ``detokenize``, ``tokens.forget`` and
``share.create``; ``secret.read`` can be added where secrets are only read interactively, since notebooks and jobs
read them as the person with no one there to touch a key):

* **Passkeys.** ``POST /v1/passkeys/register/options`` gives the browser the options for
  ``navigator.credentials.create``; ``POST /v1/passkeys/register`` with the result keeps the passkey (its public key
  only). Someone who already has a passkey needs a step-up to add another or to remove one, so a stolen session
  cannot swap in the thief's own; the first is audited and the person is told. ``GET /v1/passkeys`` lists one's own.
* **Step-up.** ``POST /v1/passkeys/step-up/options``, then ``POST /v1/passkeys/step-up`` with the assertion
  (``navigator.credentials.get``) gives a step-up token, good for ``CAIRN_STEP_UP_SECONDS`` (300) and for that person
  only. A listed action reads it from the ``X-Cairn-Step-Up`` header; without a valid one it is refused (401,
  ``WWW-Authenticate: step-up``).
* Someone with no passkey yet is refused a listed action (register one first), unless
  ``CAIRN_STEP_UP_ALLOW_UNENROLLED=true`` (for a roll-out).
* Settings: ``CAIRN_PASSKEY_RP_ID`` (the site, e.g. ``cairn.example.com``) and ``CAIRN_PASSKEY_ORIGIN``
  (``https://cairn.example.com``); without them passkeys and step-up are off. Challenges last 5 minutes and are used
  once; signature counters that go backwards (a cloned authenticator) are refused. Only hashes of step-up tokens
  are kept; every registration, removal and step-up is audited.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import time
from typing import Any, Callable, Optional

from sqlalchemy import BigInteger, Column, Integer, String, Table, Text, delete, func, select, update
from starlette.requests import Request  # module level: FastAPI resolves the handlers' annotations here

from cairn.common.db import metadata

passkeys_table = Table(
    "cairn_passkeys", metadata,
    Column("credential_id", String(512), primary_key=True),     # base64url
    Column("user", String(512), nullable=False, index=True),
    Column("public_key", Text, nullable=False),                  # COSE key, base64
    Column("sign_count", BigInteger, nullable=False),
    Column("name", String(200), nullable=False),
    Column("created_ms", BigInteger, nullable=False),
    Column("last_used_ms", BigInteger, nullable=True),
)
passkey_challenges = Table(
    "cairn_passkey_challenges", metadata,
    Column("id", String(64), primary_key=True),
    Column("user", String(512), nullable=False),
    Column("kind", String(16), nullable=False),                  # register | step_up
    Column("challenge", String(128), nullable=False),            # base64url
    Column("expires_ms", BigInteger, nullable=False),
)
step_ups = Table(
    "cairn_step_ups", metadata,
    Column("token_hash", String(64), primary_key=True),
    Column("user", String(512), nullable=False),
    Column("credential_id", String(512), nullable=False),
    Column("expires_ms", BigInteger, nullable=False, index=True),
    Column("uses", Integer, nullable=False, default=0),
)

DEFAULT_ACTIONS = "detokenize,tokens.forget,share.create"
CHALLENGE_MS = 300_000


class PasskeyError(Exception):
    def __init__(self, message: str, status: int = 400, headers: Optional[dict] = None):
        super().__init__(message)
        self.status, self.headers = status, headers


def _now() -> int:
    return int(time.time() * 1000)


def _b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _unb64url(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


class Passkeys:
    def __init__(self, engine, audit=None, notify: Optional[Callable[[str, str], None]] = None, clock=_now):
        self.engine, self.audit, self.notify, self.clock = engine, audit, notify, clock
        self.rp_id = os.environ.get("CAIRN_PASSKEY_RP_ID", "").strip()
        self.origin = os.environ.get("CAIRN_PASSKEY_ORIGIN", "").strip()
        self.actions = {a.strip() for a in os.environ.get("CAIRN_STEP_UP_ACTIONS", DEFAULT_ACTIONS).split(",") if a.strip()}
        self.seconds = int(os.environ.get("CAIRN_STEP_UP_SECONDS", "300"))
        self.allow_unenrolled = os.environ.get("CAIRN_STEP_UP_ALLOW_UNENROLLED", "").lower() == "true"

    @property
    def enabled(self) -> bool:
        return bool(self.rp_id and self.origin)

    def _need(self) -> None:
        if not self.enabled:
            raise PasskeyError("passkeys are not configured (CAIRN_PASSKEY_RP_ID, CAIRN_PASSKEY_ORIGIN)", 503)

    def _audit(self, action: str, user: str, details: dict, conn=None) -> None:
        if self.audit is not None:
            self.audit.record("identity", action, user, None, details, conn=conn)

    # ---- passkeys -------------------------------------------------------------------------------
    def list(self, user: str) -> list[dict]:
        with self.engine.connect() as conn:
            rows = conn.execute(select(passkeys_table).where(passkeys_table.c.user == user)
                                .order_by(passkeys_table.c.created_ms)).all()
        return [{"id": r.credential_id, "name": r.name, "created_ms": r.created_ms, "last_used_ms": r.last_used_ms}
                for r in rows]

    def _count(self, user: str) -> int:
        with self.engine.connect() as conn:
            return conn.execute(select(func.count()).select_from(passkeys_table).where(passkeys_table.c.user == user)).scalar() or 0

    def _challenge(self, user: str, kind: str) -> tuple[str, bytes]:
        cid, challenge = secrets.token_urlsafe(24), secrets.token_bytes(32)
        with self.engine.begin() as conn:
            conn.execute(delete(passkey_challenges).where(passkey_challenges.c.expires_ms < self.clock()))
            conn.execute(passkey_challenges.insert().values(id=cid, user=user, kind=kind, challenge=_b64url(challenge),
                                                            expires_ms=self.clock() + CHALLENGE_MS))
        return cid, challenge

    def _take_challenge(self, cid: str, user: str, kind: str) -> bytes:
        with self.engine.begin() as conn:
            r = conn.execute(select(passkey_challenges).where(passkey_challenges.c.id == cid)).first()
            conn.execute(delete(passkey_challenges).where(passkey_challenges.c.id == cid))   # used once
        if r is None or r.user != user or r.kind != kind or r.expires_ms < self.clock():
            raise PasskeyError("the challenge is unknown, used or expired: start again", 400)
        return _unb64url(r.challenge)

    def register_options(self, user: str, step_up: Optional[str] = None) -> dict:
        import webauthn
        from webauthn.helpers import options_to_json
        from webauthn.helpers.structs import (AuthenticatorSelectionCriteria, PublicKeyCredentialDescriptor,
                                              ResidentKeyRequirement, UserVerificationRequirement)

        self._need()
        if self._count(user):
            self.check(user, step_up, "passkey.add")            # adding to existing passkeys needs one of them
        cid, challenge = self._challenge(user, "register")
        opts = webauthn.generate_registration_options(
            rp_id=self.rp_id, rp_name="Cairn", user_id=hashlib.sha256(user.encode()).digest()[:16], user_name=user,
            challenge=challenge, exclude_credentials=[PublicKeyCredentialDescriptor(id=_unb64url(p["id"])) for p in self.list(user)],
            authenticator_selection=AuthenticatorSelectionCriteria(resident_key=ResidentKeyRequirement.PREFERRED,
                                                                   user_verification=UserVerificationRequirement.REQUIRED))
        return {"challenge_id": cid, "options": json.loads(options_to_json(opts))}

    def register(self, user: str, challenge_id: str, credential: dict, name: str = "passkey",
                 step_up: Optional[str] = None) -> dict:
        import webauthn

        self._need()
        first = self._count(user) == 0
        if not first:
            self.check(user, step_up, "passkey.add")
        challenge = self._take_challenge(challenge_id, user, "register")
        try:
            v = webauthn.verify_registration_response(credential=credential, expected_challenge=challenge,
                                                      expected_rp_id=self.rp_id, expected_origin=self.origin,
                                                      require_user_verification=True)
        except Exception as e:  # noqa: BLE001 - the library's own reasons
            raise PasskeyError(f"the passkey was not accepted: {e}", 400)
        cred = _b64url(v.credential_id)
        from sqlalchemy.exc import IntegrityError

        try:
            with self.engine.begin() as conn:
                conn.execute(passkeys_table.insert().values(
                    credential_id=cred, user=user, public_key=base64.b64encode(v.credential_public_key).decode(),
                    sign_count=v.sign_count, name=str(name or "passkey")[:200], created_ms=self.clock()))
                self._audit("passkey.registered", user, {"passkey": cred, "first": first}, conn)
        except IntegrityError:
            raise PasskeyError("this passkey is registered already", 409)
        if self.notify is not None:
            self.notify(user, f"A passkey named {str(name or 'passkey')[:60]} was added to your Cairn account.")
        return {"id": cred, "name": str(name or "passkey")[:200]}

    def remove(self, user: str, credential_id: str, step_up: Optional[str]) -> None:
        self._need()
        self.check(user, step_up, "passkey.remove")
        with self.engine.begin() as conn:
            if not conn.execute(delete(passkeys_table).where(passkeys_table.c.credential_id == credential_id,
                                                             passkeys_table.c.user == user)).rowcount:
                raise PasskeyError("no such passkey", 404)
            self._audit("passkey.removed", user, {"passkey": credential_id}, conn)

    # ---- step-up ------------------------------------------------------------------------------------
    def step_up_options(self, user: str) -> dict:
        import webauthn
        from webauthn.helpers import options_to_json
        from webauthn.helpers.structs import PublicKeyCredentialDescriptor, UserVerificationRequirement

        self._need()
        mine = self.list(user)
        if not mine:
            raise PasskeyError("register a passkey first (POST /v1/passkeys/register/options)", 409)
        cid, challenge = self._challenge(user, "step_up")
        opts = webauthn.generate_authentication_options(
            rp_id=self.rp_id, challenge=challenge, user_verification=UserVerificationRequirement.REQUIRED,
            allow_credentials=[PublicKeyCredentialDescriptor(id=_unb64url(p["id"])) for p in mine])
        return {"challenge_id": cid, "options": json.loads(options_to_json(opts))}

    def step_up(self, user: str, challenge_id: str, credential: dict) -> dict:
        import webauthn

        self._need()
        challenge = self._take_challenge(challenge_id, user, "step_up")
        cred = str((credential or {}).get("id") or "")
        with self.engine.connect() as conn:
            row = conn.execute(select(passkeys_table).where(passkeys_table.c.credential_id == cred,
                                                            passkeys_table.c.user == user)).first()
        if row is None:
            raise PasskeyError("not one of your passkeys", 401)
        try:
            v = webauthn.verify_authentication_response(
                credential=credential, expected_challenge=challenge, expected_rp_id=self.rp_id, expected_origin=self.origin,
                credential_public_key=base64.b64decode(row.public_key), credential_current_sign_count=row.sign_count,
                require_user_verification=True)
        except Exception as e:  # noqa: BLE001 - a counter that went backwards is refused here too
            self._audit("passkey.step_up_refused", user, {"passkey": cred, "reason": str(e)[:200]})
            raise PasskeyError(f"the passkey check failed: {e}", 401)
        token = secrets.token_urlsafe(32)
        expires = self.clock() + self.seconds * 1000
        with self.engine.begin() as conn:
            conn.execute(update(passkeys_table).where(passkeys_table.c.credential_id == cred)
                         .values(sign_count=v.new_sign_count, last_used_ms=self.clock()))
            conn.execute(delete(step_ups).where(step_ups.c.expires_ms < self.clock()))
            conn.execute(step_ups.insert().values(token_hash=hashlib.sha256(token.encode()).hexdigest(), user=user,
                                                  credential_id=cred, expires_ms=expires, uses=0))
            self._audit("passkey.stepped_up", user, {"passkey": cred, "expires_ms": expires}, conn)
        return {"step_up_token": token, "expires_ms": expires}

    def required(self, action: str) -> bool:
        return self.enabled and action in self.actions

    def check(self, user: str, token: Optional[str], action: str) -> None:
        """Refuses (401, ``WWW-Authenticate: step-up``) unless ``token`` is the user's valid step-up."""
        refuse = {"WWW-Authenticate": 'step-up realm="cairn"'}
        if not token:
            if self.allow_unenrolled and action not in ("passkey.add", "passkey.remove") and self._count(user) == 0:
                return
            raise PasskeyError(f"{action} needs a passkey check: POST /v1/passkeys/step-up/options, then send "
                               "X-Cairn-Step-Up", 401, refuse)
        with self.engine.begin() as conn:
            r = conn.execute(select(step_ups).where(step_ups.c.token_hash == hashlib.sha256(token.encode()).hexdigest())).first()
            if r is None or r.user != user or r.expires_ms < self.clock():
                raise PasskeyError(f"{action}: the step-up is unknown, someone else's or expired", 401, refuse)
            conn.execute(update(step_ups).where(step_ups.c.token_hash == r.token_hash).values(uses=r.uses + 1))
            self._audit("passkey.step_up_used", user, {"action": action}, conn)

    def require(self, principal, request: Optional[Request], action: str) -> None:
        """For routers: refuses a listed action without the caller's step-up. Services (no person) are not asked."""
        if not self.required(action) or getattr(principal, "is_service", False):
            return
        token = request.headers.get("x-cairn-step-up") if request is not None else None
        self.check(principal.urn, token, action)


def build_passkeys_router(pk: Passkeys, current_principal) -> Any:
    from fastapi import APIRouter, Body, Depends, HTTPException

    router = APIRouter(prefix="/v1/passkeys", tags=["identity"])

    def call(fn, *a):
        try:
            return fn(*a)
        except PasskeyError as e:
            raise HTTPException(e.status, str(e), headers=e.headers)

    def person(p):
        if p.is_service:
            raise HTTPException(403, "passkeys are people's")
        return p

    @router.get("")
    def mine(p=Depends(current_principal)):
        return {"passkeys": call(pk.list, person(p).urn), "step_up_actions": sorted(pk.actions) if pk.enabled else []}

    @router.post("/register/options")
    def register_options(request: Request, p=Depends(current_principal)):
        return call(pk.register_options, person(p).urn, request.headers.get("x-cairn-step-up"))

    @router.post("/register", status_code=201)
    def register(request: Request, challenge_id: str = Body(...), credential: dict = Body(...),
                 name: str = Body("passkey"), p=Depends(current_principal)):
        return call(pk.register, person(p).urn, challenge_id, credential, name, request.headers.get("x-cairn-step-up"))

    @router.delete("/{credential_id}", status_code=204)
    def remove(credential_id: str, request: Request, p=Depends(current_principal)):
        call(pk.remove, person(p).urn, credential_id, request.headers.get("x-cairn-step-up"))

    @router.post("/step-up/options")
    def step_up_options(p=Depends(current_principal)):
        return call(pk.step_up_options, person(p).urn)

    @router.post("/step-up")
    def step_up(challenge_id: str = Body(...), credential: dict = Body(...), p=Depends(current_principal)):
        return call(pk.step_up, person(p).urn, challenge_id, credential)

    return router
