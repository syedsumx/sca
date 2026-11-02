"""Operators for the Cairn lakehouse: SQL on Trino and dbt, as the ``cairn`` connection's Cairn user."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Sequence
from typing import Any, Optional

try:  # Airflow 3
    from airflow.sdk import BaseOperator
except ImportError:  # pragma: no cover
    from airflow.models import BaseOperator

try:
    from airflow.exceptions import AirflowException
except ImportError:  # pragma: no cover
    AirflowException = RuntimeError  # type: ignore[misc,assignment]

from cairn_airflow.hooks import CairnHook

MAX_XCOM_ROWS = 1000


def split_statements(sql: str | Sequence[str]) -> list[str]:
    if not isinstance(sql, str):
        return [s for s in sql if s.strip()]
    try:
        import sqlparse

        return [s.strip().rstrip(";") for s in sqlparse.split(sql) if s.strip().rstrip(";").strip()]
    except ImportError:  # pragma: no cover - Airflow ships sqlparse
        return [s.strip() for s in sql.split(";") if s.strip()]


class CairnTrinoOperator(BaseOperator):
    """Runs SQL on Trino as the ``cairn`` connection's Cairn user (Cairn's policies, masks and row filters
    apply; Trino's lineage names the user). Statements run in order; the last one's rows (at most 1000)
    are the task's return value. Queries run as source ``airflow-cairn`` (Trino's ``pipelines`` group)."""

    template_fields: Sequence[str] = ("sql", "schema")
    template_ext: Sequence[str] = (".sql",)
    template_fields_renderers = {"sql": "sql"}
    ui_color = "#dfe9f3"

    def __init__(self, *, sql: str | Sequence[str], schema: Optional[str] = None, cairn_conn_id: str = "cairn_default",
                 token_ttl: int = 3600, return_rows: bool = True, **kwargs):
        super().__init__(**kwargs)
        self.sql, self.schema, self.cairn_conn_id = sql, schema, cairn_conn_id
        self.token_ttl, self.return_rows = token_ttl, return_rows

    def _lineage(self):
        """The tables the SQL reads and writes, for OpenLineage (Cairn names them as Trino does), so a
        failed run marks the datasets it writes as possibly stale."""
        from airflow.providers.common.compat.openlineage.facet import Dataset
        from airflow.providers.openlineage.extractors import OperatorLineage

        try:
            import openlineage_sql

            hook = CairnHook(self.cairn_conn_id)
            extra = hook.extra
            catalog = extra.get("trino_catalog", "lakehouse")
            ns = f"trino://{extra.get('trino_host', 'cairn-trino')}:{extra.get('trino_port', 8443)}"
            meta = openlineage_sql.parse(split_statements(self.sql), dialect="trino", default_schema=self.schema)
        except Exception as e:  # noqa: BLE001 - lineage is best effort; the task's own result is not
            self.log.debug("no OpenLineage for this SQL: %s", e)
            return OperatorLineage()

        def ds(t) -> Dataset:
            return Dataset(namespace=ns, name=".".join([t.database or catalog, t.schema or self.schema or "", t.name]))
        return OperatorLineage(inputs=[ds(t) for t in meta.in_tables], outputs=[ds(t) for t in meta.out_tables])

    def get_openlineage_facets_on_start(self):
        return self._lineage()

    def get_openlineage_facets_on_complete(self, task_instance):
        return self._lineage()

    def get_openlineage_facets_on_failure(self, task_instance):
        return self._lineage()

    def execute(self, context) -> Any:
        hook = CairnHook(self.cairn_conn_id)
        conn, user = hook.trino_connection(self.schema, self.token_ttl)
        self.log.info("Running on Trino as %s", user)
        rows: list = []
        try:
            cur = conn.cursor()
            for stmt in split_statements(self.sql):
                self.log.info("SQL: %s", stmt)
                cur.execute(stmt)
                rows = cur.fetchall()
                self.log.info("%s (query %s)", cur.stats.get("state", "done"), cur.query_id)
        finally:
            conn.close()
        return [list(r) for r in rows[:MAX_XCOM_ROWS]] if self.return_rows else None


class CairnDbtOperator(BaseOperator):
    """Runs ``cairn dbt <command>`` on a dbt project as the ``cairn`` connection's Cairn user: dbt on Trino
    with a short-lived token, and the run (docs, lineage, tests) recorded in Cairn. ``cairn dbt`` runs from
    its own virtualenv (``cairn_bin``), so dbt's dependencies never meet Airflow's. The task fails when dbt
    fails, and when Cairn could not record a successful run (exit 3)."""

    template_fields: Sequence[str] = ("command", "select", "exclude", "vars", "project_dir", "schema")
    ui_color = "#ff694a"

    def __init__(self, *, project_dir: str, command: str = "build", select: Optional[str] = None,
                 exclude: Optional[str] = None, vars: Optional[str] = None, schema: Optional[str] = None,
                 target_path: Optional[str] = None, cairn_conn_id: str = "cairn_default",
                 cairn_bin: str = os.environ.get("CAIRN_DBT_BIN", "/opt/cairn-dbt/bin/cairn"),
                 threads: Optional[int] = None, extra_args: Sequence[str] = (), env: Optional[dict] = None, **kwargs):
        super().__init__(**kwargs)
        self.project_dir, self.command, self.select, self.exclude, self.vars = project_dir, command, select, exclude, vars
        self.schema, self.target_path, self.cairn_conn_id, self.cairn_bin = schema, target_path, cairn_conn_id, cairn_bin
        self.threads, self.extra_args, self.extra_env = threads, list(extra_args), env or {}

    def args(self) -> list[str]:
        out = [self.cairn_bin, "dbt", *self.command.split(), "--project-dir", self.project_dir]
        for flag, value in (("--select", self.select), ("--exclude", self.exclude), ("--vars", self.vars),
                            ("--target-path", self.target_path)):
            if value:
                out += [flag, value]
        return out + self.extra_args

    def execute(self, context) -> int:
        hook = CairnHook(self.cairn_conn_id)
        env = {k: v for k, v in os.environ.items() if not k.startswith("AIRFLOW__")}
        env.update(hook.env())
        if self.schema:
            env["CAIRN_DBT_SCHEMA"] = self.schema
        if self.threads:
            env["CAIRN_DBT_THREADS"] = str(self.threads)
        # dbt writes artifacts, logs and packages into the project unless told otherwise; projects are
        # often read-only (an image, a git-sync volume), so each run gets its own scratch directory.
        run = str(context.get("run_id") or "run")
        scratch = os.path.join(os.environ.get("TMPDIR", "/tmp"), "cairn-dbt",
                               f"{self.dag_id}.{self.task_id}.{run}".replace("/", "_").replace(":", "_"))
        if not self.target_path:
            env.setdefault("CAIRN_DBT_TARGET_PATH", os.path.join(scratch, "target"))
        env.setdefault("DBT_LOG_PATH", os.path.join(scratch, "logs"))
        env.setdefault("DBT_PACKAGES_INSTALL_PATH", os.path.join(scratch, "dbt_packages"))
        env.setdefault("DBT_SEND_ANONYMOUS_USAGE_STATS", "false")
        env.update({k: str(v) for k, v in self.extra_env.items()})
        self.log.info("Running %s", " ".join(self.args()))
        proc = subprocess.Popen(self.args(), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                cwd=self.project_dir)
        assert proc.stdout is not None
        for line in proc.stdout:
            self.log.info("%s", line.rstrip())
        code = proc.wait()
        if code == 3:
            raise AirflowException("dbt succeeded but Cairn could not record the run (see the log)")
        if code != 0:
            raise AirflowException(f"dbt failed (exit {code})")
        return code
