import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cairn.common.config import Settings  # noqa: E402
from cairn.common.db import get_engine, init_db  # noqa: E402
from cairn.common.core import InMemoryGraph  # noqa: E402

def _patient_httpx(seconds: float = 30.0) -> None:
    """Requests the tests themselves make wait up to ``seconds`` unless they say otherwise: httpx's default is 5 s,
    and on a loaded CI runner a request to a test's own server can take longer than that (several jobs failed on
    ReadTimeout). Only calls made from files in this folder are changed: Cairn's own code keeps its timeouts."""
    import functools

    import httpx

    here = os.path.dirname(os.path.abspath(__file__)) + os.sep

    def from_tests() -> bool:
        return sys._getframe(2).f_code.co_filename.startswith(here)

    def patient(fn):
        @functools.wraps(fn)
        def call(*args, **kwargs):
            if "timeout" not in kwargs and from_tests():
                kwargs["timeout"] = seconds
            return fn(*args, **kwargs)
        return call

    for name in ("request", "get", "post", "put", "patch", "delete", "head", "options", "stream"):
        setattr(httpx, name, patient(getattr(httpx, name)))
    for cls in (httpx.Client, httpx.AsyncClient):
        cls.__init__ = patient(cls.__init__)


_patient_httpx()

DS = "urn:li:dataset:(urn:li:dataPlatform:postgres,finance.public.orders,PROD)"
DS_DOWN = "urn:li:dataset:(urn:li:dataPlatform:snowflake,analytics.orders_enriched,PROD)"
DS_DOWN2 = "urn:li:dataset:(urn:li:dataPlatform:looker,orders_dashboard_view,PROD)"


@pytest.fixture
def engine(tmp_path):
    e = get_engine(f"sqlite:///{tmp_path}/cairn.db")
    init_db(e)
    return e


@pytest.fixture
def graph():
    return InMemoryGraph()


@pytest.fixture
def settings(tmp_path):
    return Settings(
        database_url=f"sqlite:///{tmp_path}/cairn.db",
        service_token="svc-secret",
        dev_trust_actor_header=True,
        gateway={"key_dir": str(tmp_path / "keys")},
    )
