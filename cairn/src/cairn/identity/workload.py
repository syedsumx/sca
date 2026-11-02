"""Workload identity: pipelines sign in to Cairn with the identity their platform already gives them.

A dbt job, an Airflow task or a Spark driver in Kubernetes has a service-account token the cluster
signs and rotates (a projected token, for the audience you choose); a CI job has its OIDC token. Cairn
exchanges such a token for a short-lived Cairn token of the pipeline user an administrator bound it
to, so no personal access token has to be stored, copied or rotated.

* **Issuers** (``CAIRN_WORKLOAD_ISSUERS``, JSON): which identity providers Cairn trusts, each with its
  ``issuer`` (the ``iss`` claim, exactly), the ``audience`` its tokens must be for (e.g. ``cairn``;
  never a provider's default audience), and its signing keys (``jwks_url``, over HTTPS unless on
  loopback, with ``ca_file`` if needed; or ``jwks_file``). ``max_token_lifetime`` (24 hours) refuses
  long-lived tokens such as Kubernetes' legacy secret tokens, which never expire. ``token_ttl`` is how
  long the Cairn token lasts (an hour).
* **Bindings** (``/v1/workload-identity/bindings``, ``MANAGE_SYSTEM_OPERATIONS``): an issuer and a
  token subject (e.g. ``system:serviceaccount:pipelines:svc-dbt``, or a CI ``sub``), optionally with
  claims that must match too (e.g. ``{"ref": "refs/heads/main"}``), map to one Cairn user. Changes are
  audited with the change, in one transaction.
* **The exchange** (``POST /v1/auth/workload-token``, OAuth 2.0 token exchange, RFC 8693): the token's
  signature, issuer, audience, expiry and lifetime are checked; exactly one binding must match; the
  user must not be suspended. The Cairn token (audience ``cairn``) then works wherever a personal
  access token does, as that user. Each exchange is audited. Anything else is refused.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urlparse

import httpx
from sqlalchemy import BigInteger, Column, String, Table, Text, UniqueConstraint, select

from cairn.common.db import metadata

logger = logging.getLogger(__name__)

AUDIENCE = "cairn"
TOKEN_EXCHANGE = "urn:ietf:params:oauth:grant-type:token-exchange"
SUBJECT_TYPES = ("urn:ietf:params:oauth:token-type:jwt", "urn:ietf:params:oauth:token-type:id_token",
                 "urn:ietf:params:oauth:token-type:access_token")
ALGORITHMS = ("RS256", "RS384", "RS512", "ES256", "ES384", "PS256")
JWKS_TTL = 600
NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")

workload_bindings = Table(
    "cairn_workload_bindings", metadata,
    Column("id", String(32), primary_key=True),
    Column("issuer", String(64), nullable=False),       # an issuer's name in CAIRN_WORKLOAD_ISSUERS
    Column("subject", String(512), nullable=False),     # the token's sub, exactly
    Column("conditions", Text, nullable=False),         # JSON {claim path: value}, all must match
    Column("user", String(256), nullable=False),        # urn:li:corpuser:...
    Column("created_by", String(256), nullable=False),
    Column("created_ms", BigInteger, nullable=False),
    UniqueConstraint("issuer", "subject", "conditions", name="uq_cairn_workload_binding"),
)


class WorkloadRefused(Exception):
    """The workload token is not exchanged; ``error`` is the OAuth error code."""

    def __init__(self, message: str, error: str = "invalid_grant", status: int = 400):
        super().__init__(message)
        self.error, self.status = error, status


@dataclass
class Issuer:
    name: str
    issuer: str
    audience: str
    jwks_url: str = ""
    jwks_file: str = ""
    ca_file: str = ""
    algorithms: tuple[str, ...] = ("RS256", "ES256")
    max_token_lifetime: int = 86400
    token_ttl: int = 3600
    _keys: dict = field(default_factory=dict, repr=False)
    _fetched: float = field(default=0.0, repr=False)


def parse_issuers(raw: str) -> dict[str, Issuer]:
    """``CAIRN_WORKLOAD_ISSUERS``: a JSON list of issuers (or ``@/path`` to a file holding it)."""
    raw = raw.strip()
    if not raw:
        return {}
    if raw.startswith("@"):
        raw = Path(raw[1:]).read_text()
    try:
        items = json.loads(raw)
    except ValueError as e:
        raise ValueError(f"CAIRN_WORKLOAD_ISSUERS is not JSON: {e}")
    if not isinstance(items, list):
        raise ValueError("CAIRN_WORKLOAD_ISSUERS must be a list of issuers")
    out: dict[str, Issuer] = {}
    for it in items:
        if not isinstance(it, dict):
            raise ValueError("each workload issuer is an object")
        name = str(it.get("name", ""))
        if not NAME.match(name) or name in out:
            raise ValueError(f"workload issuer name {name!r}: lowercase letters, digits and dashes, unique")
        iss, aud = str(it.get("issuer", "")), str(it.get("audience", ""))
        if not iss or not aud:
            raise ValueError(f"workload issuer {name}: issuer and audience are required")
        if aud in ("kubernetes", "https://kubernetes.default.svc", "https://kubernetes.default.svc.cluster.local"):
            raise ValueError(f"workload issuer {name}: use an audience of its own (e.g. 'cairn'), not the cluster's")
        url, path = str(it.get("jwks_url", "")), str(it.get("jwks_file", ""))
        if bool(url) == bool(path):
            raise ValueError(f"workload issuer {name}: give one of jwks_url or jwks_file")
        if url:
            u = urlparse(url)
            if u.scheme != "https" and u.hostname not in ("127.0.0.1", "localhost", "::1"):
                raise ValueError(f"workload issuer {name}: jwks_url must use HTTPS")
        algs = tuple(it.get("algorithms") or ("RS256", "ES256"))
        if not algs or any(a not in ALGORITHMS for a in algs):
            raise ValueError(f"workload issuer {name}: algorithms must be among {', '.join(ALGORITHMS)}")
        life, ttl = int(it.get("max_token_lifetime", 86400)), int(it.get("token_ttl", 3600))
        if not 60 <= life <= 7 * 86400 or not 300 <= ttl <= 43200:
            raise ValueError(f"workload issuer {name}: max_token_lifetime 60 s-7 days, token_ttl 300-43200 s")
        out[name] = Issuer(name, iss, aud, url, path, str(it.get("ca_file", "")), algs, life, ttl)
    return out


def _claim(claims: dict, path: str) -> Any:
    """A claim by dotted path; ``kubernetes.io`` style keys with dots are looked up whole first."""
    if path in claims:
        return claims[path]
    cur: Any = claims
    parts = path.split(".")
    i = 0
    while i < len(parts):
        if not isinstance(cur, dict):
            return None
        for j in range(len(parts), i, -1):  # the longest key that exists (keys may contain dots)
            key = ".".join(parts[i:j])
            if key in cur:
                cur, i = cur[key], j
                break
        else:
            return None
    return cur


class WorkloadIdentity:
    def __init__(self, issuers: dict[str, Issuer], engine, tokens, audit=None,
                 user_status: Optional[Callable[[str], str]] = None, http: Optional[httpx.Client] = None):
        self.issuers, self.engine, self.tokens, self.audit = issuers, engine, tokens, audit
        self.user_status = user_status
        self._http = http
        self._lock = threading.Lock()

    # ---- the identity providers' keys ----------------------------------------------------------
    def _load_keys(self, iss: Issuer) -> dict:
        import jwt

        if iss.jwks_file:
            doc = json.loads(Path(iss.jwks_file).read_text())
        else:
            import ssl

            verify = ssl.create_default_context(cafile=iss.ca_file) if iss.ca_file else True
            client = self._http or httpx.Client(timeout=10, verify=verify)  # proxies from the environment
            try:
                r = client.get(iss.jwks_url)
                r.raise_for_status()
                doc = r.json()
            finally:
                if self._http is None:
                    client.close()
        keys = {}
        for k in doc.get("keys") or []:
            try:
                keys[k.get("kid", "")] = jwt.PyJWK(k)
            except Exception as e:  # noqa: BLE001 - one unusable key does not take down the others
                logger.warning("workload issuer %s: key %s skipped: %s", iss.name, k.get("kid"), e)
        return keys

    def _key(self, iss: Issuer, kid: str):
        with self._lock:
            fresh = time.time() - iss._fetched < JWKS_TTL
            if fresh and kid in iss._keys:
                return iss._keys[kid]
            if fresh and time.time() - iss._fetched < 30:  # an unknown kid refetches, at most every 30 s
                return None
        try:
            keys = self._load_keys(iss)
        except Exception as e:  # noqa: BLE001 - fail closed
            logger.warning("workload issuer %s: keys unavailable: %s", iss.name, e)
            raise WorkloadRefused(f"the keys of issuer {iss.name} are unavailable", "temporarily_unavailable", 503)
        with self._lock:
            iss._keys, iss._fetched = keys, time.time()
        return keys.get(kid)

    # ---- verifying a workload token ----------------------------------------------------------
    def verify(self, token: str) -> tuple[Issuer, dict]:
        import jwt

        try:
            header = jwt.get_unverified_header(token)
            unverified = jwt.decode(token, options={"verify_signature": False})
        except jwt.PyJWTError:
            raise WorkloadRefused("not a JWT")
        iss = next((i for i in self.issuers.values() if i.issuer == unverified.get("iss")), None)
        if iss is None:
            raise WorkloadRefused("the token's issuer is not trusted")
        if header.get("alg") not in iss.algorithms:
            raise WorkloadRefused(f"issuer {iss.name} tokens must be signed with {', '.join(iss.algorithms)}")
        key = self._key(iss, header.get("kid", ""))
        if key is None:
            raise WorkloadRefused("the token's key is not one the issuer publishes")
        try:
            claims = jwt.decode(token, key, algorithms=list(iss.algorithms), audience=iss.audience, issuer=iss.issuer,
                                leeway=30, options={"require": ["exp", "iat", "sub", "iss", "aud"]})
        except jwt.PyJWTError as e:
            raise WorkloadRefused(f"the token is not valid: {e}")
        if claims["exp"] - claims["iat"] > iss.max_token_lifetime:
            raise WorkloadRefused(f"the token lives longer than issuer {iss.name} allows ({iss.max_token_lifetime} s)")
        return iss, claims

    # ---- bindings ----------------------------------------------------------------------------
    def bindings(self, issuer: Optional[str] = None) -> list[dict]:
        q = select(workload_bindings).order_by(workload_bindings.c.issuer, workload_bindings.c.subject)
        if issuer:
            q = q.where(workload_bindings.c.issuer == issuer)
        with self.engine.connect() as conn:
            return [{**dict(r._mapping), "conditions": json.loads(r.conditions)} for r in conn.execute(q)]

    def bind(self, issuer: str, subject: str, user: str, actor: str, conditions: Optional[dict] = None) -> dict:
        if issuer not in self.issuers:
            raise ValueError(f"no workload issuer named {issuer!r}")
        if not subject or len(subject) > 512 or subject.strip() != subject:
            raise ValueError("subject: the token's sub, exactly")
        user = user if user.startswith("urn:li:corpuser:") else f"urn:li:corpuser:{user}"
        if user == "urn:li:corpuser:":
            raise ValueError("name a Cairn user")
        conditions = conditions or {}
        if not isinstance(conditions, dict) or any(not isinstance(v, (str, int, bool)) for v in conditions.values()):
            raise ValueError("conditions: claims to match, as {claim: value}")
        row = {"id": uuid.uuid4().hex, "issuer": issuer, "subject": subject, "user": user,
               "conditions": json.dumps(conditions, sort_keys=True), "created_by": actor,
               "created_ms": int(time.time() * 1000)}
        from sqlalchemy.exc import IntegrityError

        try:
            with self.engine.begin() as conn:
                conn.execute(workload_bindings.insert().values(**row))
                if self.audit is not None:
                    self.audit.record("identity", "workload.binding_created", actor, user,
                                      {"binding": row["id"], "issuer": issuer, "subject": subject,
                                       "conditions": conditions}, conn=conn)
        except IntegrityError:
            raise ValueError("this issuer, subject and conditions are already bound")
        return {**row, "conditions": conditions}

    def unbind(self, binding_id: str, actor: str) -> bool:
        with self.engine.begin() as conn:
            row = conn.execute(select(workload_bindings).where(workload_bindings.c.id == binding_id)).first()
            if row is None:
                return False
            conn.execute(workload_bindings.delete().where(workload_bindings.c.id == binding_id))
            if self.audit is not None:
                self.audit.record("identity", "workload.binding_deleted", actor, row.user,
                                  {"binding": binding_id, "issuer": row.issuer, "subject": row.subject}, conn=conn)
        return True

    # ---- the exchange ------------------------------------------------------------------------
    def exchange(self, subject_token: str) -> dict:
        iss, claims = self.verify(subject_token)
        matches = [b for b in self.bindings(iss.name) if b["subject"] == claims["sub"]
                   and all(str(_claim(claims, k)) == str(v) for k, v in b["conditions"].items())]
        users = {b["user"] for b in matches}
        if not users:
            raise WorkloadRefused(f"no Cairn user is bound to {claims['sub']} of issuer {iss.name}", "access_denied", 403)
        if len(users) > 1:
            raise WorkloadRefused("several Cairn users are bound to this workload; fix the bindings", "access_denied", 403)
        user = users.pop()
        try:
            status = self.user_status(user) if self.user_status else "ACTIVE"
        except Exception as e:  # noqa: BLE001 - fail closed
            logger.warning("workload identity: cannot check %s: %s", user, e)
            raise WorkloadRefused("Cairn cannot check the user right now", "temporarily_unavailable", 503)
        if (status or "ACTIVE").upper() == "SUSPENDED":
            raise WorkloadRefused("the bound user is suspended", "access_denied", 403)
        out = self.tokens.mint(user, iss.token_ttl, audience=AUDIENCE,
                               extra={"via": "workload", "wid": iss.name, "wsub": claims["sub"]})
        if self.audit is not None:
            self.audit.record("identity", "workload.token_issued", user, None,
                              {"issuer": iss.name, "subject": claims["sub"], "binding": matches[0]["id"],
                               "token_id": out["token_id"], "expires_at": out["expires_at"]})
        return out

    def principal_of(self, token: str) -> Optional[str]:
        """The Cairn user of a Cairn workload token; None if it is not one Cairn issued (the caller then
        tries other credentials); ValueError if it is Cairn's but not valid any more."""
        import jwt

        try:
            header = jwt.get_unverified_header(token)
            claims = jwt.decode(token, options={"verify_signature": False})
        except jwt.PyJWTError:
            return None
        if claims.get("aud") != AUDIENCE or claims.get("via") != "workload" or claims.get("iss") != self.tokens.issuer:
            return None
        _, kid = self.tokens._load()
        if header.get("kid") != kid:
            return None
        verified = self.tokens.verify(token, AUDIENCE, leeway=0)  # ValueError when expired, forged, ...
        return f"urn:li:corpuser:{verified['sub']}"


def from_env(engine, tokens, audit, user_status) -> Optional[WorkloadIdentity]:
    issuers = parse_issuers(os.environ.get("CAIRN_WORKLOAD_ISSUERS", ""))
    if not issuers:
        return None
    return WorkloadIdentity(issuers, engine, tokens, audit, user_status)


def build_workload_router(wi: WorkloadIdentity, current_principal, authorize):
    from fastapi import APIRouter, Body, Depends, Form, HTTPException
    from fastapi.responses import JSONResponse

    router = APIRouter(tags=["workload identity"])

    def admin(p):
        if authorize is None:
            raise HTTPException(403, "managing workload identity needs MANAGE_SYSTEM_OPERATIONS")
        authorize(p, "MANAGE_SYSTEM_OPERATIONS", None)

    @router.post("/v1/auth/workload-token")
    def workload_token(grant_type: str = Form(""), subject_token: str = Form(""), subject_token_type: str = Form(""),
                       requested_token_type: str = Form(""), audience: str = Form("")):
        """OAuth 2.0 token exchange (RFC 8693): a workload's token (Kubernetes service account, CI OIDC)
        for a Cairn token of the pipeline user it is bound to."""
        def oauth_error(code: str, text: str, status: int = 400):
            return JSONResponse({"error": code, "error_description": text}, status_code=status,
                                headers={"Cache-Control": "no-store"})

        if grant_type != TOKEN_EXCHANGE:
            return oauth_error("unsupported_grant_type", "workload tokens are exchanged by token exchange")
        if subject_token_type not in SUBJECT_TYPES or not subject_token:
            return oauth_error("invalid_request", "subject_token must be the workload's JWT")
        if requested_token_type not in ("", "urn:ietf:params:oauth:token-type:access_token") or audience not in ("", AUDIENCE):
            return oauth_error("invalid_target", "only Cairn access tokens are issued")
        try:
            out = wi.exchange(subject_token)
        except WorkloadRefused as e:
            logger.info("workload token refused: %s", e)
            return oauth_error(e.error, str(e), e.status)
        return JSONResponse({"access_token": out["token"], "issued_token_type": "urn:ietf:params:oauth:token-type:access_token",
                             "token_type": "Bearer", "expires_in": out["expires_in"], "user": out["user"]},
                            headers={"Cache-Control": "no-store"})

    @router.get("/v1/workload-identity/issuers")
    def issuers(p=Depends(current_principal)):
        admin(p)
        return [{"name": i.name, "issuer": i.issuer, "audience": i.audience, "jwks": i.jwks_url or i.jwks_file,
                 "algorithms": list(i.algorithms), "max_token_lifetime": i.max_token_lifetime, "token_ttl": i.token_ttl}
                for i in wi.issuers.values()]

    @router.get("/v1/workload-identity/bindings")
    def list_bindings(issuer: Optional[str] = None, p=Depends(current_principal)):
        admin(p)
        return wi.bindings(issuer)

    @router.post("/v1/workload-identity/bindings", status_code=201)
    def create_binding(issuer: str = Body(...), subject: str = Body(...), user: str = Body(...),
                       conditions: Optional[dict] = Body(None), p=Depends(current_principal)):
        admin(p)
        try:
            return wi.bind(issuer, subject, user, p.urn, conditions)
        except ValueError as e:
            raise HTTPException(422, str(e))

    @router.delete("/v1/workload-identity/bindings/{binding_id}", status_code=204)
    def delete_binding(binding_id: str, p=Depends(current_principal)):
        admin(p)
        if not wi.unbind(binding_id, p.urn):
            raise HTTPException(404, "no such binding")

    return router
