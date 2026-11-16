"""Fine-tuning requests that are checked before any training (no PyTorch needed): review findings on L91."""

from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine

from cairn.common.db import init_db
from cairn.ml.finetune import FineTuneError, FineTunes

ANA, BOB = "urn:li:corpuser:ana", "urn:li:corpuser:bob"
SQL = "SELECT subject, team FROM main.tickets WHERE email = 'vip@example.com'"


@pytest.fixture
def ft(tmp_path, monkeypatch):
    models = tmp_path / "models"
    (models / "base").mkdir(parents=True)
    (models / "base" / "config.json").write_text("{}")
    (tmp_path / "elsewhere").mkdir()
    (tmp_path / "elsewhere" / "config.json").write_text("{}")        # a folder outside the models' one
    engine = create_engine(f"sqlite:///{tmp_path}/cairn.db")
    init_db(engine)
    rows = [[f"subject {i}", "team"] for i in range(12)]
    grants = {ANA: {"CREATE_FINETUNE"}, BOB: {"USE_FINETUNE"}}
    f = FineTunes(engine, lambda actor, sql, conn, limit: {"columns": ["subject", "team"], "rows": rows, "tables": []},
                  lambda actor, priv, urn: priv in grants.get(actor, set()), workdir=str(tmp_path / "ft"),
                  models=str(models))
    monkeypatch.setattr(FineTunes, "_train", lambda self, name: None)    # no training: the requests are the point
    return f


BODY = {"base_model": "base", "source": {"connection": "wh", "sql": SQL}, "prompt_column": "subject",
        "completion_column": "team"}


def test_the_base_model_is_a_model_id_not_a_path(ft, tmp_path):
    """Review finding: base_model was joined to the models' folder unchecked, so ``../elsewhere`` or an absolute
    path loaded (and trained on) any folder on the server holding a config.json."""
    for bad in ("../elsewhere", str(tmp_path / "elsewhere"), "a/../../elsewhere"):
        with pytest.raises(FineTuneError) as e:
            ft.create(ANA, {**BODY, "name": "x", "base_model": bad})
        assert e.value.status == 400, (bad, str(e.value))
    assert ft.create(ANA, {**BODY, "name": "ok"})["base_model"] == "base"


def test_max_length_is_checked(ft):
    """Review finding: max_length went through int() unchecked: a string was a 500, any size was accepted."""
    for bad in ("abc", 0, -5, 10**9, True, 1.5):
        with pytest.raises(FineTuneError) as e:
            ft.create(ANA, {**BODY, "name": "x", "max_length": bad})
        assert e.value.status == 400 and "max_length" in str(e.value)


def test_the_training_query_is_shown_to_its_creator_only(ft):
    """Review finding: GET and list returned the creator's training SQL (its literals included) to anyone with
    USE_FINETUNE, without the read check on its sources that use itself has."""
    ft.create(ANA, {**BODY, "name": "triage"})
    assert ft.get(ANA, "triage")["source"]["sql"] == SQL
    assert ft.get(BOB, "triage")["source"] is None
    assert [m["source"] for m in ft.list(BOB)] == [None]
    assert "vip@example.com" not in repr(ft.list(BOB))


def test_a_name_taken_at_the_same_moment_is_a_conflict(ft, monkeypatch):
    """Review finding: two creates of one name both passed the existence check; the second failed with a database
    500 and left its training data on disk."""
    real = FineTunes._examples
    first = []

    def examples(self, actor, spec):            # the other create commits while this one reads its training data
        out = real(self, actor, spec)
        if not first:
            first.append(1)
            self.create(ANA, {**BODY, "name": "triage"})
        return out
    monkeypatch.setattr(FineTunes, "_examples", examples)
    with pytest.raises(FineTuneError) as e:
        ft.create(ANA, {**BODY, "name": "triage"})
    assert e.value.status == 409
    assert len(os.listdir(ft.workdir)) == 1      # the loser's training data is not left behind
