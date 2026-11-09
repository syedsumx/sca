"""Control-plane persistence for split deployments: data planes, their connectors, the calls queued for them, and
which compute each workspace uses."""

from __future__ import annotations

from sqlalchemy import BigInteger, Column, String, Table, Text

from cairn.common.db import JSONText, metadata

dataplanes = Table(
    "cairn_dataplanes", metadata,
    Column("name", String(64), primary_key=True),
    Column("description", String(1000), nullable=False, default=""),
    # The Trino connection (CAIRN connections, trino://...?dataplane=<name>) its queries run on.
    Column("connection", String(128), nullable=True),
    Column("created_by", String(256), nullable=False),
    Column("created_ms", BigInteger, nullable=False),
)

dataplane_tokens = Table(
    "cairn_dataplane_enrollment_tokens", metadata,
    Column("token_hash", String(64), primary_key=True),
    Column("dataplane", String(64), nullable=False, index=True),
    Column("expires_at_ms", BigInteger, nullable=False),
    Column("used_by", String(64), nullable=True),
    Column("created_by", String(256), nullable=False),
)

dataplane_connectors = Table(
    "cairn_dataplane_connectors", metadata,
    Column("connector_id", String(64), primary_key=True),
    Column("dataplane", String(64), nullable=False, index=True),
    Column("hostname", String(255), nullable=False),
    Column("version", String(64), nullable=False),
    Column("signing_public", String(128), nullable=False),
    Column("encryption_public", String(128), nullable=False),
    Column("previous_signing_public", String(128), nullable=True),
    Column("keys_rotated_at_ms", BigInteger, nullable=True),
    Column("status", String(16), nullable=False),                 # ONLINE | REVOKED
    Column("capabilities", JSONText, nullable=True),              # what it runs: trino, jobs (docker|kubernetes), storage
    Column("enrolled_at_ms", BigInteger, nullable=False),
    Column("last_seen_ms", BigInteger, nullable=True),
    Column("last_ip", String(64), nullable=True),
)

dataplane_calls = Table(
    "cairn_dataplane_calls", metadata,
    Column("call_id", String(64), primary_key=True),
    Column("dataplane", String(64), nullable=False, index=True),
    Column("kind", String(32), nullable=False),
    Column("request", JSONText, nullable=False),                  # cleared once answered
    Column("status", String(16), nullable=False, index=True),    # QUEUED | LEASED | DONE | FAILED | EXPIRED
    Column("connector_id", String(64), nullable=True),
    Column("result", Text, nullable=True),                        # the sealed answer, removed when read
    Column("error", String(2000), nullable=True),
    Column("actor", String(256), nullable=False),
    Column("created_ms", BigInteger, nullable=False, index=True),
    Column("deadline_ms", BigInteger, nullable=False),
    Column("leased_ms", BigInteger, nullable=True),
    Column("finished_ms", BigInteger, nullable=True),
)

dataplane_nonces = Table(
    "cairn_dataplane_nonces", metadata,
    Column("nonce", String(64), primary_key=True),
    Column("connector_id", String(64), nullable=False),
    Column("seen_at_ms", BigInteger, nullable=False, index=True),
)

workspace_compute = Table(
    "cairn_workspace_compute", metadata,
    Column("workspace", String(64), primary_key=True),
    Column("compute", String(16), nullable=False),                # customer | managed
    Column("dataplane", String(64), nullable=True),
    Column("updated_by", String(256), nullable=False),
    Column("updated_ms", BigInteger, nullable=False),
)
