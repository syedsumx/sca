"""The registry of configurable resources: every resource type a team keeps in git, declared once.

Each :class:`Kind` says how Cairn's API reads, creates, changes and deletes one resource type, and what its
fields are. Everything else is derived from it:

* ``cairn bundle`` sections (:mod:`cairn.iac.bundle`),
* the Terraform provider's resources and data sources (``terraform-provider-cairn/internal/provider/
  zz_generated_kinds.go``, written by ``python -m cairn.iac.terraform``; CI checks it is up to date),
* ``cairn export`` and ``GET /v1/iac/export`` (a live workspace as a bundle or as Terraform),
* ``GET /v1/iac/schema`` (this registry, for agents and the UI),
* the coverage gate (``tests/test_iac_coverage.py``): every API collection that can be created and deleted is
  either here or on the gate's reasoned exclusion list.

Adding a resource type is one :class:`Kind` entry in :data:`KINDS` (or in a module's ``IAC_KINDS``, listed in
:data:`MODULES`), then ``python -m cairn.iac.terraform`` to regenerate the provider.

Paths are templates: ``{id}`` is Cairn's id of the resource (the record's ``id`` field, see :attr:`Kind.id`),
``{<field>}`` is a field's value (a parent, such as a secret's scope). A kind whose read path names fields is a
child of the resource those fields name; its Terraform id is the parents' values and its own id joined by ``/``.
"""

from __future__ import annotations

import importlib
import re
from dataclasses import dataclass
from typing import Any, Optional

UNSET: Any = type("Unset", (), {"__repr__": lambda self: "UNSET"})()
TYPES = ("string", "bool", "int", "float", "list", "json")
FAMILIES = {
    "access": "Identity and access",
    "data": "Data and pipelines",
    "bi": "SQL and BI",
    "quality": "Quality and governance",
    "compute": "Compute and jobs",
    "ai": "AI and agents",
    "workspace": "Workspace",
    "operations": "Operations and observability",
}
PLACEHOLDER = re.compile(r"\{([a-z_]+)\}")
TF_RESERVED = {"connection", "count", "provider", "provisioner", "lifecycle", "depends_on", "for_each", "id"}


@dataclass(frozen=True)
class Field:
    """One attribute of a resource, named as Cairn's API names it."""

    name: str
    type: str = "string"            # string | bool | int | float | list (of strings) | json (a JSON document)
    required: bool = False
    force_new: bool = False         # changing it replaces the resource (Terraform); a bundle refuses the change
    computed: bool = False          # Cairn decides it when it is not given
    sensitive: bool = False         # never shown in plans or logs
    write_only: bool = False        # never returned by Cairn: sent, never read back or compared (secrets)
    default: Any = UNSET            # the value when not given (None: null); UNSET: optional and not compared
    description: str = ""
    tf: str = ""                    # Terraform's name for it, when Terraform reserves Cairn's (connection)
    read: str = ""                  # the field's name in Cairn's records, when it differs from the request's
    only_set: bool = False          # a JSON document Cairn always returns: compared only when declared
    once: bool = False              # only the create call returns it (a key): kept as created, never compared
    ref: str = ""                   # "kind.field": the value names that resource (exported as a reference)

    @property
    def tf_name(self) -> str:
        return self.tf or self.name

    @property
    def read_name(self) -> str:
        return self.read or self.name

    def schema(self) -> dict:
        out: dict[str, Any] = {"name": self.name, "type": self.type, "required": self.required,
                               "force_new": self.force_new, "computed": self.computed, "sensitive": self.sensitive,
                               "write_only": self.write_only, "description": self.description,
                               "terraform_name": self.tf_name}
        if self.default is not UNSET:
            out["default"] = self.default
        if self.read:
            out["read_as"] = self.read
        if self.once:
            out["once"] = True
        if self.ref:
            out["ref"] = self.ref
        return out


@dataclass(frozen=True)
class Op:
    """One API call: its method and path template, and how its body is made from the resource's fields."""

    method: str
    path: str
    omit: tuple[str, ...] = ()      # fields left out of the body (e.g. those already in the path)
    only: tuple[str, ...] = ()      # only these fields go in the body (empty: all of them)
    body: Optional[dict] = None     # fixed values added to the body (e.g. {"permission": None} to remove)
    no_body: bool = False
    query: tuple[str, ...] = ()     # fields sent as query parameters (not in the body)

    def schema(self) -> dict:
        out: dict[str, Any] = {"method": self.method, "path": self.path}
        for k in ("omit", "only"):
            if getattr(self, k):
                out[k] = list(getattr(self, k))
        if self.body is not None:
            out["body"] = self.body
        if self.no_body:
            out["no_body"] = True
        if self.query:
            out["query"] = list(self.query)
        return out


def op(spec: str, **kw) -> Op:
    method, path = spec.split(" ", 1)
    return Op(method, path, **kw)


@dataclass(frozen=True)
class Kind:
    """One resource type."""

    name: str                       # the bundle section (plural, snake_case)
    tf: str                         # the Terraform type, after cairn_
    family: str
    description: str
    fields: tuple[Field, ...]
    read: str                       # GET: the list (or, with list_key, the record holding it)
    create: Op
    update: Optional[Op]            # None: created once; any change replaces it
    delete: Optional[Op]            # None: destroying only forgets it (a setting that always exists)
    list_key: str = ""              # the list is under this key of the read answer ("." : the answer is the one record)
    id: str = "id"                  # the field of Cairn's records that is its id
    key: tuple[str, ...] = ("name",)   # the fields a bundle knows it by (unique among the kind's records)
    lookup: str = ""                # the data source's lookup attribute (default: the last key field)
    mine: bool = False              # the token user's own (others' shared ones are not managed)
    owner_field: str = "owner"
    match: tuple[tuple[str, Any], ...] = ()   # only records with these values are this kind (others are skipped)
    skip: tuple[tuple[str, str], ...] = ()    # records whose field starts with this are Cairn's own (not managed)
    order: int = 100                # created in this order, deleted in the reverse
    service: str = ""               # the Services attribute that serves it (None there: the kind is off)
    docs: str = ""                  # the operator manual's section on it
    example: Optional[dict] = None  # a valid resource (docs, the UI's samples, tests)
    example_update: Optional[dict] = None   # a change to it that is made in place
    never_replace: bool = False     # a bundle refuses a change instead of replacing it (people's work inside)
    bundle_fields: tuple[str, ...] = ()     # extra fields a bundle accepts (handled by the bundle itself)

    # ---- derived ---------------------------------------------------------------------------------------
    @property
    def parents(self) -> tuple[str, ...]:
        """The fields named in the read path: what this kind lives under."""
        return tuple(p for p in PLACEHOLDER.findall(self.read) if p != "id")

    @property
    def singleton(self) -> bool:
        return self.list_key == "."

    @property
    def lookup_field(self) -> str:
        return self.lookup or (self.key[-1] if self.key else "")

    def field(self, name: str) -> Field:
        for f in self.fields:
            if f.name == name:
                return f
        raise KeyError(f"{self.name} has no field {name}")

    @property
    def required(self) -> tuple[str, ...]:
        return tuple(f.name for f in self.fields if f.required)

    @property
    def defaults(self) -> dict:
        return {f.name: f.default for f in self.fields if f.default is not UNSET and not f.required}

    @property
    def optional(self) -> tuple[str, ...]:
        return tuple(f.name for f in self.fields if f.default is UNSET and not f.required)

    @property
    def write_only(self) -> tuple[str, ...]:
        return tuple(f.name for f in self.fields if f.write_only)

    @property
    def immutable(self) -> bool:
        return self.update is None

    def schema(self) -> dict:
        out = {"name": self.name, "terraform_type": f"cairn_{self.tf}", "family": self.family,
               "family_title": FAMILIES[self.family], "description": self.description, "read": self.read,
               "list_key": self.list_key, "id": self.id, "key": list(self.key), "lookup": self.lookup_field,
               "parents": list(self.parents), "mine": self.mine, "singleton": self.singleton,
               "immutable": self.immutable, "order": self.order,
               "create": self.create.schema(), "update": self.update.schema() if self.update else None,
               "delete": self.delete.schema() if self.delete else None,
               "fields": [f.schema() for f in self.fields],
               "docs": "/docs/administration.html#infrastructure-as-code"}
        if self.match:
            out["match"] = dict(self.match)
        if self.skip:
            out["skip"] = [list(x) for x in self.skip]
        if self.example is not None:
            out["example"] = self.example
        return out


def F(name: str, type: str = "string", **kw) -> Field:   # noqa: N802 - a short constructor for the tables
    return Field(name, type, **kw)


def R(name: str, type: str = "string", **kw) -> Field:   # noqa: N802 - a required field
    return Field(name, type, required=True, **kw)


def crud(path: str, *, id: str = "{id}", update: str = "PUT", **kw) -> dict:
    """The usual API: POST path, update path/{id}, DELETE path/{id}."""
    item = f"{path}/{id}"
    return {"read": path, "create": op(f"POST {path}"), "update": op(f"{update} {item}") if update else None,
            "delete": op(f"DELETE {item}", no_body=True), **kw}


# ---- the kinds ---------------------------------------------------------------------------------------------------
# The first fourteen are the original bundle and provider kinds; their order is the order they are created in.
CORE: list[Kind] = [
    Kind("policies", "policy", "access",
         "One of Cairn's own access policies (source cairn), set and deleted on its own: the rest of the policy "
         "bundle stays as it is. Who may use a warehouse is such a policy (USE_WAREHOUSE on urn:li:warehouse:<name>).",
         (R("id", force_new=True, tf="policy_id", description="the policy's id (letters, digits, _ . : -)"),
          F("description", default=""),
          R("effect", description="allow, deny or obligation"),
          F("priority", "int", default=0, description="higher is decided first"),
          R("actions", "list", description="privileges, e.g. VIEW_DATASET_PROFILE, USE_WAREHOUSE, or *"),
          F("target", "json", computed=True, description='{"resource_types": ["dataset"], "resource_urns": [...]}'),
          F("condition", "json", default=None, description='{"attr": "subject.groups", "op": "contains", "value": "analysts"}'),
          F("obligations", "json", computed=True, description='[{"type": "mask_columns", ...}]'),
          F("enabled", "bool", default=True)),
         read="/governance/policies", list_key="policies", match=(("source", "cairn"),), key=("id",),
         create=op("PUT /governance/policies/{id}"), update=op("PUT /governance/policies/{id}"),
         delete=op("DELETE /governance/policies/{id}", no_body=True), order=10, service="pdp",
         example={"id": "analysts-read", "effect": "allow", "actions": ["VIEW_DATASET_PROFILE"], "priority": 10,
                  "target": {"resource_types": ["dataset"]},
                  "condition": {"attr": "subject.groups", "op": "contains", "value": "analysts"}},
         example_update={"priority": 20}),
    Kind("connections", "connection", "data",
         "An ingestion source: a connector, its form values (secret fields name a Cairn secret, never its value), "
         "where it runs and when.",
         (R("name"),
          R("connector", force_new=True, description="a gallery connector id (postgres, snowflake, ...), or custom"),
          F("values", "json", default={}, description='{"host_port": "db:5432", "database": "finance", "password": "FINANCE_PG_PASSWORD"}'),
          F("advanced", "json", default={}, description="connector options beyond the form"),
          F("recipe", "json", only_set=True, description="custom connector only: the whole recipe"),
          F("schedule", "json", default=None, description='{"interval": "0 2 * * *", "timezone": "UTC"}'),
          F("executor", default="default", description="default, or an agent pool"),
          F("owners", "list", computed=True, description="user URNs; the token's user when not given")),
         read="/sources", id="urn", create=op("POST /sources"), update=op("PUT /sources/{id}/detail"),
         delete=op("DELETE /sources/{id}/detail", no_body=True), order=20, service="sources"),
    Kind("maintenance", "maintenance_policy", "data",
         "Table maintenance (compaction, snapshot expiry, orphan cleanup) for *, a schema or schema.table.",
         (R("scope", force_new=True),
          F("optimize", "bool", default=True), F("optimize_threshold", default="128MB"),
          F("expire_days", "int", default=None), F("orphan_days", "int", default=None),
          F("every_hours", "int", default=24), F("enabled", "bool", default=True)),
         read="/maintenance/policies", id="scope", key=("scope",),
         create=op("PUT /maintenance/policies/{scope}", omit=("scope",)),
         update=op("PUT /maintenance/policies/{scope}", omit=("scope",)),
         delete=op("DELETE /maintenance/policies/{scope}", no_body=True), order=30, service="maintenance",
         example={"scope": "raw", "expire_days": 7}, example_update={"every_hours": 12}),
    Kind("pipes", "pipe", "data", "Files landing in a Cairn Store prefix, loaded into a lakehouse table once each.",
         (R("name"), R("bucket"), F("prefix", default=""), R("format", description="csv, json (lines) or parquet"),
          R("target", description="namespace.table"), F("create_table", "bool", default=False),
          F("enabled", "bool", default=True)),
         **crud("/v1/pipes"), order=40, service="pipes",
         example={"name": "orders-files", "bucket": "landing", "prefix": "orders/", "format": "csv",
                  "target": "raw.orders", "create_table": True}, example_update={"prefix": "orders/v2/"}),
    Kind("streams", "stream", "data",
         "A Kafka topic (Debezium change events included) written into a lakehouse table, exactly once.",
         (R("name"), R("topic"), F("format", default="json", description="json or debezium"),
          F("encoding", default="json", description="json, or avro (Confluent wire format)"),
          F("mode", default="append", computed=True, description="append or upsert (debezium: always upsert)"),
          F("key", "list", default=[]), R("target"), F("create_table", "bool", default=False),
          F("evolve", "bool", default=False), F("start", default="earliest"),
          F("source", default=None, description="the dataset URN the topic carries (lineage)"),
          F("batch_rows", "int", default=5000), F("batch_seconds", "int", default=10), F("enabled", "bool", default=True)),
         **crud("/v1/streams"), order=50, service="streams",
         example={"name": "customers-cdc", "topic": "pg.public.customers", "format": "debezium", "key": ["id"],
                  "target": "cdc.customers", "create_table": True}, example_update={"batch_rows": 100}),
    Kind("derived", "derived_table", "data", "A SELECT kept as a lakehouse table, refreshed on Trino as the token's user.",
         (R("name"), R("sql", description="one SELECT, without a trailing semicolon"), R("target", description="schema.table"),
          F("mode", default="full", description="full, append or merge"), F("watermark", default=None),
          F("key", "list", default=[]), F("every_minutes", "int", default=None), F("on_change", "bool", default=False),
          F("enabled", "bool", default=True)),
         **crud("/v1/derived"), order=60, service="derived", bundle_fields=("owner",),
         example={"name": "revenue", "target": "marts.revenue", "on_change": True,
                  "sql": "SELECT region, sum(amount) AS total FROM raw.orders GROUP BY region"},
         example_update={"every_minutes": 60}),
    Kind("vectors", "vector_index", "ai",
         "Vector search over a lakehouse table's text, kept in step with the table and searched under Cairn's policies.",
         (R("name", force_new=True), R("source", force_new=True, description="namespace.table"),
          R("key", description="the source's key column"), R("text", description="the source's text column"),
          F("chunk_chars", "int", default=1500), F("every_minutes", "int", default=None), F("enabled", "bool", default=True)),
         **crud("/v1/vectors"), order=70, service="vectors",
         example={"name": "articles", "source": "support.articles", "key": "id", "text": "body", "every_minutes": 60},
         example_update={"every_minutes": 30}),
    Kind("input_tables", "input_table", "bi",
         "A lakehouse table people type into, written on Trino as the person writing. Its reviewers (changes wait "
         "for one of them) change in place; any other change replaces it in Terraform's plan, and destroying it "
         "unregisters it (the table and its rows stay).",
         (R("table", force_new=True, description="schema.table, lowercase"),
          R("connection", force_new=True, tf="connection_name", description="a Trino connection that runs as the user"),
          R("columns", "json", force_new=True, description='[{"name": "region", "type": "text"}]'),
          F("description", default="", force_new=True),
          F("reviewers", "list", default=[], description="people and groups who approve changes before they land")),
         read="/sql/input-tables", create=op("POST /sql/input-tables"),
         update=op("PUT /sql/input-tables/{id}/approval", only=("reviewers",)),
         delete=op("DELETE /sql/input-tables/{id}", no_body=True), key=("table",), mine=True, owner_field="created_by",
         never_replace=True, order=80, service="input_tables"),
    Kind("queries", "saved_query", "bi", "A saved SQL query (read-only), with its chart; the token user's own.",
         (R("name"), R("sql"), R("connection", tf="connection_name", description="the SQL connection it runs on"),
          F("description", default=""), F("shared", "bool", default=False),
          F("chart", "json", default={}, description='{"type": "bar", "x": "region", "y": ["total"]}')),
         **crud("/sql/saved"), mine=True, order=90, service="sql_workspace",
         example={"name": "revenue-by-region", "connection": "lakehouse", "sql": "SELECT 1 AS n"},
         example_update={"description": "Revenue"}),
    Kind("metrics", "metric", "bi",
         "A business measure defined once (a workbook aggregate over one table), the same number in every query, "
         "workbook and dashboard; run as the person asking.",
         (R("name", force_new=True, description="lowercase letters, digits and _"), R("label"),
          R("connection", tf="connection_name"), R("table", description="schema.table"),
          R("measure", description="a workbook aggregate, e.g. Sum([amount])"), F("description", default=""),
          F("filters", "json", default=[], only_set=True, description='[{"column": "status", "op": "=", "value": "paid"}]'),
          F("time_column", default=None), F("dimensions", "list", default=[])),
         **crud("/sql/metrics", id="{name}"), id="name", mine=True, order=100, service="metrics",
         example={"name": "revenue", "label": "Revenue", "connection": "wh", "table": "main.orders",
                  "measure": "Sum([amount])", "time_column": "day", "dimensions": ["region"]},
         example_update={"label": "Revenue (USD)"}),
    Kind("workbooks", "workbook", "bi", "A spreadsheet workbook: elements compiled to SQL and run as the person looking.",
         (R("name"), R("connection", tf="connection_name"), R("spec", "json"), F("description", default=""),
          F("shared", "bool", default=False)),
         **crud("/sql/workbooks"), mine=True, order=110, service="workbooks",
         example={"name": "orders", "connection": "wh", "spec": {"elements": [
             {"source": {"table": "main.orders"}, "group_by": ["region"], "aggregates": [{"name": "n", "formula": "Count()"}]}]}},
         example_update={"description": "Orders by region"}),
    Kind("dashboards", "dashboard", "bi",
         "Saved queries, metrics and workbook elements shown together, each run as its viewer; ops tiles (PromQL, "
         "LogQL, TraceQL, SLOs, alerts, PostgreSQL instances) next to them; with its controls (filters, $variables, "
         "the time range) and its actions (forms and buttons that change things, run as the person clicking).",
         (R("name"), F("description", default=""), F("shared", "bool", default=False),
          R("tiles", "json", description='[{"query": "<saved query id>", "title": "...", "width": 1}] or an ops tile: '
                                         '{"promql": {"queries": [{"expr", "legend"}]}, "view", "display"}; also '
                                         "logql, traces, slo, alerts, postgres"),
          F("controls", "json", default=[], only_set=True,
            description='filters (values, range, date_range, text), {"kind": "variable"} ($name in ops queries) and '
                        '{"kind": "time", "default": "1h", "refresh": 30}'),
          F("actions", "json", default=[], only_set=True,
            description='[{"name": "approve", "kind": "input_update", "input_table": "<id>", "set": {...}}]: '
                        "input_row, input_update, workflow or webhook; matched by name")),
         **crud("/sql/dashboards"), mine=True, order=120, service="sql_boards",
         example={"name": "sales", "tiles": [
             {"connection": "wh", "title": "Orders by region", "element": {
                 "source": {"table": "main.orders"}, "group_by": ["region"], "aggregates": [{"name": "n", "formula": "Count()"}]}},
             {"title": "Requests / s", "view": "timeseries", "display": {"unit": "reqps"}, "promql": {"queries": [
                 {"expr": 'sum by (service_name) (rate(traces_span_metrics_calls_total{service_name="$service"}'
                          "[$__rate_interval]))", "legend": "{{service_name}}"}]}},
             {"title": "Error logs", "view": "logs", "logql": {"queries": [
                 {"expr": '{service_name="$service"} |= "error"'}], "limit": 50}},
             {"title": "Slow requests", "traces": {"query": '{ resource.service.name = "$service" && duration > 500ms }',
                                                   "limit": 10}},
             {"title": "API availability", "view": "stat", "slo": {"id": "api-availability"}},
             {"title": "Firing", "alerts": {"state": "firing", "labels": {"service": "$service"}}},
             {"title": "Database", "postgres": {"instance": "default"}}],
             "controls": [{"id": "time", "kind": "time", "default": "1h", "refresh": 30},
                          {"id": "service", "kind": "variable", "label": "Service", "default": "cairn-api",
                           "query": {"source": "label_values", "label": "service_name"}}],
             "actions": [{"name": "flag", "kind": "webhook", "label": "Flag a problem",
                          "url": "https://hooks.example.com/cairn/flag", "fields": [{"name": "note", "type": "text"}]}]},
         example_update={"description": "Sales, by region"}),
    Kind("alerts", "sql_alert", "bi",
         "A saved query checked on a schedule as its owner; its owner and recipients are told when the condition "
         "starts and stops holding.",
         (R("name"), R("query", ref="queries.id", description="the saved query's id (a bundle names it)"),
          F("measure", default="rows", description="rows, or a column of the first row"),
          R("op", description="> >= < <= == !="), R("value", "float"), F("every_minutes", "int", default=60),
          F("recipients", "list", default=[]), F("enabled", "bool", default=True)),
         **crud("/sql/alerts"), mine=True, order=130, service="sql_boards"),
    Kind("workflows", "sql_workflow", "bi",
         "Steps that depend on each other (derived-table refreshes, checks, report queries), on a schedule, run as "
         "their owner.",
         (R("name"), R("steps", "json"), F("cron", default=None, description="five fields, UTC"),
          F("recipients", "list", default=[]), F("notify", default="failure", description="failure or always"),
          F("enabled", "bool", default=True)),
         **crud("/v1/sql-workflows"), mine=True, order=140, service="sql_workflows"),
]

# Modules that declare more kinds (``IAC_KINDS``), so a feature's resources live beside it.
MODULES: tuple[str, ...] = ()


def _collect() -> list[Kind]:
    out = list(CORE)
    from cairn.iac import kinds as more

    out += list(more.KINDS)
    for m in MODULES:
        out += list(importlib.import_module(m).IAC_KINDS)
    return sorted(out, key=lambda k: k.order)


def check(kinds: list[Kind]) -> None:
    """The registry's own rules, so a bad entry fails at import (and in CI), not in a user's apply."""
    seen, tfs = set(), set()
    for k in kinds:
        assert re.match(r"^[a-z][a-z0-9_]*$", k.name) and k.name not in seen, k.name
        assert re.match(r"^[a-z][a-z0-9_]*$", k.tf) and k.tf not in tfs, k.tf
        seen.add(k.name)
        tfs.add(k.tf)
        assert k.family in FAMILIES, (k.name, k.family)
        names = [f.name for f in k.fields]
        assert len(set(names)) == len(names), k.name
        tfn = [f.tf_name for f in k.fields]
        assert len(set(tfn)) == len(tfn), k.name
        for f in k.fields:
            assert f.type in TYPES, (k.name, f.name)
            assert f.tf_name not in TF_RESERVED, (k.name, f.name, "give it tf=")
            assert not (f.required and f.default is not UNSET), (k.name, f.name)
            if f.ref:
                other, attr = f.ref.split(".")
                assert other in {x.name for x in kinds} and attr in {x.name for x in kind(other, kinds).fields} | {"id"}, \
                    (k.name, f.name, f.ref)
        for p in k.parents:
            assert p in names and k.field(p).force_new and k.field(p).required, (k.name, p)
        for key in k.key:
            assert key in names, (k.name, key)
        assert k.key[:len(k.parents)] == k.parents, (k.name, "a child's key starts with its parents")
        assert k.key or k.singleton, (k.name, "only a setting has no key")
        assert not k.lookup_field or k.lookup_field in names, k.name
        assert k.create is not None
        for o in (k.create, k.update, k.delete):
            if o is None:
                continue
            for q in o.query:
                assert q in names, (k.name, o.path, q)
            for p in PLACEHOLDER.findall(o.path):
                assert p == "id" or p in names, (k.name, o.path, p)
        if k.update is None:
            assert all(f.force_new or f.computed for f in k.fields), (k.name, "no update: every field forces new")


_KINDS: Optional[list[Kind]] = None


def kinds() -> list[Kind]:
    global _KINDS
    if _KINDS is None:
        out = _collect()
        check(out)
        _KINDS = out
    return _KINDS


def kind(name: str, among: Optional[list[Kind]] = None) -> Kind:
    for k in among if among is not None else kinds():
        if k.name == name or k.tf == name:
            return k
    raise KeyError(name)


def by_name() -> dict[str, Kind]:
    return {k.name: k for k in kinds()}


def schema() -> dict:
    """The registry as JSON (``GET /v1/iac/schema``)."""
    return {"version": 1, "families": FAMILIES, "kinds": [k.schema() for k in kinds()]}
