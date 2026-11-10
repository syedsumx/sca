"""A managed Airflow deployment's identities and keys, all derived from Cairn's key (``CAIRN_AIRFLOW_DEPLOY_KEY``) and
the deployment, never stored:

* its **pipeline user** (``svc-af-<name>``): what its tasks are in Cairn. Tasks sign in by workload identity: on
  Kubernetes with their pods' projected service-account token (the cluster's issuer, bound by Cairn to the pipeline
  user); on Docker with a token Cairn itself issues as a workload issuer (``cairn-airflow``: ES256, one hour, subject
  ``airflow-deployment:<name>``), written into the containers and renewed every 20 minutes, the way the kubelet
  rotates a projected token. Either is exchanged at ``/v1/auth/workload-token`` like any workload's; removing the
  binding stops the deployment's tasks at their next exchange.
* its **OAuth client** (``airflow-<name>``, audience ``airflow-<name>``): people sign in to its UI through Cairn; a
  token for one deployment is refused by every other.
* its metadata database password, Fernet key, API JWT secret and session secret.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import tempfile
import time
import uuid
from typing import Optional

ISSUER_NAME = "cairn-airflow"
ISSUER = "urn:cairn:airflow-deployments"
AUDIENCE = "cairn"
TOKEN_TTL = 3600


def pipeline_user(name: str) -> str:
    return f"urn:li:corpuser:svc-af-{name}"


def client_id(name: str) -> str:
    return f"airflow-{name}"


def subject(name: str) -> str:
    return f"airflow-deployment:{name}"


class Keys:
    def __init__(self, key: bytes):
        if len(key) < 16:
            raise ValueError("managed Airflow needs CAIRN_AIRFLOW_DEPLOY_KEY (16 bytes or more)")
        self.key = key
        self._signing = None

    def derive(self, purpose: str, name: str, uid: str, n: int = 32) -> bytes:
        return hmac.new(self.key, f"airflow-deploy:{purpose}:{name}:{uid}".encode(), hashlib.sha256).digest()[:n]

    def text(self, purpose: str, name: str, uid: str) -> str:
        return base64.urlsafe_b64encode(self.derive(purpose, name, uid)).decode().rstrip("=")

    def secrets(self, name: str, uid: str) -> dict:
        return {"db_password": self.text("db", name, uid), "jwt_secret": self.text("jwt", name, uid) * 2,
                "api_secret": self.text("api", name, uid),
                "fernet_key": base64.urlsafe_b64encode(self.derive("fernet", name, uid)).decode(),
                "oauth_secret": self.text("oauth", name, uid)}

    # ---- Cairn as the workload issuer of Docker deployments --------------------------------------------------
    def _key(self):
        from cryptography.hazmat.primitives.asymmetric import ec

        if self._signing is None:
            n = int.from_bytes(hmac.new(self.key, b"airflow-deploy:issuer", hashlib.sha512).digest(), "big")
            order = int("FFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551", 16)
            self._signing = ec.derive_private_key(n % (order - 1) + 1, ec.SECP256R1())
        return self._signing

    def kid(self) -> str:
        from cryptography.hazmat.primitives import serialization

        pub = self._key().public_key().public_bytes(serialization.Encoding.DER,
                                                    serialization.PublicFormat.SubjectPublicKeyInfo)
        return hashlib.sha256(pub).hexdigest()[:16]

    def jwks(self) -> dict:
        import jwt

        jwk = json.loads(jwt.algorithms.ECAlgorithm.to_jwk(self._key().public_key()))
        jwk.update({"kid": self.kid(), "use": "sig", "alg": "ES256"})
        return {"keys": [jwk]}

    def workload_token(self, name: str, ttl: int = TOKEN_TTL) -> str:
        import jwt

        now = int(time.time())
        return jwt.encode({"iss": ISSUER, "sub": subject(name), "aud": AUDIENCE, "iat": now, "nbf": now - 5,
                           "exp": now + ttl, "jti": uuid.uuid4().hex}, self._key(), algorithm="ES256",
                          headers={"kid": self.kid()})

    def issuer(self, directory: Optional[str] = None):
        """The workload issuer Cairn trusts for Docker deployments (its keys in a file of their own)."""
        from cairn.identity.workload import Issuer

        d = directory or tempfile.mkdtemp(prefix="cairn-airflow-issuer-")
        path = os.path.join(d, "jwks.json")
        with open(path, "w") as fh:
            json.dump(self.jwks(), fh)
        return Issuer(ISSUER_NAME, ISSUER, AUDIENCE, jwks_file=path, algorithms=("ES256",),
                      max_token_lifetime=2 * TOKEN_TTL, token_ttl=TOKEN_TTL)

    # ---- Airflow's own API, as Cairn's operations user in the deployment -------------------------------------
    def api_token(self, name: str, uid: str, user_id: int, audience: str, ttl: int = 300) -> str:
        """A token Airflow's API accepts (HS512 with the deployment's JWT secret) for its Cairn operations user."""
        import jwt

        now = int(time.time())
        secret = self.secrets(name, uid)["jwt_secret"]
        return jwt.encode({"sub": str(user_id), "aud": audience, "iat": now, "nbf": now - 5, "exp": now + ttl,
                           "jti": uuid.uuid4().hex}, secret, algorithm="HS512")
