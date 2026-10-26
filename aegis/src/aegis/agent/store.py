"""Gateway-side persistence: pools, enrolment tokens, agents, tasks/runs, nonces."""

from __future__ import annotations

from sqlalchemy import BigInteger, Boolean, Column, Integer, String, Table

from aegis.common.db import JSONText, metadata

pools = Table(
    "aegis_agent_pools",
    metadata,
    Column("name", String(64), primary_key=True),
    Column("description", String(1000), nullable=False, default=""),
    Column("allowed_platforms", JSONText, nullable=False),
    Column("allowed_aspects", JSONText, nullable=True),
    Column("max_concurrency", Integer, nullable=False, default=2),
    Column("allow_profiling", Boolean, nullable=False, default=True),
    Column("created_at", String(40), nullable=False),
)

enrollment_tokens = Table(
    "aegis_agent_enrollment_tokens",
    metadata,
    Column("token_hash", String(64), primary_key=True),
    Column("pool", String(64), nullable=False),
    Column("expires_at_ms", BigInteger, nullable=False),
    Column("used_by", String(64), nullable=True),
    Column("created_by", String(512), nullable=False),
)

agents = Table(
    "aegis_agents",
    metadata,
    Column("agent_id", String(64), primary_key=True),
    Column("pool", String(64), nullable=False, index=True),
    Column("hostname", String(255), nullable=False),
    Column("version", String(64), nullable=False),
    Column("signing_public", String(128), nullable=False),
    Column("encryption_public", String(128), nullable=False),
    Column("status", String(16), nullable=False, default="ONLINE"),
    Column("enrolled_at_ms", BigInteger, nullable=False),
    Column("last_heartbeat_ms", BigInteger, nullable=True),
    Column("last_ip", String(64), nullable=True),
    Column("running_tasks", Integer, nullable=False, default=0),
)

tasks = Table(
    "aegis_agent_tasks",
    metadata,
    Column("task_id", String(64), primary_key=True),
    Column("pool", String(64), nullable=False, index=True),
    Column("kind", String(32), nullable=False, default="ingest"),
    Column("recipe", JSONText, nullable=False),
    Column("execution_request_urn", String(512), nullable=True, unique=True),
    Column("status", String(16), nullable=False, index=True),  # QUEUED|LEASED|RUNNING|SUCCESS|FAILURE|CANCELLED
    Column("attempts", Integer, nullable=False, default=0),
    Column("max_attempts", Integer, nullable=False, default=3),
    Column("agent_id", String(64), nullable=True),
    Column("run_id", String(64), nullable=True, index=True),
    Column("lease_expires_ms", BigInteger, nullable=True),
    Column("created_at_ms", BigInteger, nullable=False),
    Column("started_at_ms", BigInteger, nullable=True),
    Column("finished_at_ms", BigInteger, nullable=True),
    Column("last_seq", Integer, nullable=False, default=-1),
    Column("mcps_forwarded", Integer, nullable=False, default=0),
    Column("mcps_rejected", Integer, nullable=False, default=0),
    Column("report", JSONText, nullable=True),
    Column("logs", JSONText, nullable=True),
)

nonces = Table(
    "aegis_agent_nonces",
    metadata,
    Column("nonce", String(64), primary_key=True),
    Column("agent_id", String(64), nullable=False),
    Column("seen_at_ms", BigInteger, nullable=False, index=True),
)
