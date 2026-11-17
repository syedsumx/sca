"""SAML sign-in (cairn.identity.saml) without an identity provider: responses are signed here with a certificate of
the test's own, published in the identity provider's metadata, and posted to the bridge's assertion consumer
service. The end-to-end test against a real Keycloak is tests/test_saml_e2e.py."""

from __future__ import annotations

import base64
import datetime as dt
import uuid
from urllib.parse import parse_qs, urlsplit

import pytest

pytest.importorskip("signxml", reason="needs the saml extra")
pytest.importorskip("lxml", reason="needs the saml extra")

from cryptography import x509  # noqa: E402
from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402
from cryptography.x509.oid import NameOID  # noqa: E402
from lxml import etree  # noqa: E402
from signxml import XMLSigner, methods  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402

from cairn.common.db import metadata  # noqa: E402
from cairn.identity import saml as S  # noqa: E402

IDP = "https://idp.example/realms/acme"
ISSUER = "https://cairn.example/v1/sso"


def _cert():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "idp")])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(1).not_valid_before(now - dt.timedelta(days=1)).not_valid_after(now + dt.timedelta(days=1))
            .sign(key, hashes.SHA256()))
    return (key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()),
            cert.public_bytes(serialization.Encoding.PEM).decode())


KEY, CERT = _cert()
METADATA = (
    f'<md:EntityDescriptor xmlns:md="urn:oasis:names:tc:SAML:2.0:metadata" xmlns:ds="http://www.w3.org/2000/09/xmldsig#" '
    f'entityID="{IDP}"><md:IDPSSODescriptor protocolSupportEnumeration="urn:oasis:names:tc:SAML:2.0:protocol">'
    f'<md:KeyDescriptor use="signing"><ds:KeyInfo><ds:X509Data><ds:X509Certificate>'
    f'{"".join(CERT.strip().splitlines()[1:-1])}</ds:X509Certificate></ds:X509Data></ds:KeyInfo></md:KeyDescriptor>'
    f'<md:SingleSignOnService Binding="{S.REDIRECT_BINDING}" Location="https://idp.example/sso"/>'
    f'</md:IDPSSODescriptor></md:EntityDescriptor>').encode()


class _Signer:
    def mint(self, *a, **k):
        return {"token": "t"}


@pytest.fixture()
def bridge():
    engine = create_engine("sqlite://")
    metadata.create_all(engine, tables=[S.saml_requests, S.saml_assertions, S.sso_codes])
    return S.SamlBridge(engine, _Signer(), ISSUER,
                        [{"client_id": "web", "client_secret": "s", "redirect_uris": ["https://web.example/cb"]}],
                        lambda: METADATA)


def _start(b) -> str:
    url = b.authorize({"client_id": "web", "redirect_uri": "https://web.example/cb", "response_type": "code",
                       "scope": "openid", "state": "st"})
    return parse_qs(urlsplit(url).query)["RelayState"][0]


def _at(seconds: int) -> str:
    return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _signed_response(rid: str, user: str = "jane") -> str:
    """A response holding one assertion for ``user``, the assertion signed by the identity provider."""
    aid = "_a" + uuid.uuid4().hex
    assertion = (
        f'<saml:Assertion xmlns:saml="urn:oasis:names:tc:SAML:2.0:assertion" ID="{aid}" Version="2.0" '
        f'IssueInstant="{_at(0)}"><saml:Issuer>{IDP}</saml:Issuer><saml:Subject><saml:NameID>{user}</saml:NameID>'
        f'<saml:SubjectConfirmation Method="{S.BEARER}"><saml:SubjectConfirmationData InResponseTo="{rid}" '
        f'NotOnOrAfter="{_at(300)}" Recipient="{ISSUER}/saml/acs"/></saml:SubjectConfirmation></saml:Subject>'
        f'<saml:Conditions NotBefore="{_at(-10)}" NotOnOrAfter="{_at(300)}"><saml:AudienceRestriction>'
        f'<saml:Audience>{ISSUER}/saml/metadata</saml:Audience></saml:AudienceRestriction></saml:Conditions>'
        f'<saml:AttributeStatement><saml:Attribute Name="groups"><saml:AttributeValue>finance</saml:AttributeValue>'
        f'</saml:Attribute></saml:AttributeStatement></saml:Assertion>')
    signer = XMLSigner(method=methods.enveloped, signature_algorithm="rsa-sha256", digest_algorithm="sha256",
                       c14n_algorithm="http://www.w3.org/2001/10/xml-exc-c14n#")
    signed = etree.tostring(signer.sign(etree.fromstring(assertion.encode()), key=KEY, cert=CERT,
                                        reference_uri=aid)).decode()
    return (f'<samlp:Response xmlns:samlp="urn:oasis:names:tc:SAML:2.0:protocol" '
            f'xmlns:saml="urn:oasis:names:tc:SAML:2.0:assertion" ID="_r{uuid.uuid4().hex}" Version="2.0" '
            f'IssueInstant="{_at(0)}" Destination="{ISSUER}/saml/acs" InResponseTo="{rid}"><saml:Issuer>{IDP}'
            f'</saml:Issuer><samlp:Status><samlp:StatusCode Value="{S.SUCCESS}"/></samlp:Status>{signed}'
            f'</samlp:Response>')


def _post(b, rid: str, data: bytes) -> str:
    return b.consume(base64.b64encode(data).decode(), rid)


def test_a_signed_assertion_signs_the_person_in(bridge):
    rid = _start(bridge)
    url = _post(bridge, rid, _signed_response(rid).encode())
    assert url.startswith("https://web.example/cb?code=") and "state=st" in url


@pytest.mark.parametrize("user", ["cairn-observability", "cleanroom-q3", "checked-1234", "cairnapp-x", "Checked-1"])
def test_a_name_reserved_by_cairn_is_refused(bridge, user):
    """Cairn's own identities (clean rooms, checked queries, installed apps, observability) are never signed in as:
    an identity provider account of that name is refused, as SCIM and the people API refuse to make one."""
    rid = _start(bridge)
    with pytest.raises(S.SamlError, match="reserved"):
        _post(bridge, rid, _signed_response(rid, user).encode())


def test_a_dtd_is_refused_in_any_encoding(bridge):
    """The DTD check is on the parsed document, not its bytes: a UTF-16 document does not slip a DTD past it."""
    rid = _start(bridge)
    doc = '<?xml version="1.0" encoding="UTF-16"?><!DOCTYPE r [<!ENTITY e "x">]>' + _signed_response(rid)
    with pytest.raises(S.SamlError, match="DTD"):
        _post(bridge, rid, doc.encode("utf-16"))
    md = '<?xml version="1.0" encoding="UTF-16"?><!DOCTYPE md:EntityDescriptor []>' + METADATA.decode()
    with pytest.raises(S.SamlError, match="DTD"):
        S.IdentityProvider.from_metadata(md.encode("utf-16"))
    assert S.IdentityProvider.from_metadata(METADATA).entity_id == IDP



@pytest.mark.parametrize("url,ok", [
    ("https://idp.example.com/realms/x/protocol/saml/descriptor", True),
    ("http://127.0.0.1:8080/realms/x/protocol/saml/descriptor", True),
    ("http://localhost:8080/m", True),
    ("http://[::1]:8080/m", True),
    ("http://idp.example.com/realms/x/protocol/saml/descriptor", False),
    ("http://10.0.0.5/m", False),
    ("ftp://idp.example.com/m", False),
    ("https:///m", False),
])
def test_idp_metadata_is_fetched_only_over_https_or_loopback(url, ok):
    # The metadata carries the IdP's signing certificate: over plain http, anyone on the way could swap it and sign
    # assertions for any user and group.
    from cairn.identity.saml import metadata_url_ok

    assert metadata_url_ok(url) is ok
