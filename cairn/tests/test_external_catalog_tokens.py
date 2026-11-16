"""External catalogs: the token broker's cache and a name registered again (review finding on L105; the catalogs are
tested with Trino and a second real Cairn Catalog in tests/test_external_catalogs_e2e.py)."""

from __future__ import annotations

from sqlalchemy import create_engine, delete, insert

from cairn.common.db import metadata
from cairn.dataaccess.external_catalogs import ExternalCatalogs, external_catalogs

KEY = "trino-access-key"


def register(engine, ref: str, created_ms: int) -> None:
    with engine.begin() as c:
        c.execute(delete(external_catalogs).where(external_catalogs.c.name == "partner"))
        c.execute(insert(external_catalogs).values(
            name="partner", uri="https://catalog.example", auth="token", credential_ref=ref, writable=False,
            created_by="urn:li:corpuser:op", created_ms=created_ms, updated_ms=created_ms, trino_state="attached"))


def test_a_catalog_registered_again_never_gets_the_earlier_ones_token(tmp_path, monkeypatch):
    """Review finding: the broker cached each catalog's token by name only, and a delete clears the cache on the
    replica that ran it. On another replica, a catalog registered again under the same name (another URI, another
    credential) was handed the earlier registration's token for Trino to send to it, until that token expired."""
    monkeypatch.setenv("CAIRN_EXTCAT_OLD", "old-partner-token")
    monkeypatch.setenv("CAIRN_EXTCAT_NEW", "new-partner-token")
    engine = create_engine(f"sqlite:///{tmp_path}/c.db")
    metadata.create_all(engine)
    replica = ExternalCatalogs(engine, None, None, trino_key=KEY)
    register(engine, "env:CAIRN_EXTCAT_OLD", 1000)
    assert replica.broker("partner", KEY, "client_credentials")["access_token"] == "old-partner-token"
    register(engine, "env:CAIRN_EXTCAT_NEW", 2000)          # deleted and registered again elsewhere
    assert replica.broker("partner", KEY, "client_credentials")["access_token"] == "new-partner-token"
