"""Fine-tuning (Snowflake gap M), for real: a base model (distilbert/distilgpt2, put in place with
`cairn finetune-model download`, CAIRN_FINETUNE_MODEL_DIR) is tuned with LoRA on PyTorch on examples read through
Cairn's governed query path from a real database, then used through Cairn. Checked:
- the training data is what the asker may see (a masked column stays masked);
- PII in the text is redacted;
- the loss falls and the tuned model answers what it was taught;
- training runs offline in a process of its own with none of Cairn's environment;
- use is governed and audited;
- too little data and a missing base model are refused.
Skipped without torch, transformers and peft, or without the base model."""

from __future__ import annotations

import json
import os

import pytest
from sqlalchemy import create_engine, select, text

pytest.importorskip("torch")
pytest.importorskip("transformers")
pytest.importorskip("peft")

from cairn.common.audit import AuditLog, audit_log  # noqa: E402
from cairn.common.db import init_db  # noqa: E402
from cairn.coreproxy.masking import DatasetPlan  # noqa: E402
from cairn.dataaccess.service import DataAccessService  # noqa: E402
from cairn.quality.collectors import ConnectionRegistry  # noqa: E402

BASE = "distilbert/distilgpt2"
TICKETS = "urn:li:dataset:(urn:li:dataPlatform:sqlite,main.tickets,PROD)"
ANA, BOB = "urn:li:corpuser:ana", "urn:li:corpuser:bob"
SUBJECTS = {"refund": ["refund my order", "money back please", "charged twice refund", "cancel and refund",
                       "refund not received", "want a refund now", "refund status", "refund for damaged item"],
            "shipping": ["parcel is late", "where is my package", "tracking not updating", "delivery delayed",
                         "package lost in transit", "courier did not come", "shipping address wrong", "late delivery"]}


@pytest.fixture
def world(tmp_path, monkeypatch):
    root = os.environ.get("CAIRN_FINETUNE_MODEL_DIR", "")
    if not root or not os.path.isfile(os.path.join(root, BASE.replace("/", "--"), "config.json")):
        pytest.skip(f"the base model is not in place: cairn finetune-model download {BASE}")
    from cairn.ml.finetune import FineTunes

    url = f"sqlite:///{tmp_path}/wh.db"
    wh = create_engine(url)
    with wh.begin() as c:
        c.execute(text("CREATE TABLE tickets (id INTEGER, subject TEXT, team TEXT, email TEXT)"))
        i = 0
        for team, subjects in SUBJECTS.items():
            for s_ in subjects:
                c.execute(text("INSERT INTO tickets VALUES (:i, :s, :t, :e)"),
                          {"i": i, "s": s_ if i != 3 else f"{s_}, call me on +1 415 555 0100", "t": team,
                           "e": f"customer{i}@example.com"})
                i += 1
    engine = create_engine(f"sqlite:///{tmp_path}/cairn.db")
    init_db(engine)
    audit = AuditLog(engine)
    das = DataAccessService(ConnectionRegistry({"wh": {"url": url, "catalog": {"platform": "sqlite"}}}),
                            lambda actor, urn: DatasetPlan(fields={"email"}, strategy="partial"),
                            lambda actor, urn: True, audit, known=lambda urn: urn == TICKETS)
    monkeypatch.setenv("CAIRN_SECRET_PROBE", "must-not-reach-the-worker")
    grants = {ANA: {"CREATE_FINETUNE"}, BOB: set()}
    ft = FineTunes(engine, lambda actor, sql, conn, limit: das.query(actor, sql, conn, None, limit),
                   lambda actor, priv, urn: priv in grants.get(actor, set()), audit,
                   workdir=str(tmp_path / "ft"), models=root)
    return {"ft": ft, "grants": grants, "engine": engine}


def test_a_model_is_tuned_on_governed_data_and_used_through_cairn(world, monkeypatch):
    import subprocess

    from cairn.ml.finetune import FineTuneError

    ft = world["ft"]
    envs = []
    real_run = subprocess.run

    def run(*a, **k):                                 # the real worker, with the environment it was given noted
        envs.append(dict(k.get("env") or {}))
        return real_run(*a, **k)
    monkeypatch.setattr("cairn.ml.finetune.subprocess.run", run)
    body = {"name": "triage", "base_model": BASE,
            "source": {"connection": "wh", "sql": "SELECT subject, team, email FROM main.tickets"},
            "prompt_column": "subject", "completion_column": "team",
            "prompt_template": "Ticket: {prompt}\nTeam:", "epochs": 25, "learning_rate": 0.002, "lora": True,
            "wait": True}
    with pytest.raises(FineTuneError) as e:
        ft.create(BOB, {**body, "name": "nope"})
    assert e.value.status == 403
    out = ft.create(ANA, body)
    assert out["status"] == "done", out["error"]
    assert out["examples"] == 16 and out["sources"] == [TICKETS] and out["redacted_values"] == 1
    loss = out["metrics"]["epoch_loss"]
    assert loss[-1] < loss[0] * 0.3, loss                                  # it learned
    assert out["metrics"]["trainable_parameters"] < 1_000_000              # LoRA: a small adapter
    # The examples are what Ana may see: the phone number redacted (the email column was masked, not asked for).
    examples = ft.examples(ANA, "triage", 100)
    assert all("555 0100" not in e["prompt"] for e in examples)
    assert any("[REDACTED" in e["prompt"] for e in examples)
    assert examples[0]["prompt"].startswith("Ticket: ") and examples[0]["completion"] in (" refund", " shipping")
    # Used through Cairn: it answers what it was taught.
    got = ft.generate(ANA, "triage", ["refund my order", "where is my package"], max_new_tokens=3)["outputs"]
    assert got[0].strip().startswith("refund") and got[1].strip().startswith("shipping"), got
    # Bob may not use it until a policy gives him USE_FINETUNE; every use is audited, without the prompts.
    with pytest.raises(FineTuneError) as e:
        ft.generate(BOB, "triage", ["refund my order"])
    assert e.value.status == 403
    world["grants"][BOB].add("USE_FINETUNE")
    assert len(ft.generate(BOB, "triage", ["late delivery"], max_new_tokens=2)["outputs"]) == 1
    with world["engine"].connect() as c:
        used = [json.loads(r.details) if isinstance(r.details, str) else r.details
                for r in c.execute(select(audit_log).where(audit_log.c.action == "finetune.used"))]
    assert [u["prompts"] for u in used] == [2, 1] and "refund my order" not in json.dumps(used)
    # The worker ran offline with none of Cairn's environment.
    assert envs and all("CAIRN_SECRET_PROBE" not in e and e.get("HF_HUB_OFFLINE") == "1" for e in envs)
    assert not any(k.startswith(("CAIRN_", "AWS_", "ANTHROPIC")) for e in envs for k in e)
    ft.delete(ANA, "triage")
    with pytest.raises(FineTuneError):
        ft.get(ANA, "triage")


def test_what_cannot_be_tuned_is_refused(world):
    from cairn.ml.finetune import FineTuneError

    ft = world["ft"]
    base = {"base_model": BASE, "source": {"connection": "wh", "sql": "SELECT subject, team FROM main.tickets"},
            "prompt_column": "subject", "completion_column": "team"}
    for bad, status in [({"name": "a", "source": {"connection": "wh",
                                                  "sql": "SELECT subject, team FROM main.tickets WHERE id < 3"}}, 400),
                        ({"name": "b", "base_model": "nobody/missing"}, 409),
                        ({"name": "c", "completion_column": "nope"}, 400),
                        ({"name": "d", "epochs": 0}, 400),
                        ({"name": "e", "prompt_template": "no slot"}, 400),
                        ({"name": "Bad"}, 400)]:
        with pytest.raises(FineTuneError) as e:
            ft.create(ANA, {**base, **bad})
        assert e.value.status == status, (bad, str(e.value))
