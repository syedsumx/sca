"""Fine-tuning: a language model tuned on governed data, trained and served inside Cairn.

``POST /v1/ml/finetunes`` (``CREATE_FINETUNE``)::

    {"name": "ticket-triage", "base_model": "distilgpt2",
     "source": {"connection": "lakehouse", "sql": "SELECT subject, team FROM support.tickets"},
     "prompt_column": "subject", "completion_column": "team",
     "prompt_template": "Ticket: {prompt}\\nTeam:", "epochs": 3, "learning_rate": 0.0005, "lora": true}

* **The data is governed.** The SQL runs through Cairn's query path as the person who asks, so their access, masks,
  row filters and aggregation or projection policies shape the training set, exactly as a query of theirs would
  (at most 1000 rows, the query limit). PII left in the text (emails, phone numbers, card and account numbers) is
  redacted before training (``"redact_pii": false`` to keep it, for data that is meant to hold it). The examples are
  kept with the job (``GET .../examples``, for its owner), with the datasets they came from.
* **Trained in Cairn.** A base model is put in place once (``cairn finetune-model download <name>`` into
  ``CAIRN_FINETUNE_MODEL_DIR``); jobs never download. A job trains in a process of its own with no network and
  none of Cairn's environment: a LoRA adapter (default) or the whole model. The status moves queued → running →
  done | failed, with each epoch's loss.
* **Used through Cairn.** ``POST /v1/ml/finetunes/<name>/generate`` ``{"prompts": [...]}`` needs
  ``USE_FINETUNE`` on ``urn:li:finetune:<name>`` (its owner has it) and, for anyone but its owner, read access
  (``VIEW_DATASET_PROFILE``) to every dataset it was trained on, decided at each call (the same for the examples);
  every call is audited with the number of
  prompts, not their text. ``DELETE`` removes the model and its examples.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from typing import Any, Callable, Optional

from sqlalchemy import BigInteger, Boolean, Column, Float, Integer, String, Table, Text, insert, select, update

from cairn.common.db import metadata

logger = logging.getLogger(__name__)

finetunes_table = Table(
    "cairn_finetunes", metadata,
    Column("name", String(64), primary_key=True),
    Column("id", String(32), nullable=False, unique=True),
    Column("base_model", String(200), nullable=False),
    Column("spec", Text, nullable=False),                 # source, columns, template, epochs, learning rate, lora
    Column("sources", Text, nullable=False),              # dataset urns of the training data
    Column("examples", Integer, nullable=False),
    Column("redacted", Integer, nullable=False),
    Column("status", String(16), nullable=False),         # queued | running | done | failed
    Column("error", Text, nullable=True),
    Column("metrics", Text, nullable=True),
    Column("lora", Boolean, nullable=False),
    Column("learning_rate", Float, nullable=False),
    Column("created_by", String(512), nullable=False),
    Column("created_ms", BigInteger, nullable=False),
    Column("finished_ms", BigInteger, nullable=True),
)

NAME = re.compile(r"^[a-z][a-z0-9-]{0,40}$")
MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}(?:/[A-Za-z0-9][A-Za-z0-9._-]{0,99})?$")
MIN_EXAMPLES = 10
MAX_PROMPTS = 20


class FineTuneError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def urn(name: str) -> str:
    return f"urn:li:finetune:{name}"


def model_dir() -> str:
    return os.environ.get("CAIRN_FINETUNE_MODEL_DIR", "")


def local_name(model: str) -> str:
    return model.replace("/", "--")


def download(model: str, root: str) -> str:
    """Puts a base model (a Hugging Face model id) in ``root``, where jobs read it."""
    from huggingface_hub import snapshot_download

    if not MODEL.match(model):
        raise FineTuneError("a model id: owner/name or name")
    path = os.path.join(root, local_name(model))
    # The model's own files only, at the top of the repository (not ONNX, Core ML or other exports in folders).
    snapshot_download(model, local_dir=path, allow_patterns=["*.json", "*.safetensors", "*.txt", "*.model"],
                      ignore_patterns=["*/*"])
    if not any(f.endswith(".safetensors") for f in os.listdir(path)):
        raise FineTuneError(f"{model} has no safetensors weights", 422)
    return path


class FineTunes:
    """``query(actor, sql, connection, limit) -> {"columns", "rows", "tables"}`` is Cairn's governed query;
    ``allowed(actor, privilege, urn)`` the PDP."""

    def __init__(self, engine, query: Callable[..., dict], allowed: Callable[[str, str, Optional[str]], bool],
                 audit=None, workdir: Optional[str] = None, models: Optional[str] = None,
                 timeout: int = 3600, clock: Callable[[], int] = lambda: int(time.time() * 1000)):
        self.engine, self.query, self.allowed, self.audit, self.clock = engine, query, allowed, audit, clock
        self.workdir = workdir or os.path.join(os.environ.get("CAIRN_DATA_DIR", "/tmp/cairn"), "finetunes")
        self.models = models if models is not None else model_dir()
        self.timeout = timeout
        self._threads: dict[str, threading.Thread] = {}

    # ---- helpers ----------------------------------------------------------------------------------
    def _need(self, actor: str, privilege: str, target: Optional[str]) -> None:
        try:
            ok = self.allowed(actor, privilege, target)
        except Exception:  # noqa: BLE001 - fail closed
            ok = False
        if not ok:
            raise FineTuneError(f"{privilege} is needed" + (f" on {target}" if target else ""), 403)

    def _row(self, name: str):
        with self.engine.connect() as c:
            row = c.execute(select(finetunes_table).where(finetunes_table.c.name == name)).first()
        if row is None:
            raise FineTuneError("no such fine-tune", 404)
        return row

    def _dir(self, row) -> str:
        return os.path.join(self.workdir, row.id)

    def _base(self, model: str) -> str:
        if not self.models:
            raise FineTuneError("fine-tuning is not configured: set CAIRN_FINETUNE_MODEL_DIR", 503)
        path = os.path.join(self.models, local_name(model))
        if not os.path.isfile(os.path.join(path, "config.json")):
            raise FineTuneError(f"base model {model} is not in place: cairn finetune-model download {model}", 409)
        return path

    def _audit(self, action: str, actor: str, name: str, details: dict) -> None:
        if self.audit is not None:
            self.audit.record("ml", action, actor, urn(name), details)

    @staticmethod
    def _public(r) -> dict:
        spec = json.loads(r.spec)
        return {"name": r.name, "urn": urn(r.name), "base_model": r.base_model, "status": r.status, "error": r.error,
                "examples": r.examples, "redacted_values": r.redacted, "sources": json.loads(r.sources),
                "metrics": json.loads(r.metrics) if r.metrics else None, "lora": r.lora, "learning_rate": r.learning_rate,
                "epochs": spec["epochs"], "source": spec["source"], "prompt_template": spec["prompt_template"],
                "created_by": r.created_by, "created_ms": r.created_ms, "finished_ms": r.finished_ms}

    # ---- the training data ----------------------------------------------------------------------------
    def _examples(self, actor: str, spec: dict) -> tuple[list[dict], list[str], int]:
        from cairn.ai.guardrails import redact

        out = self.query(actor, spec["source"]["sql"], spec["source"]["connection"], 1000)
        cols = [c.lower() for c in out["columns"]]
        try:
            pi, ci = cols.index(spec["prompt_column"].lower()), cols.index(spec["completion_column"].lower())
        except ValueError:
            raise FineTuneError(f"the query's columns are {', '.join(out['columns'])}: name the prompt and "
                                "completion columns among them")
        examples, redacted = [], 0
        for row in out["rows"]:
            p, c = row[pi], row[ci]
            if p is None or c is None or not str(p).strip() or not str(c).strip():
                continue
            p, c = str(p), str(c)
            if spec["redact_pii"]:
                rp, rc = redact(p), redact(c)
                redacted += (rp != p) + (rc != c)
                p, c = rp, rc
            examples.append({"prompt": spec["prompt_template"].replace("{prompt}", p), "completion": " " + c.strip()})
        return examples, sorted(set(out.get("tables") or [])), redacted

    # ---- jobs ---------------------------------------------------------------------------------------
    def create(self, actor: str, body: dict) -> dict:
        name = str(body.get("name") or "")
        if not NAME.match(name):
            raise FineTuneError("name: lowercase letters, digits and '-'")
        self._need(actor, "CREATE_FINETUNE", None)
        base = str(body.get("base_model") or "")
        self._base(base)
        source = body.get("source") or {}
        if not isinstance(source, dict) or not source.get("sql") or not source.get("connection"):
            raise FineTuneError("source: {connection, sql}")
        epochs, lr = body.get("epochs", 3), body.get("learning_rate", 5e-4)
        if isinstance(epochs, bool) or not isinstance(epochs, int) or not 1 <= epochs <= 50:
            raise FineTuneError("epochs is 1-50")
        if isinstance(lr, bool) or not isinstance(lr, (int, float)) or not 0 < lr <= 0.1:
            raise FineTuneError("learning_rate is above 0 and at most 0.1")
        template = str(body.get("prompt_template") or "{prompt}")
        if "{prompt}" not in template or len(template) > 2000:
            raise FineTuneError("prompt_template holds {prompt}")
        spec = {"source": {"connection": str(source["connection"]), "sql": str(source["sql"])},
                "prompt_column": str(body.get("prompt_column") or "prompt"),
                "completion_column": str(body.get("completion_column") or "completion"),
                "prompt_template": template, "epochs": epochs, "redact_pii": body.get("redact_pii", True) is not False,
                "max_length": int(body.get("max_length", 256))}
        with self.engine.connect() as c:
            if c.execute(select(finetunes_table.c.name).where(finetunes_table.c.name == name)).first():
                raise FineTuneError(f"{name} exists", 409)
        try:
            examples, sources, redacted = self._examples(actor, spec)
        except FineTuneError:
            raise
        except Exception as e:  # noqa: BLE001 - the query was refused or failed: say so
            status = getattr(e, "status", 400)
            raise FineTuneError(f"the training data could not be read: {e}", status if isinstance(status, int) else 400)
        if len(examples) < MIN_EXAMPLES:
            raise FineTuneError(f"{len(examples)} examples: a fine-tune needs at least {MIN_EXAMPLES}")
        fid = uuid.uuid4().hex[:16]
        d = os.path.join(self.workdir, fid)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "train.jsonl"), "w") as fh:
            for e in examples:
                fh.write(json.dumps(e) + "\n")
        lora = body.get("lora", True) is not False
        with self.engine.begin() as c:
            c.execute(insert(finetunes_table).values(
                name=name, id=fid, base_model=base, spec=json.dumps(spec), sources=json.dumps(sources),
                examples=len(examples), redacted=redacted, status="queued", error=None, metrics=None, lora=lora,
                learning_rate=float(lr), created_by=actor, created_ms=self.clock(), finished_ms=None))
        self._audit("finetune.created", actor, name, {"base_model": base, "examples": len(examples),
                                                      "sources": sources, "redacted_values": redacted})
        t = threading.Thread(target=self._train, args=(name,), daemon=True, name=f"cairn-finetune-{name}")
        self._threads[name] = t
        t.start()
        if body.get("wait"):
            t.join()
        return self.get(actor, name)

    def _run(self, job: dict, d: str) -> dict:
        path = os.path.join(d, f"{job['mode']}-{uuid.uuid4().hex[:8]}.json")
        with open(path, "w") as fh:
            json.dump(job, fh)
        env = {k: os.environ[k] for k in ("PATH", "LANG", "TZ", "PYTHONPATH") if k in os.environ}
        env.update({"HOME": d, "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1", "HF_DATASETS_OFFLINE": "1",
                    "TOKENIZERS_PARALLELISM": "false"})
        try:
            r = subprocess.run([sys.executable, "-I", "-m", "cairn.ml.finetune_train", path], env=env,
                               capture_output=True, text=True, timeout=self.timeout, cwd=d)
        finally:
            os.unlink(path)
        if r.returncode != 0:
            tail = (r.stderr or r.stdout).strip().splitlines()[-1:] or ["no output"]
            raise FineTuneError(f"the worker failed: {tail[0][:500]}", 502)
        return json.loads(r.stdout.strip().splitlines()[-1])

    def _train(self, name: str) -> None:
        row = self._row(name)
        spec, d = json.loads(row.spec), self._dir(row)
        with self.engine.begin() as c:
            c.execute(update(finetunes_table).where(finetunes_table.c.name == name).values(status="running"))
        try:
            out = os.path.join(d, "model")
            shutil.rmtree(out, ignore_errors=True)
            metrics = self._run({"mode": "train", "base": self._base(row.base_model), "data": os.path.join(d, "train.jsonl"),
                                 "out": out, "epochs": spec["epochs"], "learning_rate": row.learning_rate,
                                 "lora": row.lora, "max_length": spec["max_length"]}, d)
            values = {"status": "done", "metrics": json.dumps(metrics), "error": None}
        except Exception as e:  # noqa: BLE001 - recorded on the job
            logger.warning("fine-tune %s failed: %s", name, e)
            values = {"status": "failed", "error": str(e)[:2000]}
        with self.engine.begin() as c:
            c.execute(update(finetunes_table).where(finetunes_table.c.name == name).values(finished_ms=self.clock(),
                                                                                           **values))
        self._audit(f"finetune.{values['status']}", row.created_by, name, {"error": values.get("error")})

    def get(self, actor: str, name: str) -> dict:
        row = self._row(name)
        if row.created_by != actor and not self.allowed(actor, "USE_FINETUNE", urn(name)):
            raise FineTuneError("no such fine-tune", 404)
        return self._public(row)

    def list(self, actor: str) -> list[dict]:
        with self.engine.connect() as c:
            rows = c.execute(select(finetunes_table).order_by(finetunes_table.c.created_ms.desc())).all()
        return [self._public(r) for r in rows if r.created_by == actor or self.allowed(actor, "USE_FINETUNE", urn(r.name))]

    def _may_read_sources(self, actor: str, row) -> None:
        """The model holds what its creator could read: anyone else must be allowed to read every dataset it was
        trained on, decided now (fail closed: a decision that cannot be made refuses)."""
        for src in json.loads(row.sources):
            try:
                ok = self.allowed(actor, "VIEW_DATASET_PROFILE", src)
            except Exception:  # noqa: BLE001 - fail closed
                ok = False
            if not ok:
                self._audit("finetune.refused", actor, row.name, {"source": src})
                raise FineTuneError("this model was trained on data you are not allowed to read", 403)

    def examples(self, actor: str, name: str, limit: int = 50) -> list[dict]:
        row = self._row(name)
        if row.created_by != actor:
            self._need(actor, "MANAGE_FINETUNE", urn(name))
            self._may_read_sources(actor, row)
        out = []
        with open(os.path.join(self._dir(row), "train.jsonl")) as fh:
            for line in fh:
                out.append(json.loads(line))
                if len(out) >= limit:
                    break
        return out

    def generate(self, actor: str, name: str, prompts: list, max_new_tokens: int = 32) -> dict:
        row = self._row(name)
        if row.created_by != actor:
            self._need(actor, "USE_FINETUNE", urn(name))
            self._may_read_sources(actor, row)
        if row.status != "done":
            raise FineTuneError(f"{name} is {row.status}, not ready", 409)
        if not isinstance(prompts, list) or not 1 <= len(prompts) <= MAX_PROMPTS or \
                not all(isinstance(p, str) and 0 < len(p) <= 4000 for p in prompts):
            raise FineTuneError(f"prompts: 1 to {MAX_PROMPTS} strings")
        if not 1 <= int(max_new_tokens) <= 256:
            raise FineTuneError("max_new_tokens is 1-256")
        spec, d = json.loads(row.spec), self._dir(row)
        out = self._run({"mode": "generate", "base": self._base(row.base_model), "adapter": os.path.join(d, "model"),
                         "lora": row.lora, "max_new_tokens": int(max_new_tokens),
                         "prompts": [spec["prompt_template"].replace("{prompt}", p) for p in prompts]}, d)
        self._audit("finetune.used", actor, name, {"prompts": len(prompts)})
        return {"name": name, "outputs": out["outputs"]}

    def delete(self, actor: str, name: str) -> None:
        row = self._row(name)
        if row.created_by != actor:
            self._need(actor, "MANAGE_FINETUNE", urn(name))
        if row.status in ("queued", "running"):
            raise FineTuneError(f"{name} is {row.status}: wait for it to finish", 409)
        shutil.rmtree(self._dir(row), ignore_errors=True)
        with self.engine.begin() as c:
            c.execute(finetunes_table.delete().where(finetunes_table.c.name == name))
        self._audit("finetune.deleted", actor, name, {})


def build_finetunes_router(ft: FineTunes, current_principal) -> Any:
    from fastapi import APIRouter, Body, Depends, HTTPException, Response
    from starlette.concurrency import run_in_threadpool

    router = APIRouter(prefix="/v1/ml/finetunes", tags=["ml"])

    async def call(fn):
        try:
            return await run_in_threadpool(fn)
        except FineTuneError as e:
            raise HTTPException(e.status, str(e))

    @router.get("")
    async def list_(p=Depends(current_principal)):
        return {"finetunes": await call(lambda: ft.list(p.urn))}

    @router.post("", status_code=202)
    async def create(body: dict = Body(...), p=Depends(current_principal)):
        return await call(lambda: ft.create(p.urn, {k: v for k, v in body.items() if k != "wait"}))

    @router.get("/{name}")
    async def get(name: str, p=Depends(current_principal)):
        return await call(lambda: ft.get(p.urn, name))

    @router.get("/{name}/examples")
    async def examples(name: str, p=Depends(current_principal)):
        return {"examples": await call(lambda: ft.examples(p.urn, name))}

    @router.post("/{name}/generate")
    async def generate(name: str, prompts: list = Body(..., embed=True), max_new_tokens: int = Body(32, embed=True),
                       p=Depends(current_principal)):
        return await call(lambda: ft.generate(p.urn, name, prompts, max_new_tokens))

    @router.delete("/{name}", status_code=204)
    async def delete(name: str, p=Depends(current_principal)):
        await call(lambda: ft.delete(p.urn, name))
        return Response(status_code=204)

    return router

