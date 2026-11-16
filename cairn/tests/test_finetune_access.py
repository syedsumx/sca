"""Who may use a fine-tuned model and read its examples, decided against what its training read was: the training set
is read through the real governed query path (a SQLite warehouse, masks and row filters per person); the worker is
replaced, since only the access decisions are tested here (tests/test_finetune.py trains for real).

Review finding: a model's examples, and what it repeats, are its creator's read, but any user with USE_FINETUNE (or
MANAGE_FINETUNE for the examples) and VIEW_DATASET_PROFILE on the sources was let in, even when their own reads of
those sources are row-filtered or masked where the creator's were not. The owner was not checked at all, so
losing access to the data left the examples readable."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine, text

from cairn.common.audit import AuditLog
from cairn.common.db import init_db
from cairn.coreproxy.masking import NO_MASK, DatasetPlan
from cairn.dataaccess.service import DataAccessService
from cairn.ml.finetune import FineTuneError, FineTunes, restriction_items
from cairn.quality.collectors import ConnectionRegistry

TICKETS = "urn:li:dataset:(urn:li:dataPlatform:sqlite,main.tickets,PROD)"
ANA, BOB, CAL, DEE = (f"urn:li:corpuser:{u}" for u in ("ana", "bob", "cal", "dee"))


@pytest.fixture
def world(tmp_path):
    wh_url = f"sqlite:///{tmp_path}/wh.db"
    with create_engine(wh_url).begin() as c:
        c.execute(text("CREATE TABLE tickets (id INTEGER, subject TEXT, team TEXT, region TEXT, email TEXT)"))
        for i in range(24):
            c.execute(text("INSERT INTO tickets VALUES (:i, :s, :t, :r, :e)"),
                      {"i": i, "s": f"ticket {i}", "t": "refund" if i % 2 else "shipping", "r": "EU" if i < 12 else "US",
                       "e": f"customer{i}@example.com"})
    engine = create_engine(f"sqlite:///{tmp_path}/cairn.db")
    init_db(engine)
    # Ana reads everything; Bob only EU rows; Cal sees email masked; Dee reads it as Ana does.
    filters = {BOB: ["region = 'EU'"]}
    plans = {CAL: DatasetPlan(fields={"email"}, strategy="redact")}
    das = DataAccessService(ConnectionRegistry({"wh": {"url": wh_url, "catalog": {"platform": "sqlite"}}}),
                            lambda actor, u: plans.get(actor, NO_MASK), lambda actor, u: True, AuditLog(engine),
                            known=lambda u: u == TICKETS)
    das.row_filters = lambda actor, u, dialect: list(filters.get(actor, []))
    grants = {ANA: {"CREATE_FINETUNE", "VIEW_DATASET_PROFILE"}}
    for u in (BOB, CAL, DEE):
        grants[u] = {"USE_FINETUNE", "MANAGE_FINETUNE", "VIEW_DATASET_PROFILE"}
    models = tmp_path / "models"
    (models / "tiny").mkdir(parents=True)
    (models / "tiny" / "config.json").write_text("{}")
    ft = FineTunes(engine, lambda actor, sql, conn, limit: das.query(actor, sql, conn, None, limit),
                   lambda actor, priv, u: priv in grants.get(actor, set()), workdir=str(tmp_path / "ft"),
                   models=str(models),
                   restrictions=lambda actor, u: restriction_items(das.row_filters(actor, u, "trino"),
                                                                   das.plan_for(actor, u)))
    ft._run = lambda job, d: {"epoch_loss": [1.0]} if job["mode"] == "train" else {"outputs": ["x"] * len(job["prompts"])}
    return {"ft": ft, "grants": grants, "filters": filters}


def test_a_model_is_used_only_by_those_whose_reads_are_as_open_as_its_training(world):
    ft = world["ft"]
    out = ft.create(ANA, {"name": "triage", "base_model": "tiny", "wait": True,
                          "source": {"connection": "wh", "sql": "SELECT subject, team, email FROM main.tickets"},
                          "prompt_column": "email", "completion_column": "team", "redact_pii": False})
    assert out["status"] == "done" and out["examples"] == 24 and out["sources"] == [TICKETS]
    assert any("customer20@example.com" in e["prompt"] for e in ft.examples(ANA, "triage", 100))   # a US row, unmasked
    # Dee reads the tickets as Ana did: she may use the model and read its examples.
    assert len(ft.examples(DEE, "triage", 5)) == 5
    assert ft.generate(DEE, "triage", ["ticket 1"])["outputs"] == ["x"]
    # Bob reads EU rows only, Cal sees email masked: the model and its examples would give them more.
    for who in (BOB, CAL):
        for call in (lambda w=who: ft.examples(w, "triage", 100), lambda w=who: ft.generate(w, "triage", ["ticket 1"])):
            with pytest.raises(FineTuneError) as e:
                call()
            assert e.value.status == 403, who
    # The owner too is decided at each call: without read access now, neither the model nor its examples.
    world["grants"][ANA].discard("VIEW_DATASET_PROFILE")
    with pytest.raises(FineTuneError):
        ft.examples(ANA, "triage")
    with pytest.raises(FineTuneError):
        ft.generate(ANA, "triage", ["ticket 1"])
    world["grants"][ANA].add("VIEW_DATASET_PROFILE")
    # Nor once a row filter is put on the owner's reads after training.
    world["filters"][ANA] = ["region = 'EU'"]
    with pytest.raises(FineTuneError):
        ft.examples(ANA, "triage")
    world["filters"].pop(ANA)
    assert len(ft.examples(ANA, "triage", 3)) == 3


def test_restrictions_are_compared_as_items():
    plan = DatasetPlan(fields={"email"}, strategy="partial")
    a = restriction_items(["region = 'EU'"], plan)
    assert json.dumps(["row_filter", "region = 'EU'"]) in a and json.dumps(["mask", "email", "partial"]) in a
    assert restriction_items([], NO_MASK) == []
