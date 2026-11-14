"""SAML single sign-on: Cairn signs people in with a SAML 2.0 identity provider and hands the result on as OIDC.

Cairn Core's sign-in speaks OIDC. For an identity provider set up for SAML (Okta, Entra ID, ADFS, Keycloak,
PingFederate…), Cairn stands in the middle as a broker, the way Dex or Keycloak brokering do:

* **Towards the identity provider, Cairn is a SAML service provider.** ``GET /v1/sso/saml/metadata`` is its
  metadata (entity ID, the assertion consumer service at ``/v1/sso/saml/acs``, HTTP-POST). It sends an
  ``AuthnRequest`` (HTTP-Redirect) and accepts only a response that is for that request and checks out in full:
  the XML signature with the identity provider's certificate (from its metadata), over the response or the one
  assertion it holds, and only what the signature covers is read (no XML signature wrapping); the issuer; the
  destination and recipient (Cairn's ACS); the audience (Cairn's entity ID); the validity window (with
  ``CAIRN_SAML_CLOCK_SKEW_SECONDS``, default 120); ``InResponseTo`` (a request Cairn made, used once); and an
  assertion ID never seen before (replays are refused). Encrypted assertions and documents with a DTD are refused.
* **Towards Cairn Core (or any OIDC client), Cairn is an OpenID provider**: ``/v1/sso/.well-known/openid-configuration``,
  ``/v1/sso/authorize`` (authorization code, ``state``, ``nonce``, PKCE S256), ``/v1/sso/token`` (client secret,
  basic or post; a code works once, for 2 minutes), ``/v1/sso/jwks.json`` and ``/v1/sso/userinfo``. The ID token
  carries ``sub`` (the user), ``email``, ``name``, ``preferred_username`` and ``groups`` (from the assertion's
  attributes), signed with a key of its own (never Trino's).

Settings: ``CAIRN_SSO_ISSUER_URL`` (where browsers and Core reach ``/v1/sso``), ``CAIRN_SSO_SIGNING_PASSPHRASE``
(encrypts the signing key; ``_FILE`` works), ``CAIRN_SSO_CLIENTS_FILE`` (JSON: ``[{"client_id", "client_secret",
"redirect_uris"}]``), ``CAIRN_SAML_IDP_METADATA_URL`` or ``CAIRN_SAML_IDP_METADATA_FILE``, and optionally
``CAIRN_SAML_SP_ENTITY_ID``, ``CAIRN_SAML_USERNAME_ATTRIBUTE`` (else the NameID), ``CAIRN_SAML_EMAIL_ATTRIBUTE``
(``email``), ``CAIRN_SAML_NAME_ATTRIBUTE`` (``name``), ``CAIRN_SAML_GROUPS_ATTRIBUTE`` (``groups``). Every sign-in
and every refusal is audited (``sso.saml_signed_in``, ``sso.saml_refused`` with the reason).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import time
import uuid
import zlib
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import urlencode

from sqlalchemy import BigInteger, Boolean, Column, String, Table, Text, delete, insert, select, update
from sqlalchemy.exc import IntegrityError
from starlette.requests import Request  # at module level: FastAPI must see it as the request (AGENTS.md)

from cairn.common.db import metadata

NS = {"samlp": "urn:oasis:names:tc:SAML:2.0:protocol", "saml": "urn:oasis:names:tc:SAML:2.0:assertion",
      "md": "urn:oasis:names:tc:SAML:2.0:metadata", "ds": "http://www.w3.org/2000/09/xmldsig#"}
SUCCESS = "urn:oasis:names:tc:SAML:2.0:status:Success"
BEARER = "urn:oasis:names:tc:SAML:2.0:cm:bearer"
POST_BINDING = "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST"
REDIRECT_BINDING = "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-Redirect"
MAX_RESPONSE = 512 * 1024
REQUEST_TTL_MS = 10 * 60 * 1000
CODE_TTL_MS = 2 * 60 * 1000
USER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@+-]{0,127}$")

saml_requests = Table(
    "cairn_saml_requests", metadata,
    Column("id", String(64), primary_key=True),            # the AuthnRequest ID (and RelayState)
    Column("client_id", String(128), nullable=False),
    Column("redirect_uri", Text, nullable=False),
    Column("state", Text, nullable=True),
    Column("nonce", Text, nullable=True),
    Column("code_challenge", String(128), nullable=True),
    Column("used", Boolean, nullable=False, default=False),
    Column("created_ms", BigInteger, nullable=False, index=True),
)
saml_assertions = Table(
    "cairn_saml_assertions", metadata,
    Column("id", String(256), primary_key=True),           # assertion IDs already accepted (replays refused)
    Column("expires_ms", BigInteger, nullable=False, index=True),
)
sso_codes = Table(
    "cairn_sso_codes", metadata,
    Column("code_hash", String(64), primary_key=True),
    Column("client_id", String(128), nullable=False),
    Column("redirect_uri", Text, nullable=False),
    Column("claims", Text, nullable=False),
    Column("code_challenge", String(128), nullable=True),
    Column("used", Boolean, nullable=False, default=False),
    Column("expires_ms", BigInteger, nullable=False, index=True),
)


class SamlError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _now_ms() -> int:
    return int(time.time() * 1000)


def _xml(data: bytes):
    """Parses untrusted XML: no DTD, no entities, no network."""
    from lxml import etree

    if b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
        raise SamlError("SAML documents with a DTD are refused")
    parser = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False, dtd_validation=False,
                             huge_tree=False, remove_comments=True)
    try:
        return etree.fromstring(data, parser)
    except etree.XMLSyntaxError as e:
        raise SamlError(f"not a SAML document: {e}")


def _time(text: Optional[str]) -> Optional[float]:
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.strip().replace("Z", "+00:00")).timestamp()
    except ValueError:
        raise SamlError(f"bad time {text!r}")


@dataclass
class IdentityProvider:
    entity_id: str
    sso_url: str
    certs: list[str]                       # PEM

    @classmethod
    def from_metadata(cls, data: bytes) -> "IdentityProvider":
        root = _xml(data)
        ed = root if root.tag == f"{{{NS['md']}}}EntityDescriptor" else root.find("md:EntityDescriptor", NS)
        if ed is None:
            raise SamlError("the identity provider's metadata holds no EntityDescriptor")
        idp = ed.find("md:IDPSSODescriptor", NS)
        if idp is None:
            raise SamlError("the metadata describes no identity provider (IDPSSODescriptor)")
        sso = next((s.get("Location") for s in idp.findall("md:SingleSignOnService", NS)
                    if s.get("Binding") == REDIRECT_BINDING), None)
        if not sso:
            raise SamlError("the identity provider has no HTTP-Redirect SingleSignOnService")
        certs = []
        for kd in idp.findall("md:KeyDescriptor", NS):
            if kd.get("use") not in (None, "signing"):
                continue
            for c in kd.findall(".//ds:X509Certificate", NS):
                b64 = re.sub(r"\s+", "", c.text or "")
                if b64:
                    lines = "\n".join(b64[i:i + 64] for i in range(0, len(b64), 64))
                    certs.append(f"-----BEGIN CERTIFICATE-----\n{lines}\n-----END CERTIFICATE-----\n")
        if not certs:
            raise SamlError("the identity provider's metadata has no signing certificate")
        return cls(ed.get("entityID") or "", sso, certs)


class SamlBridge:
    def __init__(self, engine, signer, issuer: str, clients: list[dict], idp_loader, audit=None,
                 sp_entity_id: Optional[str] = None):
        """``signer``: a :class:`cairn.dataaccess.trino_auth.TrinoTokens` with a key of its own; ``issuer``: the
        public ``…/v1/sso`` URL; ``clients``: the OIDC clients; ``idp_loader()``: the identity provider's metadata
        (bytes)."""
        self.engine, self.signer, self.issuer, self.audit = engine, signer, issuer.rstrip("/"), audit
        self.clients = {c["client_id"]: c for c in clients}
        self.idp_loader = idp_loader
        self.sp_entity_id = sp_entity_id or f"{self.issuer}/saml/metadata"
        self.acs_url = f"{self.issuer}/saml/acs"
        self.skew = float(os.environ.get("CAIRN_SAML_CLOCK_SKEW_SECONDS", "120"))
        self.attr = {k: os.environ.get(f"CAIRN_SAML_{k.upper()}_ATTRIBUTE", d) for k, d in
                     (("username", ""), ("email", "email"), ("name", "name"), ("groups", "groups"))}
        self._idp: Optional[IdentityProvider] = None
        self._idp_at = 0.0

    def idp(self) -> IdentityProvider:
        if self._idp is None or time.time() - self._idp_at > 3600:
            self._idp = IdentityProvider.from_metadata(self.idp_loader())
            self._idp_at = time.time()
        return self._idp

    # ---- OIDC discovery ------------------------------------------------------------------------
    def discovery(self) -> dict:
        return {"issuer": self.issuer, "authorization_endpoint": f"{self.issuer}/authorize",
                "token_endpoint": f"{self.issuer}/token", "jwks_uri": f"{self.issuer}/jwks.json",
                "userinfo_endpoint": f"{self.issuer}/userinfo", "response_types_supported": ["code"],
                "subject_types_supported": ["public"], "id_token_signing_alg_values_supported": ["RS256"],
                "scopes_supported": ["openid", "profile", "email", "groups"],
                "token_endpoint_auth_methods_supported": ["client_secret_basic", "client_secret_post"],
                "code_challenge_methods_supported": ["S256"], "grant_types_supported": ["authorization_code"],
                "claims_supported": ["sub", "email", "name", "preferred_username", "groups", "nonce"]}

    def sp_metadata(self) -> str:
        return (f'<?xml version="1.0"?><md:EntityDescriptor xmlns:md="{NS["md"]}" entityID="{_attr(self.sp_entity_id)}">'
                f'<md:SPSSODescriptor AuthnRequestsSigned="false" WantAssertionsSigned="true" '
                f'protocolSupportEnumeration="urn:oasis:names:tc:SAML:2.0:protocol">'
                f'<md:NameIDFormat>urn:oasis:names:tc:SAML:1.1:nameid-format:unspecified</md:NameIDFormat>'
                f'<md:AssertionConsumerService Binding="{POST_BINDING}" Location="{_attr(self.acs_url)}" index="0" '
                f'isDefault="true"/></md:SPSSODescriptor></md:EntityDescriptor>')

    # ---- authorize: off to the identity provider -----------------------------------------------
    def authorize(self, params: dict) -> str:
        """The identity provider URL to send the browser to (with the AuthnRequest)."""
        client = self.clients.get(str(params.get("client_id") or ""))
        redirect = str(params.get("redirect_uri") or "")
        if client is None:
            raise SamlError("unknown client_id", 400)
        if redirect not in client.get("redirect_uris", []):
            raise SamlError("redirect_uri is not one registered for this client", 400)
        if params.get("response_type") != "code":
            raise SamlError("response_type: code", 400)
        if "openid" not in str(params.get("scope") or "").split():
            raise SamlError("scope must include openid", 400)
        challenge = params.get("code_challenge")
        if challenge and params.get("code_challenge_method", "plain") != "S256":
            raise SamlError("code_challenge_method: S256", 400)
        idp = self.idp()
        rid = "_" + uuid.uuid4().hex
        now = _now_ms()
        with self.engine.begin() as conn:
            conn.execute(delete(saml_requests).where(saml_requests.c.created_ms < now - REQUEST_TTL_MS))
            conn.execute(insert(saml_requests).values(id=rid, client_id=client["client_id"], redirect_uri=redirect,
                                                      state=params.get("state"), nonce=params.get("nonce"),
                                                      code_challenge=challenge, used=False, created_ms=now))
        instant = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        req = (f'<samlp:AuthnRequest xmlns:samlp="{NS["samlp"]}" xmlns:saml="{NS["saml"]}" ID="{rid}" Version="2.0" '
               f'IssueInstant="{instant}" Destination="{_attr(idp.sso_url)}" ProtocolBinding="{POST_BINDING}" '
               f'AssertionConsumerServiceURL="{_attr(self.acs_url)}"><saml:Issuer>{_text(self.sp_entity_id)}</saml:Issuer>'
               f'<samlp:NameIDPolicy AllowCreate="true" Format="urn:oasis:names:tc:SAML:1.1:nameid-format:unspecified"/>'
               f'</samlp:AuthnRequest>')
        comp = zlib.compressobj(9, zlib.DEFLATED, -15)
        encoded = base64.b64encode(comp.compress(req.encode()) + comp.flush()).decode()
        sep = "&" if "?" in idp.sso_url else "?"
        return idp.sso_url + sep + urlencode({"SAMLRequest": encoded, "RelayState": rid})

    # ---- the assertion consumer service --------------------------------------------------------
    def _refuse(self, reason: str, status: int = 403) -> SamlError:
        if self.audit is not None:
            self.audit.record("identity", "sso.saml_refused", "urn:li:corpuser:anonymous", None, {"reason": reason[:300]})
        return SamlError(f"SAML sign-in refused: {reason}", status)

    def _verified_assertion(self, root, data: bytes):
        """The one assertion, as covered by a valid signature of the identity provider."""
        from lxml import etree
        from signxml import XMLVerifier
        from signxml.exceptions import InvalidSignature

        if root.findall(".//saml:EncryptedAssertion", NS):
            raise self._refuse("encrypted assertions are not supported; ask the identity provider to sign, not encrypt")
        assertions = root.findall(".//saml:Assertion", NS)
        if len(assertions) != 1:
            raise self._refuse(f"expected one assertion, found {len(assertions)}")
        signed_response = root.find("ds:Signature", NS) is not None
        target = data if signed_response else etree.tostring(assertions[0])
        last = None
        for cert in self.idp().certs:
            try:
                result = XMLVerifier().verify(target, x509_cert=cert)
            except (InvalidSignature, ValueError) as e:
                last = e
                continue
            signed = result.signed_xml
            if signed_response:
                if signed.tag != f"{{{NS['samlp']}}}Response":
                    raise self._refuse("the signature does not cover the response")
                inner = signed.findall("saml:Assertion", NS)
                if len(inner) != 1:
                    raise self._refuse("the signed response holds no single assertion")
                return signed, inner[0]
            if signed.tag != f"{{{NS['saml']}}}Assertion":
                raise self._refuse("the signature does not cover the assertion")
            return None, signed
        raise self._refuse(f"the signature does not verify with the identity provider's certificate ({last})")

    def consume(self, saml_response: str, relay_state: str) -> str:
        """Checks the identity provider's response; the client's redirect URL (with the code) on success."""
        try:
            data = base64.b64decode(saml_response or "", validate=False)
        except (ValueError, TypeError):
            raise self._refuse("SAMLResponse is not base64", 400)
        if not data or len(data) > MAX_RESPONSE:
            raise self._refuse("SAMLResponse is empty or too large", 400)
        root = _xml(data)
        if root.tag != f"{{{NS['samlp']}}}Response":
            raise self._refuse("not a SAML Response", 400)
        idp = self.idp()
        signed_response, a = self._verified_assertion(root, data)
        resp = signed_response if signed_response is not None else root
        now = time.time()
        # The response: ours, from the identity provider, a success.
        dest = resp.get("Destination")
        if dest and dest != self.acs_url:
            raise self._refuse("the response is for another destination")
        status = resp.find("samlp:Status/samlp:StatusCode", NS)
        if status is None or status.get("Value") != SUCCESS:
            said = status.get("Value") if status is not None else "no status"
            raise self._refuse(f"the identity provider did not sign the person in ({said})")
        rid = resp.get("InResponseTo") or ""
        if not rid or rid != relay_state:
            raise self._refuse("the response is not for a sign-in Cairn started")
        # The assertion: issuer, conditions, subject confirmation.
        issuer = (a.findtext("saml:Issuer", namespaces=NS) or "").strip()
        if issuer != idp.entity_id:
            raise self._refuse("the assertion is from another issuer")
        cond = a.find("saml:Conditions", NS)
        if cond is None:
            raise self._refuse("the assertion has no conditions")
        nb, noa = _time(cond.get("NotBefore")), _time(cond.get("NotOnOrAfter"))
        if (nb is not None and now + self.skew < nb) or noa is None or now - self.skew >= noa:
            raise self._refuse("the assertion is outside its validity window")
        audiences = [x.text.strip() for x in cond.findall("saml:AudienceRestriction/saml:Audience", NS) if x.text]
        if self.sp_entity_id not in audiences:
            raise self._refuse("the assertion is for another service provider")
        subject = a.find("saml:Subject", NS)
        confirmed = False
        for sc in (subject.findall("saml:SubjectConfirmation", NS) if subject is not None else []):
            d = sc.find("saml:SubjectConfirmationData", NS)
            if sc.get("Method") != BEARER or d is None:
                continue
            if d.get("Recipient") != self.acs_url:
                continue
            if d.get("InResponseTo") not in (None, rid):
                continue
            exp = _time(d.get("NotOnOrAfter"))
            if exp is None or now - self.skew >= exp:
                continue
            confirmed = True
        if not confirmed:
            raise self._refuse("no bearer subject confirmation for Cairn's ACS")
        # The request: one Cairn made, recent, used once.
        with self.engine.begin() as conn:
            req = conn.execute(select(saml_requests).where(saml_requests.c.id == rid)).first()
            if req is None or req.used or req.created_ms < _now_ms() - REQUEST_TTL_MS:
                raise self._refuse("the sign-in request is unknown, used or expired")
            if not conn.execute(update(saml_requests).where((saml_requests.c.id == rid) & (saml_requests.c.used.is_(False)))
                                .values(used=True)).rowcount:
                raise self._refuse("the sign-in request was already used")
        aid = a.get("ID") or ""
        try:
            with self.engine.begin() as conn:
                conn.execute(delete(saml_assertions).where(saml_assertions.c.expires_ms < _now_ms()))
                conn.execute(insert(saml_assertions).values(id=aid[:256], expires_ms=int((noa + self.skew) * 1000)))
        except IntegrityError:
            raise self._refuse("the assertion was already used (a replay)")
        # Who: the NameID or an attribute; email, name and groups from attributes.
        attrs: dict[str, list[str]] = {}
        for at in a.findall("saml:AttributeStatement/saml:Attribute", NS):
            vals = [(v.text or "").strip() for v in at.findall("saml:AttributeValue", NS) if (v.text or "").strip()]
            for key in {at.get("Name") or "", at.get("FriendlyName") or ""} - {""}:
                attrs.setdefault(key, []).extend(vals)
        if self.attr["username"]:
            user = (attrs.get(self.attr["username"]) or [""])[0]
        else:
            user = (a.findtext("saml:Subject/saml:NameID", namespaces=NS) or "").strip()
        if not USER.match(user):
            raise self._refuse(f"the assertion names no usable user ({user[:60]!r})")
        groups = sorted({g.strip("/").split("/")[-1] for g in attrs.get(self.attr["groups"], []) if g.strip("/")})
        claims = {"sub": user, "preferred_username": user, "email": (attrs.get(self.attr["email"]) or [None])[0],
                  "name": (attrs.get(self.attr["name"]) or [user])[0], "groups": groups, "nonce": req.nonce,
                  "auth_time": int(now), "amr": ["saml"], "idp": idp.entity_id}
        code = secrets.token_urlsafe(32)
        with self.engine.begin() as conn:
            conn.execute(delete(sso_codes).where(sso_codes.c.expires_ms < _now_ms()))
            conn.execute(insert(sso_codes).values(code_hash=hashlib.sha256(code.encode()).hexdigest(),
                                                  client_id=req.client_id, redirect_uri=req.redirect_uri,
                                                  claims=json.dumps(claims), code_challenge=req.code_challenge,
                                                  used=False, expires_ms=_now_ms() + CODE_TTL_MS))
        if self.audit is not None:
            self.audit.record("identity", "sso.saml_signed_in", f"urn:li:corpuser:{user}", None,
                              {"idp": idp.entity_id, "client": req.client_id, "groups": groups})
        q = {"code": code, **({"state": req.state} if req.state else {})}
        sep = "&" if "?" in req.redirect_uri else "?"
        return req.redirect_uri + sep + urlencode(q)

    # ---- token and userinfo ----------------------------------------------------------------------
    def token(self, form: dict, basic: Optional[tuple[str, str]]) -> dict:
        cid, secret = basic if basic else (str(form.get("client_id") or ""), str(form.get("client_secret") or ""))
        client = self.clients.get(cid)
        if client is None or not hmac.compare_digest(str(client.get("client_secret", "")).encode(), secret.encode()):
            raise SamlError("invalid_client", 401)
        if form.get("grant_type") != "authorization_code":
            raise SamlError("unsupported_grant_type", 400)
        code = str(form.get("code") or "")
        h = hashlib.sha256(code.encode()).hexdigest()
        with self.engine.begin() as conn:
            row = conn.execute(select(sso_codes).where(sso_codes.c.code_hash == h)).first()
            if row is None or row.used or row.expires_ms < _now_ms() or row.client_id != cid:
                raise SamlError("invalid_grant", 400)
            if row.redirect_uri != str(form.get("redirect_uri") or ""):
                raise SamlError("invalid_grant", 400)
            if row.code_challenge:
                verifier = str(form.get("code_verifier") or "")
                calc = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
                if not verifier or not hmac.compare_digest(calc, row.code_challenge):
                    raise SamlError("invalid_grant", 400)
            if not conn.execute(update(sso_codes).where((sso_codes.c.code_hash == h) & (sso_codes.c.used.is_(False)))
                                .values(used=True)).rowcount:
                raise SamlError("invalid_grant", 400)
        claims = json.loads(row.claims)
        actor = f"urn:li:corpuser:{claims['sub']}"
        extra = {k: v for k, v in claims.items() if k not in ("sub", "groups") and v is not None}
        id_token = self.signer.mint(actor, 3600, audience=cid, groups=tuple(claims.get("groups") or ()), extra=extra)
        access = self.signer.mint(actor, 3600, audience=f"{self.issuer}/userinfo",
                                  groups=tuple(claims.get("groups") or ()), extra=extra)
        return {"access_token": access["token"], "id_token": id_token["token"], "token_type": "Bearer",
                "expires_in": 3600, "scope": "openid profile email groups"}

    def userinfo(self, bearer: str) -> dict:
        try:
            c = self.signer.verify(bearer, f"{self.issuer}/userinfo")
        except ValueError as e:
            raise SamlError(f"invalid_token: {e}", 401)
        return {k: c[k] for k in ("sub", "email", "name", "preferred_username", "groups") if k in c}


def _attr(v: str) -> str:
    return v.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;")


def _text(v: str) -> str:
    return v.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def load_clients(path: str) -> list[dict]:
    with open(path) as f:
        clients = json.load(f)
    if not isinstance(clients, list) or not all(isinstance(c, dict) and c.get("client_id") and c.get("client_secret")
                                                and isinstance(c.get("redirect_uris"), list) for c in clients):
        raise ValueError("CAIRN_SSO_CLIENTS_FILE: a list of {client_id, client_secret, redirect_uris}")
    return clients


def build_sso_router(b: SamlBridge) -> Any:
    from fastapi import APIRouter, Form, HTTPException
    from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

    router = APIRouter(prefix="/v1/sso", tags=["identity"])

    def fail(e: SamlError):
        if e.status == 401 and str(e) == "invalid_client":
            return JSONResponse({"error": "invalid_client"}, 401, headers={"WWW-Authenticate": "Basic"})
        if str(e) in ("invalid_grant", "unsupported_grant_type"):
            return JSONResponse({"error": str(e)}, e.status)
        raise HTTPException(e.status, str(e))

    @router.get("/.well-known/openid-configuration")
    def discovery():
        return b.discovery()

    @router.get("/jwks.json")
    def jwks():
        return b.signer.jwks()

    @router.get("/saml/metadata")
    def metadata_():
        return Response(b.sp_metadata(), media_type="application/samlmetadata+xml")

    @router.get("/authorize")
    def authorize(request: Request):
        try:
            return RedirectResponse(b.authorize(dict(request.query_params)), 302)
        except SamlError as e:
            return fail(e)

    @router.post("/saml/acs")
    def acs(SAMLResponse: str = Form(""), RelayState: str = Form("")):  # noqa: N803 - SAML's field names
        try:
            return RedirectResponse(b.consume(SAMLResponse, RelayState), 303)
        except SamlError as e:
            return HTMLResponse(f"<h1>Sign-in refused</h1><p>{_text(str(e))}</p>", e.status)

    @router.post("/token")
    async def token(request: Request):
        form = dict(await request.form())
        basic = None
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("basic "):
            try:
                from urllib.parse import unquote

                cid, _, sec = base64.b64decode(auth[6:]).decode().partition(":")
                basic = (unquote(cid), unquote(sec))
            except (ValueError, UnicodeDecodeError):
                return JSONResponse({"error": "invalid_client"}, 401)
        try:
            return JSONResponse(b.token(form, basic), headers={"Cache-Control": "no-store"})
        except SamlError as e:
            return fail(e)

    @router.get("/userinfo")
    def userinfo(request: Request):
        auth = request.headers.get("authorization", "")
        if not auth.lower().startswith("bearer "):
            raise HTTPException(401, "a bearer token", headers={"WWW-Authenticate": "Bearer"})
        try:
            return b.userinfo(auth[7:])
        except SamlError as e:
            return fail(e)

    return router

