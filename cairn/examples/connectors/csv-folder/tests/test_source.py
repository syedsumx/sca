"""The connector's own CI: the Cairn test kit, plus what this connector promises."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cairn.sdk.testkit import assert_conformant  # noqa: E402
from cairn_csv_folder.source import Config, CsvFolder  # noqa: E402

DATA = os.path.join(os.path.dirname(__file__), "data")


def test_conformant():
    assert_conformant(CsvFolder(), {"folder": DATA, "owner": "ana"})


def test_schema_and_description():
    by_name = {d.name: d for d in CsvFolder().datasets(Config(folder=DATA))}
    orders = by_name["exports.orders"]
    assert [(f.path, f.type) for f in orders.fields] == [("order_id", "number"), ("amount", "number"), ("paid", "boolean")]
    assert orders.description == "Orders exported nightly from the shop."
    assert CsvFolder().test_connection(Config(folder="/nope")) == (False, "/nope is not a folder the executor can read")
