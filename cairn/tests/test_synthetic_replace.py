"""Synthetic test data's replace writes over an earlier synthetic table, never over a real one named as the target."""

from __future__ import annotations

import types

import pytest

from cairn.pipelines.synthetic import Synthetic, SyntheticError


def test_replace_never_drops_a_table_that_is_not_synthetic(monkeypatch):
    pytest.importorskip("pyarrow")
    dropped = []
    real = types.SimpleNamespace(properties={"owner": "finance"})
    cat = types.SimpleNamespace(load_table=lambda ident: real, table_exists=lambda ident: True,
                                drop_table=lambda ident: dropped.append(ident))
    syn = Synthetic({}, lambda *a: True, lambda ns, t: f"urn:{ns}.{t}", lambda *a: ([], [], False), "lake",
                    lambda urn: set(), lambda *a: None)
    monkeypatch.setattr(syn, "_catalog", lambda: cat)
    with pytest.raises(SyntheticError, match="not a synthetic table") as e:
        syn.generate("u", "sales.orders", "finance.ledger", 10, replace=True)
    assert e.value.status == 409 and not dropped
