"""A real KMIP server for the store's KMIP tests: PyKMIP's server, TLS 1.2 with client certificates, its
certificates made here (P-256, a test CA). Also the client the tests use as the key's owner."""

import datetime
import ipaddress
import os
import shutil
import socket
import subprocess
import sys
import time

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID


def _write(path, data):
    with open(path, "wb") as f:
        f.write(data)
    return str(path)


def make_pki(d, clients=("cairn-store",)):
    """A CA, a server certificate for 127.0.0.1 and one client certificate per name: {name: (cert, key)}."""
    now = datetime.datetime.now(datetime.timezone.utc)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "cairn test KMIP CA")])
    ca = (x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name).public_key(ca_key.public_key())
          .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(minutes=5))
          .not_valid_after(now + datetime.timedelta(days=1))
          .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
          .add_extension(x509.KeyUsage(False, False, False, False, False, True, True, False, False), critical=True)
          .sign(ca_key, hashes.SHA256()))
    out = {"ca": _write(d / "ca.pem", ca.public_bytes(serialization.Encoding.PEM))}

    def leaf(name, usage, san=None):
        key = ec.generate_private_key(ec.SECP256R1())
        b = (x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)]))
             .issuer_name(ca_name).public_key(key.public_key()).serial_number(x509.random_serial_number())
             .not_valid_before(now - datetime.timedelta(minutes=5)).not_valid_after(now + datetime.timedelta(days=1))
             .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
             .add_extension(x509.ExtendedKeyUsage([usage]), critical=False))
        if san:
            b = b.add_extension(x509.SubjectAlternativeName(san), critical=False)
        cert = b.sign(ca_key, hashes.SHA256())
        c = _write(d / f"{name}.crt", cert.public_bytes(serialization.Encoding.PEM))
        k = _write(d / f"{name}.key", key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                      serialization.NoEncryption()))
        return c, k

    out["server"] = leaf("127.0.0.1", ExtendedKeyUsageOID.SERVER_AUTH,
                         [x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))])
    for name in clients:
        out[name] = leaf(name, ExtendedKeyUsageOID.CLIENT_AUTH)
    return out


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def server_bin():
    """pykmip-server next to this Python, or on the PATH."""
    here = os.path.join(os.path.dirname(sys.executable), "pykmip-server")
    return here if os.path.exists(here) else shutil.which("pykmip-server")


def start_server(d, pki):
    port = _free_port()
    policies = d / "policies"
    policies.mkdir(exist_ok=True)
    conf = d / "server.conf"
    conf.write_text(f"""[server]
hostname=127.0.0.1
port={port}
certificate_path={pki['server'][0]}
key_path={pki['server'][1]}
ca_path={pki['ca']}
auth_suite=TLS1.2
policy_path={policies}
enable_tls_client_auth=True
tls_cipher_suites=ECDHE-ECDSA-AES128-GCM-SHA256
logging_level=INFO
database_path={d / 'kmip.db'}
""")
    log = open(d / "server.log", "w")
    proc = subprocess.Popen([server_bin(), "-f", str(conf), "-l", str(d / "pykmip.log")], stdout=log, stderr=subprocess.STDOUT)
    end = time.time() + 60
    while True:
        try:
            socket.create_connection(("127.0.0.1", port), 0.3).close()
            break
        except OSError:
            if proc.poll() is not None or time.time() > end:
                raise RuntimeError("the KMIP server did not start: " + (d / "server.log").read_text()[-2000:])
            time.sleep(0.3)
    return proc, port


def client(port, pki, who="cairn-store"):
    """PyKMIP's own client, signed in as ``who`` (the key's owner)."""
    from kmip.core import enums
    from kmip.pie.client import ProxyKmipClient

    cert, key = pki[who]
    c = ProxyKmipClient(hostname="127.0.0.1", port=port, cert=cert, key=key, ca=pki["ca"], ssl_version="PROTOCOL_SSLv23",
                        kmip_version=enums.KMIPVersion.KMIP_1_4)
    c.open()
    return c


def make_key(c):
    from kmip.core import enums

    uid = c.create(enums.CryptographicAlgorithm.AES, 256, cryptographic_usage_mask=[
        enums.CryptographicUsageMask.ENCRYPT, enums.CryptographicUsageMask.DECRYPT])
    c.activate(uid)
    return uid
