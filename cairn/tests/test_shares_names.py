"""A share's tables are matched by their exact namespace and name: a table whose name or namespace level holds a dot
reads, joined, like a shared one ("a"."x.y" and "a.x"."y" are both "a.x.y") and must not be read through the share."""

from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from cairn.common.db import metadata
from cairn.dataaccess.shares import Shares


class Lake:
    class access:
        @staticmethod
        def allowed(actor, groups, privilege, urn):
            return True

    @staticmethod
    def urn(namespace, name):
        return f"urn:li:dataset:(lakehouse.{'.'.join(namespace)}.{name})"

    @staticmethod
    def _restricted(actor, groups, urn):
        return False


def test_a_dotted_name_is_never_the_shared_table():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    metadata.create_all(engine)
    shares = Shares(engine, tokens=None, lake=Lake())
    share = shares.create("urn:li:corpuser:ana", [], "partner-a", "Partner A", ["a.x.y"], 7)
    user = f"share:{share['id']}"
    asks = [{"namespace": ["a", "x"], "name": "y"}, {"namespace": ["a"], "name": "x.y"},
            {"namespace": ["a.x"], "name": "y"}]
    got = shares.decide(user, asks)["tables"]
    assert [d["read"] for d in got] == [True, False, False]
