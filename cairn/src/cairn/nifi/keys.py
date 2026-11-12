"""A managed NiFi deployment's keys, all derived from Cairn's key (``CAIRN_NIFI_DEPLOY_KEY``) and the deployment, never
stored: NiFi's sensitive properties key (it encrypts sensitive values in NiFi's flow), the single NiFi user Cairn signs
in as (``cairn``; people never get it: they reach NiFi only through Cairn), the keystore password and the TLS keys.

NiFi serves HTTPS only, with a certificate signed by the deployment's own CA (derived here too), which is the only CA
Cairn trusts for that deployment: Cairn pins it, and nobody else holds the key. The files a deployment gets:

* ``nifi.secrets``: the keystore password and the sensitive properties key (mode 0400, NiFi's user), read by
  ``start.sh`` with shell builtins and written into ``nifi.properties``, never the container's environment or a
  command line;
* ``keystore.p12`` (key and certificate) and ``truststore.p12`` (the CA);
* ``login-identity-providers.xml``: NiFi's single-user provider with Cairn's user and a bcrypt hash of its password
  (never the password, so NiFi does not generate one and log it);
* ``start.sh``: the above, then the image's own start script.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
from typing import Optional

USER = "cairn"
NIFI_UID = 1000
NIFI_HOME = "/opt/nifi/nifi-current"
CAIRN_DIR = "/opt/cairn"

START = r"""#!/bin/bash
# Cairn's start for a managed NiFi: its keys come from a file only NiFi's user reads, and go into nifi.properties
# with shell builtins (never the environment or a command line); then the image's own start script.
set -euo pipefail
umask 077
C=/opt/cairn
conf="${NIFI_HOME}/conf"
props="${conf}/nifi.properties"
tls="" skey=""
while IFS='=' read -r k v || [ -n "$k" ]; do
  case "$k" in
    CAIRN_TLS_PASSWORD) tls="$v" ;;
    CAIRN_SENSITIVE_KEY) skey="$v" ;;
  esac
done < "$C/nifi.secrets"
if [ -z "$tls" ] || [ -z "$skey" ]; then echo "cairn: nifi.secrets is incomplete" >&2; exit 1; fi
cp -f "$C/keystore.p12" "$conf/keystore.p12"
cp -f "$C/truststore.p12" "$conf/truststore.p12"
cp -f "$C/login-identity-providers.xml" "$conf/login-identity-providers.xml"
tmp="${props}.cairn"
{
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in
      nifi.security.keystore=*|nifi.security.keystoreType=*|nifi.security.keystorePasswd=*|nifi.security.keyPasswd=*) ;;
      nifi.security.truststore=*|nifi.security.truststoreType=*|nifi.security.truststorePasswd=*) ;;
      nifi.sensitive.props.key=*|nifi.sensitive.props.algorithm=*) ;;
      nifi.security.user.login.identity.provider=*|nifi.security.user.authorizer=*) ;;
      nifi.web.http.port=*|nifi.web.http.host=*) ;;
      *) printf '%s\n' "$line" ;;
    esac
  done < "$props"
  printf '%s\n' "nifi.security.keystore=./conf/keystore.p12" "nifi.security.keystoreType=PKCS12" \
    "nifi.security.keystorePasswd=${tls}" "nifi.security.keyPasswd=${tls}" \
    "nifi.security.truststore=./conf/truststore.p12" "nifi.security.truststoreType=PKCS12" \
    "nifi.security.truststorePasswd=${tls}" "nifi.sensitive.props.key=${skey}" \
    "nifi.sensitive.props.algorithm=NIFI_PBKDF2_AES_GCM_256" \
    "nifi.security.user.login.identity.provider=single-user-provider" \
    "nifi.security.user.authorizer=single-user-authorizer" "nifi.web.http.port=" "nifi.web.http.host="
} > "$tmp"
mv -f "$tmp" "$props"
tls="" skey=""
cd "${NIFI_HOME}"
exec "${NIFI_BASE_DIR:-/opt/nifi}/scripts/start.sh"
"""

LOGIN_PROVIDERS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<!-- Written by Cairn: NiFi's single user is Cairn's; people reach NiFi only through Cairn. -->
<loginIdentityProviders>
    <provider>
        <identifier>single-user-provider</identifier>
        <class>org.apache.nifi.authentication.single.user.SingleUserLoginIdentityProvider</class>
        <property name="Username">{user}</property>
        <property name="Password">{hash}</property>
    </provider>
</loginIdentityProviders>
"""


class Keys:
    def __init__(self, key: bytes):
        if len(key) < 16:
            raise ValueError("managed NiFi needs CAIRN_NIFI_DEPLOY_KEY (16 bytes or more)")
        self.key = key

    def derive(self, purpose: str, name: str, uid: str, n: int = 32) -> bytes:
        return hmac.new(self.key, f"nifi-deploy:{purpose}:{name}:{uid}".encode(), hashlib.sha256).digest()[:n]

    def text(self, purpose: str, name: str, uid: str) -> str:
        return base64.urlsafe_b64encode(self.derive(purpose, name, uid)).decode().rstrip("=")

    def secrets(self, name: str, uid: str) -> dict:
        """The deployment's secrets: Cairn's NiFi password, the keystore password, the sensitive properties key."""
        return {"password": self.text("password", name, uid), "tls_password": self.text("tls", name, uid),
                "sensitive_key": self.text("sensitive", name, uid)}

    # ---- TLS -------------------------------------------------------------------------------------------------------
    def _ec(self, purpose: str, name: str, uid: str):
        from cryptography.hazmat.primitives.asymmetric import ec

        n = int.from_bytes(hmac.new(self.key, f"nifi-deploy:{purpose}:{name}:{uid}".encode(), hashlib.sha512).digest(),
                           "big")
        order = int("FFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551", 16)
        return ec.derive_private_key(n % (order - 1) + 1, ec.SECP256R1())

    def tls(self, name: str, uid: str, hosts: list[str], created: Optional[dt.datetime] = None) -> dict:
        """The deployment's CA (PEM, what Cairn trusts) and NiFi's keystore and truststore (PKCS12, the keystore
        password's). The same deployment always gets the same keys; certificates last ten years from ``created``."""
        import ipaddress

        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.serialization import pkcs12
        from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

        start = (created or dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)) - dt.timedelta(days=1)
        end = start + dt.timedelta(days=3650)
        ca_key, key = self._ec("ca", name, uid), self._ec("server", name, uid)
        ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"cairn-nifi-{name} CA")])
        serial = lambda tag: int.from_bytes(self.derive(f"serial-{tag}", name, uid, 16), "big") >> 2  # noqa: E731
        ca = (x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name).public_key(ca_key.public_key())
              .serial_number(serial("ca")).not_valid_before(start).not_valid_after(end)
              .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
              .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True, crl_sign=True,
                                           content_commitment=False, key_encipherment=False, data_encipherment=False,
                                           key_agreement=False, encipher_only=False, decipher_only=False), critical=True)
              .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
              .sign(ca_key, hashes.SHA256()))
        sans: list = []
        for h in dict.fromkeys(["localhost", "127.0.0.1", *hosts]):
            try:
                sans.append(x509.IPAddress(ipaddress.ip_address(h)))
            except ValueError:
                sans.append(x509.DNSName(h))
        cert = (x509.CertificateBuilder()
                .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"cairn-nifi-{name}")]))
                .issuer_name(ca_name).public_key(key.public_key()).serial_number(serial("server"))
                .not_valid_before(start).not_valid_after(end)
                .add_extension(x509.SubjectAlternativeName(sans), critical=False)
                .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
                .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH, ExtendedKeyUsageOID.CLIENT_AUTH]),
                               critical=False)
                .sign(ca_key, hashes.SHA256()))
        password = self.secrets(name, uid)["tls_password"].encode()
        enc = serialization.BestAvailableEncryption(password)
        keystore = pkcs12.serialize_key_and_certificates(b"nifi-key", key, cert, [ca], enc)
        if hasattr(pkcs12, "serialize_java_truststore"):       # cryptography 45+: entries Java trusts
            truststore = pkcs12.serialize_java_truststore([pkcs12.PKCS12Certificate(ca, b"cairn-ca")], enc)
        else:                                                  # a CA bag (Java reads the store; NiFi needs no entry)
            truststore = pkcs12.serialize_key_and_certificates(None, None, None, [ca], enc)
        return {"ca_pem": ca.public_bytes(serialization.Encoding.PEM), "keystore": keystore, "truststore": truststore,
                "cert_pem": cert.public_bytes(serialization.Encoding.PEM)}

    # ---- NiFi's sign-in --------------------------------------------------------------------------------------------
    def login_providers(self, name: str, uid: str) -> bytes:
        """NiFi's single-user provider with Cairn's user and a bcrypt hash of its password (the salt derived too, so the
        file is the same each time it is written)."""
        import bcrypt

        pw = self.secrets(name, uid)["password"].encode()
        raw = self.derive("bcrypt-salt", name, uid, 16)
        alphabet = b"./ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
        std = base64.b64encode(raw).rstrip(b"=")
        salt = b"$2b$12$" + std.translate(bytes.maketrans(
            b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/", alphabet))[:22]
        # bcrypt's last salt character carries only 2 bits: normalise it so the hash library accepts it.
        salt = salt[:-1] + b"."
        hashed = bcrypt.hashpw(pw, salt).decode()
        return LOGIN_PROVIDERS.format(user=USER, hash=hashed).encode()

    def files(self, name: str, uid: str, hosts: list[str]) -> dict[str, tuple[bytes, int]]:
        """Every file of the deployment (path under /, content, mode), for NiFi's user."""
        s, t = self.secrets(name, uid), self.tls(name, uid, hosts)
        secrets = f"CAIRN_TLS_PASSWORD={s['tls_password']}\nCAIRN_SENSITIVE_KEY={s['sensitive_key']}\n".encode()
        d = CAIRN_DIR.lstrip("/")
        return {f"{d}/nifi.secrets": (secrets, 0o400), f"{d}/keystore.p12": (t["keystore"], 0o400),
                f"{d}/truststore.p12": (t["truststore"], 0o400),
                f"{d}/login-identity-providers.xml": (self.login_providers(name, uid), 0o400),
                f"{d}/start.sh": (START.encode(), 0o500), f"{d}/ca.crt": (t["ca_pem"], 0o444)}
