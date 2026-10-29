"""The connector gallery: each connector's setup form, and the recipe built from it.

A form field writes one value into ``source.config`` at its ``path`` (dotted; ``a[].b`` makes a list
of objects). Secret fields take the *name* of a secret, never its value: the recipe holds a
``${NAME}`` reference, resolved by the executor that runs it (Cairn Core's secret store, or the
secrets backend of the in-VPC agent pool). ``advanced`` merges extra options into the config, and the
``custom`` connector takes a whole recipe; inline credentials are refused either way.
"""

from __future__ import annotations

import copy
import re
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

SECRET_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]{1,63}$")
REF = re.compile(r"^\$\{([A-Za-z][A-Za-z0-9_]{1,63})\}$")


class SourceError(ValueError):
    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.status = status


class FormField(BaseModel):
    key: str
    label: str
    kind: Literal["text", "secret", "number", "bool", "select", "list"] = "text"
    path: str
    required: bool = False
    default: Any = None
    help: str = ""
    options: list[str] = Field(default_factory=list)


class Connector(BaseModel):
    id: str
    name: str
    source_type: str
    category: Literal["warehouse", "database", "lake", "bi", "transformation", "streaming", "other"]
    description: str
    fields: list[FormField]
    defaults: dict[str, Any] = Field(default_factory=dict)


def _f(key: str, label: str, kind: str = "text", path: Optional[str] = None, required: bool = False, **kw) -> FormField:
    return FormField(key=key, label=label, kind=kind, path=path or key, required=required, **kw)


_SQL = [_f("host_port", "Host and port", required=True, help="e.g. db.internal:5432"),
        _f("database", "Database"), _f("username", "User", required=True),
        _f("password", "Password secret", "secret", required=True),
        _f("include_views", "Include views", "bool", default=True),
        _f("profiling", "Profile tables", "bool", path="profiling.enabled", default=False),
        _f("schemas", "Schemas to include", "list", path="schema_pattern.allow",
           help="regular expressions; empty for all")]

CONNECTORS: list[Connector] = [
    Connector(id="snowflake", name="Snowflake", source_type="snowflake", category="warehouse",
              description="Databases, schemas, tables, views, lineage and usage from Snowflake.",
              fields=[_f("account_id", "Account identifier", required=True, help="e.g. xy12345.eu-west-1"),
                      _f("warehouse", "Warehouse", required=True), _f("username", "User", required=True),
                      _f("password", "Password secret", "secret", required=True), _f("role", "Role"),
                      _f("databases", "Databases to include", "list", path="database_pattern.allow"),
                      _f("lineage", "Table lineage", "bool", path="include_table_lineage", default=True),
                      _f("usage", "Usage statistics", "bool", path="include_usage_stats", default=True),
                      _f("profiling", "Profile tables", "bool", path="profiling.enabled", default=False)]),
    Connector(id="bigquery", name="BigQuery", source_type="bigquery", category="warehouse",
              description="Projects, datasets, tables, lineage and usage from Google BigQuery.",
              fields=[_f("project_ids", "Projects", "list", required=True),
                      _f("client_email", "Service account email", path="credential.client_email", required=True),
                      _f("project_id", "Service account project", path="credential.project_id", required=True),
                      _f("private_key_id", "Private key id secret", "secret", path="credential.private_key_id",
                         required=True),
                      _f("private_key", "Private key secret", "secret", path="credential.private_key", required=True),
                      _f("client_id", "Client id", path="credential.client_id"),
                      _f("lineage", "Table lineage", "bool", path="include_table_lineage", default=True),
                      _f("usage", "Usage statistics", "bool", path="include_usage_statistics", default=True)]),
    Connector(id="databricks", name="Databricks", source_type="unity-catalog", category="lake",
              description="Unity Catalog metastores, catalogs, tables, column lineage and notebooks.",
              fields=[_f("workspace_url", "Workspace URL", required=True),
                      _f("token", "Access token secret", "secret", required=True),
                      _f("warehouse_id", "SQL warehouse id"),
                      _f("column_lineage", "Column lineage", "bool", path="include_column_lineage", default=True),
                      _f("catalogs", "Catalogs to include", "list", path="catalog_pattern.allow")]),
    Connector(id="redshift", name="Amazon Redshift", source_type="redshift", category="warehouse",
              description="Schemas, tables, views, lineage and usage from Redshift.",
              fields=[*_SQL[:4], _f("lineage", "Table lineage", "bool", path="include_table_lineage", default=True),
                      *_SQL[4:]]),
    Connector(id="postgres", name="PostgreSQL", source_type="postgres", category="database",
              description="Schemas, tables and views from PostgreSQL.", fields=list(_SQL)),
    Connector(id="mysql", name="MySQL", source_type="mysql", category="database",
              description="Databases, tables and views from MySQL or MariaDB.", fields=list(_SQL)),
    Connector(id="mssql", name="SQL Server", source_type="mssql", category="database",
              description="Databases, schemas, tables and views from Microsoft SQL Server.", fields=list(_SQL)),
    Connector(id="trino", name="Trino", source_type="trino", category="database",
              description="Catalogs, schemas and tables through Trino or Starburst.",
              fields=[*_SQL[:1], _f("database", "Catalog", required=True), *_SQL[2:]]),
    Connector(id="dbt", name="dbt Core", source_type="dbt", category="transformation",
              description="Models, sources, tests and lineage from a dbt project's artifacts.",
              fields=[_f("manifest_path", "manifest.json path or URL", required=True),
                      _f("catalog_path", "catalog.json path or URL", required=True),
                      _f("run_results_paths", "run_results.json paths", "list"),
                      _f("target_platform", "Warehouse", "select", required=True,
                         options=["snowflake", "bigquery", "databricks", "redshift", "postgres"])]),
    Connector(id="dbt-cloud", name="dbt Cloud", source_type="dbt-cloud", category="transformation",
              description="Models, tests and lineage from a dbt Cloud job's latest run.",
              fields=[_f("account_id", "Account id", "number", required=True),
                      _f("project_id", "Project id", "number", required=True),
                      _f("job_id", "Job id", "number", required=True),
                      _f("token", "API token secret", "secret", required=True),
                      _f("target_platform", "Warehouse", "select", required=True,
                         options=["snowflake", "bigquery", "databricks", "redshift", "postgres"])]),
    Connector(id="looker", name="Looker", source_type="looker", category="bi",
              description="Dashboards, looks, explores and their lineage to tables.",
              fields=[_f("base_url", "Base URL", required=True), _f("client_id", "Client id", required=True),
                      _f("client_secret", "Client secret", "secret", required=True)]),
    Connector(id="tableau", name="Tableau", source_type="tableau", category="bi",
              description="Workbooks, dashboards, data sources and their lineage.",
              fields=[_f("connect_uri", "Server URL", required=True), _f("site", "Site"),
                      _f("token_name", "Personal access token name", required=True),
                      _f("token_value", "Token secret", "secret", required=True)]),
    Connector(id="powerbi", name="Power BI", source_type="powerbi", category="bi",
              description="Workspaces, reports, dashboards and datasets from Power BI.",
              fields=[_f("tenant_id", "Tenant id", required=True), _f("client_id", "Client id", required=True),
                      _f("client_secret", "Client secret", "secret", required=True)]),
    Connector(id="kafka", name="Kafka", source_type="kafka", category="streaming",
              description="Topics and their schemas from Kafka and a schema registry.",
              fields=[_f("bootstrap", "Bootstrap servers", path="connection.bootstrap", required=True),
                      _f("schema_registry_url", "Schema registry URL", path="connection.schema_registry_url"),
                      _f("topics", "Topics to include", "list", path="topic_patterns.allow")]),
    Connector(id="s3", name="Amazon S3", source_type="s3", category="lake",
              description="Datasets in S3 (Parquet, CSV, JSON, Avro), with schemas inferred from files.",
              fields=[_f("paths", "Path specs", "list", path="path_specs[].include", required=True,
                         help="e.g. s3://bucket/sales/{table}/*.parquet"),
                      _f("aws_region", "Region", path="aws_config.aws_region", required=True)]),
    Connector(id="glue", name="AWS Glue", source_type="glue", category="lake",
              description="Databases and tables from the Glue Data Catalog.",
              fields=[_f("aws_region", "Region", required=True)]),
    Connector(id="mongodb", name="MongoDB", source_type="mongodb", category="database",
              description="Databases and collections, with schemas inferred from documents.",
              fields=[_f("connect_uri", "Connection URI", required=True), _f("username", "User"),
                      _f("password", "Password secret", "secret")]),
]
BY_ID = {c.id: c for c in CONNECTORS}
BY_TYPE = {c.source_type: c for c in CONNECTORS}


def connector_for(source_type: str) -> Optional[Connector]:
    return BY_TYPE.get(source_type)


def _set(config: dict, path: str, value: Any) -> None:
    if "[]." in path:                                    # a list of objects, one per value
        head, leaf = path.split("[].", 1)
        _set(config, head, [{leaf: v} for v in value])
        return
    parts = path.split(".")
    node = config
    for p in parts[:-1]:
        node = node.setdefault(p, {})
        if not isinstance(node, dict):
            raise SourceError(f"{path} conflicts with another option")
    node[parts[-1]] = value


def _get(config: dict, path: str) -> Any:
    if "[]." in path:
        head, leaf = path.split("[].", 1)
        items = _get(config, head)
        return [i.get(leaf) for i in items if isinstance(i, dict)] if isinstance(items, list) else None
    node: Any = config
    for p in path.split("."):
        if not isinstance(node, dict) or p not in node:
            return None
        node = node[p]
    return node


def _merge(base: dict, extra: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in extra.items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def secret_refs(value: Any) -> list[str]:
    """Names of the ``${NAME}`` secrets a recipe refers to."""
    if isinstance(value, str):
        return re.findall(r"\$\{([A-Za-z][A-Za-z0-9_]{1,63})\}", value)
    if isinstance(value, dict):
        return [n for v in value.values() for n in secret_refs(v)]
    if isinstance(value, list):
        return [n for v in value for n in secret_refs(v)]
    return []


def build_recipe(connector_id: str, values: dict[str, Any], advanced: Optional[dict] = None,
                 recipe: Optional[dict] = None) -> dict:
    """The recipe for a connector's form (or, for ``custom``, the recipe given), checked for inline
    credentials."""
    from cairn.agent.gateway import find_inline_secrets

    if connector_id == "custom":
        if not isinstance(recipe, dict) or not isinstance((recipe.get("source") or {}).get("type"), str):
            raise SourceError("a custom source needs a recipe with source.type")
        out = {"source": {"type": recipe["source"]["type"], "config": dict(recipe["source"].get("config") or {})}}
        for k in ("transformers", "pipeline_name", "flags"):
            if k in recipe:
                out[k] = recipe[k]
    else:
        c = BY_ID.get(connector_id)
        if c is None:
            raise SourceError(f"unknown connector {connector_id!r}")
        config = copy.deepcopy(c.defaults)
        unknown = set(values) - {f.key for f in c.fields}
        if unknown:
            raise SourceError(f"{c.name} has no field {sorted(unknown)[0]!r}")
        for f in c.fields:
            v = values.get(f.key, f.default)
            if v in (None, "", []):
                if f.required:
                    raise SourceError(f"{f.label} is required")
                continue
            if f.kind == "secret":
                name = str(v).strip()
                m = REF.match(name)
                name = m.group(1) if m else name
                if not SECRET_NAME.match(name):
                    raise SourceError(f"{f.label}: give the name of a secret (letters, digits, _), not its value")
                v = f"${{{name}}}"
            elif f.kind == "number":
                try:
                    v = int(v)
                except (TypeError, ValueError):
                    raise SourceError(f"{f.label} must be a number") from None
            elif f.kind == "bool":
                v = bool(v)
            elif f.kind == "list":
                v = [str(x).strip() for x in (v if isinstance(v, list) else str(v).split(",")) if str(x).strip()]
            elif f.kind == "select" and f.options and v not in f.options:
                raise SourceError(f"{f.label} is one of {f.options}")
            _set(config, f.path, v)
        out = {"source": {"type": c.source_type, "config": config}}
    if advanced:
        if not isinstance(advanced, dict):
            raise SourceError("advanced options are a mapping")
        out["source"]["config"] = _merge(out["source"]["config"], advanced)
    leaked = find_inline_secrets(out)
    if leaked:
        raise SourceError(f"inline credentials at {', '.join(leaked)}: store them as secrets and use ${{NAME}}")
    return out


def form_values(recipe: dict) -> tuple[str, dict[str, Any], dict]:
    """``(connector id, form values, advanced options)`` for an existing recipe."""
    source = recipe.get("source") or {}
    c = connector_for(source.get("type", ""))
    if c is None:
        return "custom", {}, {}
    config = copy.deepcopy(source.get("config") or {})
    values: dict[str, Any] = {}
    for f in c.fields:
        v = _get(config, f.path)
        if v is None:
            continue
        if f.kind == "secret" and isinstance(v, str) and (m := REF.match(v)):
            v = m.group(1)
        values[f.key] = v
    try:
        known = build_recipe(c.id, values)["source"]["config"]
    except SourceError:                                   # made elsewhere, not from this form
        return "custom", {}, {}
    advanced = _diff(config, known)
    return c.id, values, advanced


def _diff(full: dict, known: dict) -> dict:
    out = {}
    for k, v in full.items():
        if k not in known:
            out[k] = v
        elif isinstance(v, dict) and isinstance(known[k], dict):
            if d := _diff(v, known[k]):
                out[k] = d
        elif v != known[k]:
            out[k] = v
    return out
