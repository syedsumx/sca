"""A Cairn for the IaC tests (bundles, export, Terraform): the real API with as many of the configurable services
switched on as run without outside systems, an admin, and the prerequisites the registry's examples name (a small
warehouse for metrics and workbooks, a dataset, a gateway model, ...)."""

from __future__ import annotations

import base64
import json
import os

ADMIN = "urn:li:corpuser:admin"
TOKEN = "iac-token-" + "t" * 24
ANA, ANA_TOKEN = "urn:li:corpuser:ana", "iac-ana-token-" + "a" * 20     # someone with no rights of their own
DS = "urn:li:dataset:(urn:li:dataPlatform:iceberg,raw.orders,PROD)"
WH = "urn:li:dataset:(urn:li:dataPlatform:sqlite,main.orders,PROD)"

ENV = {
    "CAIRN_PIPES_STORE": json.dumps({"endpoint": "http://127.0.0.1:1"}),
    "CAIRN_PIPES_CATALOG": json.dumps({"uri": "http://127.0.0.1:1"}),
    "CAIRN_STREAMS_ENABLED": "true",
    "CAIRN_MAINTENANCE_CONNECTION": "lakehouse",
    "CAIRN_SECRETS_KEY": base64.b64encode(b"k" * 32).decode(),
    "CAIRN_JOBS_RUNTIME": "docker",
    "CAIRN_GIT_FOLDER_URLS": "https://git.example.com/",
    "CAIRN_AI_GATEWAY_OPENAI_URL": "http://127.0.0.1:1",
    "CAIRN_AI_GATEWAY_MODELS": "test-model",
    "CAIRN_DOC_SOURCES_STORE": json.dumps({"endpoint": "http://127.0.0.1:1"}),
    # Model monitors are kept by Cairn (a period baseline reads only Cairn's own call log); MLflow is not called.
    "CAIRN_MLFLOW_URL": "http://127.0.0.1:1",
    "CAIRN_TOKENS_KEY": base64.b64encode(b"t" * 32).decode(),          # tokenisation domains
}
# Managed Postgres instances on Docker, where there is Docker (real PostgreSQL containers; no backup store here).
if os.path.exists("/var/run/docker.sock"):
    ENV.update({"CAIRN_PG_RUNTIME": "docker", "CAIRN_PG_INSTANCE_KEY": base64.b64encode(b"p" * 32).decode(),
                "CAIRN_PG_DOCKER_PORTS": "16432-16499"})


def build(tmp, with_ai: bool = False):
    """Services for the tests (the environment above is set only while they are built)."""
    import sqlalchemy

    from cairn.common.config import Settings
    from cairn.common.core import InMemoryGraph
    from cairn.governance.abac import Policy, PolicyBundle
    from cairn.quality.collectors import ConnectionRegistry
    from cairn.services import build_services

    old = {k: os.environ.get(k) for k in ENV}
    os.environ.update(ENV)
    try:
        s = build_services(Settings(database_url=f"sqlite:///{tmp}/cairn.db", service_token="svc-secret",
                                    gateway={"key_dir": str(tmp / "keys")}), graph=InMemoryGraph(), with_ai=with_ai,
                           with_gateway=True)
    finally:
        for k, v in old.items():
            os.environ.pop(k) if v is None else os.environ.__setitem__(k, v)
    s.pdp.set_bundle(PolicyBundle(policies=[Policy(id="admin", effect="allow", actions=["*"],
                                                   condition={"attr": "subject.urn", "op": "eq", "value": ADMIN})]))
    real = s.identity._me_lookup
    who = {f"Bearer {TOKEN}": ADMIN, f"Bearer {ANA_TOKEN}": ANA}
    s.identity._me_lookup = lambda url, h, c: who.get(h.get("Authorization") or "") or real(url, h, c)
    # A small warehouse: metrics, metric alerts and workbooks compile against a real table.
    with sqlalchemy.create_engine(f"sqlite:///{tmp}/wh.db").begin() as c:
        c.execute(sqlalchemy.text("CREATE TABLE IF NOT EXISTS orders (id INTEGER, region TEXT, amount REAL, day DATE)"))
    s.data_access.registry = ConnectionRegistry({"wh": {"url": f"sqlite:///{tmp}/wh.db", "catalog": {"platform": "sqlite"}}})
    for urn, cols in ((DS, ("id", "region", "amount")), (WH, ("id", "region", "amount", "day"))):
        s.graph.aspects[(urn, "schemaMetadata")] = {"fields": [{"fieldPath": c} for c in cols]}

    class NoCatalog:   # this world has no Iceberg catalog: a vector index's table was never built
        def purge_table(self, ident):
            from pyiceberg.exceptions import NoSuchTableError

            raise NoSuchTableError(ident)

    s.vectors._catalog = NoCatalog
    # Where a dashboard's webhook action may post (CAIRN_ACTION_WEBHOOK_ORIGINS, read when an action is saved).
    s.bi_actions._origins = lambda: ["https://hooks.example.com"]
    return s


def prerequisites(engine) -> None:
    """What the examples name: the secret scope the syncs' keys are in."""
    from cairn.iac import registry

    k = registry.kind("secret_scopes")
    if not engine.find(k, {}, "finance-db"):
        engine.create(k, {"name": "finance-db"})
