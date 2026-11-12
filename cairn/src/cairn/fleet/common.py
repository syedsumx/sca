"""The fleet's tables and signing (L161), shared by the vendor's console (cairn.fleet.server) and the reporter in
each install (cairn.fleet.reporter); no web framework here, so any Cairn process can load them."""

from __future__ import annotations

import hashlib
import hmac

from sqlalchemy import BigInteger, Column, Integer, String, Table

from cairn.common.db import JSONText, metadata

installs_table = Table(
    "cairn_fleet_installs", metadata,
    Column("id", String(40), primary_key=True),
    Column("name", String(200), nullable=False),
    Column("customer", String(200), nullable=False),
    Column("channel", String(32), nullable=False),
    Column("licence_id", String(64), nullable=True),
    Column("salt", String(64), nullable=False),          # the key is HMAC(master, id:salt); rotating changes the salt
    Column("created_by", String(200), nullable=False),
    Column("created_ms", BigInteger, nullable=False),
    Column("last_sent_ms", BigInteger, nullable=True),   # the newest check-in's own time: older ones are replays
    Column("last_seen_ms", BigInteger, nullable=True),   # when the console received it
    Column("last", JSONText, nullable=True),             # the newest check-in, as stored
)

checkins_table = Table(
    "cairn_fleet_checkins", metadata,
    Column("seq", Integer, primary_key=True, autoincrement=True),
    Column("install_id", String(40), nullable=False, index=True),
    Column("received_ms", BigInteger, nullable=False),
    Column("body", JSONText, nullable=False),
)

def install_key(master: str, install_id: str, salt: str) -> str:
    return hmac.new(master.encode(), f"{install_id}:{salt}".encode(), hashlib.sha256).hexdigest()


def sign(key: str, body: bytes) -> str:
    return "sha256=" + hmac.new(key.encode(), body, hashlib.sha256).hexdigest()
