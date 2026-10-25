import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from datahub_enterprise.common.config import Settings  # noqa: E402
from datahub_enterprise.common.db import get_engine, init_db  # noqa: E402
from datahub_enterprise.common.gms import InMemoryGraph  # noqa: E402

DS = "urn:li:dataset:(urn:li:dataPlatform:postgres,finance.public.orders,PROD)"
DS_DOWN = "urn:li:dataset:(urn:li:dataPlatform:snowflake,analytics.orders_enriched,PROD)"
DS_DOWN2 = "urn:li:dataset:(urn:li:dataPlatform:looker,orders_dashboard_view,PROD)"


@pytest.fixture
def engine(tmp_path):
    e = get_engine(f"sqlite:///{tmp_path}/dhe.db")
    init_db(e)
    return e


@pytest.fixture
def graph():
    return InMemoryGraph()


@pytest.fixture
def settings(tmp_path):
    return Settings(
        database_url=f"sqlite:///{tmp_path}/dhe.db",
        service_token="svc-secret",
        dev_trust_actor_header=True,
        gateway={"key_dir": str(tmp_path / "keys")},
    )
