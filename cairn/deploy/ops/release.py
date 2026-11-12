#!/usr/bin/env python3
"""Vendor-side release tooling (L159): signing keys, signed manifests, and the feed installs read. Not shipped in
the images.

    # once per key: keep the private key in your secrets manager, bake the public key into the images
    python deploy/ops/release.py keygen --kid cairn-rel-2026 --out cairn-rel-2026.pem   # prints the public key entry

    # a release: describe it (see cairn.release.manifest for the fields), sign it, add it to its channels
    python deploy/ops/release.py sign --key-file cairn-rel-2026.pem --kid cairn-rel-2026 2.14.0.yaml > 2.14.0.json
    python deploy/ops/release.py publish --feed site/ --key-file cairn-rel-2026.pem --kid cairn-rel-2026 2.14.0.json

    # take a bad release off a channel; re-sign a channel's index before it expires (run it on a schedule)
    python deploy/ops/release.py withdraw --feed site/ --channel stable --version 2.14.0 --key-file ... --kid ...
    python deploy/ops/release.py refresh --feed site/ --channel stable --key-file ... --kid ...

    # check a feed as an install would (against the keys an image carries, or --keys-file)
    python deploy/ops/release.py verify --feed https://releases.example.com/cairn --channel stable --current 2.12.0

Several --key-file/--kid pairs sign with several keys (key rotation). Private keys are read from files, never from
the command line. The development key (deploy/release/dev-private-key.pem, kid ``cairn-release-dev``) is public:
what it signs is accepted on the dev channel only.
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402

from cairn.release import publish as pub  # noqa: E402
from cairn.release.manifest import ReleaseError, load_keys  # noqa: E402


def _keys(a) -> list:
    if len(a.key_file) != len(a.kid) or not a.key_file:
        raise SystemExit("give --key-file and --kid in pairs")
    out = []
    for path, kid in zip(a.key_file, a.kid):
        k = serialization.load_pem_private_key(Path(path).read_bytes(), None)
        if not isinstance(k, Ed25519PrivateKey):
            raise SystemExit(f"{path}: not an Ed25519 key")
        out.append((kid, k))
    return out


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    k = sub.add_parser("keygen")
    k.add_argument("--kid", required=True)
    k.add_argument("--out", required=True)
    for name in ("sign", "publish", "withdraw", "refresh"):
        s = sub.add_parser(name)
        s.add_argument("--key-file", action="append", default=[])
        s.add_argument("--kid", action="append", default=[])
        if name != "sign":
            s.add_argument("--feed", required=True, type=Path)
            s.add_argument("--days", type=int, default=30, help="how long the channel index stays valid")
        if name in ("withdraw", "refresh"):
            s.add_argument("--channel", required=True)
        if name == "withdraw":
            s.add_argument("--version", required=True)
        if name in ("sign", "publish"):
            s.add_argument("file")
    v = sub.add_parser("verify")
    v.add_argument("--feed", required=True)
    v.add_argument("--channel", default="stable")
    v.add_argument("--current", required=True)
    v.add_argument("--keys-file", type=Path, help="public keys ({\"keys\": {kid: base64}}); default: the image's")
    v.add_argument("--threshold", type=int, default=1)
    a = p.parse_args(argv)
    try:
        if a.cmd == "keygen":
            key = Ed25519PrivateKey.generate()
            out = Path(a.out)
            out.touch(mode=0o600, exist_ok=False)
            out.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                              serialization.NoEncryption()))
            raw = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
            print(json.dumps({a.kid: base64.b64encode(raw).decode()}))
        elif a.cmd == "sign":
            import yaml

            sys.stdout.buffer.write(pub.sign_manifest(yaml.safe_load(Path(a.file).read_text()), _keys(a)))
        elif a.cmd == "publish":
            for idx in pub.publish(a.feed, Path(a.file).read_bytes(), _keys(a), a.days):
                print(f"{idx['channel']}: serial {idx['serial']}, {len(idx['releases'])} releases, "
                      f"valid until {idx['expires_at']}")
        elif a.cmd == "withdraw":
            idx = pub.withdraw(a.feed, a.channel, a.version, _keys(a), a.days)
            print(f"{a.channel}: serial {idx['serial']}, {a.version} withdrawn")
        elif a.cmd == "refresh":
            idx = pub.refresh(a.feed, a.channel, _keys(a), a.days)
            print(f"{a.channel}: serial {idx['serial']}, valid until {idx['expires_at']}")
        elif a.cmd == "verify":
            import sqlalchemy

            from cairn.common.db import metadata
            from cairn.release.feed import ReleaseFeed, state_table

            keys = load_keys(a.keys_file) if a.keys_file else None
            with tempfile.TemporaryDirectory() as d:
                engine = sqlalchemy.create_engine(f"sqlite:///{d}/state.db")
                metadata.create_all(engine, tables=[state_table])
                feed = ReleaseFeed(engine, a.feed, a.channel, a.current, a.threshold, keys=keys)
                state = feed.check()
            print(json.dumps(state, indent=2))
            return 1 if state["error"] else 0
    except ReleaseError as e:
        print(f"refused: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
