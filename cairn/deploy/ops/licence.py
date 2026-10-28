#!/usr/bin/env python3
"""Vendor-side licence tooling: make a signing key, issue licences. Not shipped in the images.

    # once per key: keep the private key in your secrets manager, bake the public key into the images
    python deploy/ops/licence.py keygen --kid cairn-2026 --out cairn-2026.pem >> deploy/licence/licence-keys.json

    python deploy/ops/licence.py issue --key cairn-2026.pem --kid cairn-2026 --customer "Acme" \\
        --tenant acme --edition enterprise --max-users 250 --days 365 > acme.licence

Images verify licences against deploy/licence/licence-keys.json, copied to
/usr/share/cairn/licence-keys.json at build time. The development key (deploy/licence/dev-private-key.pem,
kid ``cairn-dev``) is public: its licences are capped at 5 users and 31 days.
"""

from __future__ import annotations

import argparse
import base64
import json
import secrets
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402

from cairn.common.licence import EDITIONS, FEATURES, issue  # noqa: E402


def keygen(a) -> None:
    key = Ed25519PrivateKey.generate()
    out = Path(a.out)
    if out.exists():
        sys.exit(f"{out} exists: refusing to overwrite a signing key")
    out.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                      serialization.NoEncryption()))
    out.chmod(0o600)
    pub = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    print(json.dumps({a.kid: base64.b64encode(pub).decode()}))
    print(f"private key written to {out}; add the line above under \"keys\" in deploy/licence/licence-keys.json",
          file=sys.stderr)


def issue_cmd(a) -> None:
    key = serialization.load_pem_private_key(Path(a.key).read_bytes(), password=None)
    start = date.fromisoformat(a.start) if a.start else date.today()
    fields = {"id": f"lic_{secrets.token_hex(8)}", "customer": a.customer, "edition": a.edition,
              "issued_at": start.isoformat(), "expires_at": (start + timedelta(days=a.days)).isoformat(),
              "grace_days": a.grace_days, "features": sorted(set(a.feature or []))}
    if a.tenant:
        fields["tenant"] = a.tenant
    if a.max_users:
        fields["max_users"] = a.max_users
    print(issue(key, a.kid, **fields))


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    k = sub.add_parser("keygen", help="make a signing key pair")
    k.add_argument("--kid", required=True, help="key id, e.g. cairn-2026")
    k.add_argument("--out", required=True, help="where to write the private key (PEM)")
    k.set_defaults(fn=keygen)
    i = sub.add_parser("issue", help="sign a licence")
    i.add_argument("--key", required=True, help="private key (PEM)")
    i.add_argument("--kid", required=True)
    i.add_argument("--customer", required=True)
    i.add_argument("--tenant", help="tenant id the licence is bound to (CAIRN_TENANT_ID)")
    i.add_argument("--edition", choices=sorted(EDITIONS), default="enterprise")
    i.add_argument("--feature", action="append", choices=sorted(FEATURES), help="extra feature (repeatable)")
    i.add_argument("--max-users", type=int)
    i.add_argument("--days", type=int, default=365)
    i.add_argument("--start", help="first day, YYYY-MM-DD (default today)")
    i.add_argument("--grace-days", type=int, default=14)
    i.set_defaults(fn=issue_cmd)
    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
