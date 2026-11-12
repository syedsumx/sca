"""NiFi flow definitions as Cairn keeps them: checked, prepared for a deployment, and read for lineage.

A flow definition is NiFi 2's own JSON (what *Download flow definition* gives, a versioned flow snapshot:
``flowContents``, ``parameterContexts``, ...). Cairn:

* **refuses secrets in it**: a sensitive property with a value (it must reference a sensitive parameter, ``#{...}``),
  a sensitive parameter with a value, or any value that looks like a credential. Sensitive values come only from
  Cairn secret scopes, set into NiFi's parameter context when the flow is deployed, and NiFi never exports them;
* **prepares it** (:func:`prepare`): the root group gets a parameter context of Cairn's (``cairn-<flow>``) declaring
  the flow's parameters, its sensitive ones (without values) and, for a flow that lands in the lakehouse, Cairn's
  ``cairn.lakehouse.*`` parameters;
* **reads its lineage** (:func:`lineage`): the datasets processors read and write, from their settings (database
  tables, Kafka topics, buckets, folders, Iceberg tables), per process group;
* offers the **lakehouse sink** (:func:`lakehouse_template`): records into an Iceberg table of Cairn's lakehouse with
  ``PutIcebergRecord`` and NiFi's Iceberg REST catalog client against Cairn Catalog, signed in as the flow's owner
  (a token of the owner's lakehouse session, renewed by token exchange and by Cairn; storage keys vended per table).
"""

from __future__ import annotations

import copy
import json
import re
import uuid
from typing import Any, Iterator, Optional
from urllib.parse import urlsplit

from cairn.airflow_deploy.spec import SECRET_VALUE, SpecError

MAX_BYTES = 8 * 1024 * 1024
PARAM_REF = re.compile(r"#\{'?([^}']+)'?\}")
LAKEHOUSE_PARAMS = ("cairn.lakehouse.catalog_uri", "cairn.lakehouse.token_uri", "cairn.lakehouse.token",
                    "cairn.lakehouse.store_endpoint", "cairn.lakehouse.namespace", "cairn.lakehouse.table")
LAKEHOUSE_SENSITIVE = ("cairn.lakehouse.token",)
GROUP_KEYS = ("processGroups", "processors", "controllerServices", "connections", "inputPorts", "outputPorts",
              "funnels", "labels", "remoteProcessGroups")

# Processors whose settings name what they read or write: type suffix -> (direction, kind).
SOURCES = {"QueryDatabaseTable": "table", "QueryDatabaseTableRecord": "table", "GenerateTableFetch": "table",
           "ExecuteSQL": "sql", "ExecuteSQLRecord": "sql", "CaptureChangeMySQL": "mysql-cdc",
           "ConsumeKafka": "kafka", "ConsumeKafkaRecord_2_6": "kafka", "ConsumeKafka_2_6": "kafka",
           "ListS3": "s3", "FetchS3Object": "s3", "GetFile": "file", "ListFile": "file", "FetchFile": "file",
           "GetMongo": "mongo", "GetMongoRecord": "mongo"}
SINKS = {"PutDatabaseRecord": "table", "PutSQL": "sql", "PublishKafka": "kafka", "PublishKafkaRecord_2_6": "kafka",
         "PublishKafka_2_6": "kafka", "PutS3Object": "s3", "PutFile": "file", "PutIcebergRecord": "iceberg",
         "PutIceberg": "iceberg", "PutMongo": "mongo", "PutMongoRecord": "mongo"}


def _walk(group: dict, path: tuple[str, ...] = ()) -> Iterator[tuple[tuple[str, ...], dict]]:
    yield path, group
    for g in group.get("processGroups") or []:
        if isinstance(g, dict):
            yield from _walk(g, (*path, str(g.get("name") or g.get("identifier") or "group")))


def components(defn: dict) -> Iterator[tuple[tuple[str, ...], str, dict]]:
    """Every processor and controller service: (group path, kind, component)."""
    for path, g in _walk(defn.get("flowContents") or {}):
        for kind in ("processors", "controllerServices"):
            for c in g.get(kind) or []:
                if isinstance(c, dict):
                    yield path, kind, c


def _short(t: Any) -> str:
    return str(t or "").rsplit(".", 1)[-1]


def check_definition(defn: Any) -> dict:
    """The definition as Cairn keeps it, or a SpecError saying what is wrong (secrets above all)."""
    if not isinstance(defn, dict):
        raise SpecError("definition is a NiFi flow definition (a JSON object with flowContents)")
    try:
        raw = json.dumps(defn)
    except (TypeError, ValueError):
        raise SpecError("definition is JSON")
    if len(raw) > MAX_BYTES:
        raise SpecError(f"definition is up to {MAX_BYTES // (1024 * 1024)} MiB")
    fc = defn.get("flowContents")
    if not isinstance(fc, dict) or not isinstance(fc.get("processors", []), list):
        raise SpecError("definition.flowContents is the flow's process group (NiFi's Download flow definition)")
    for path, _kind, c in components(defn):
        where = "/".join(path) or "(root)"
        name = c.get("name") or _short(c.get("type"))
        if not c.get("type") or not isinstance(c.get("properties", {}), dict):
            raise SpecError(f"{where}: {name} has no type or its properties are not an object")
        descriptors = c.get("propertyDescriptors") or {}
        for prop, value in (c.get("properties") or {}).items():
            if value is None:
                continue
            if not isinstance(value, str):
                raise SpecError(f"{where}: {name}.{prop} is not text")
            sensitive = bool((descriptors.get(prop) or {}).get("sensitive"))
            if sensitive and not re.fullmatch(r"\s*#\{'?[^}']+'?\}\s*", value):
                raise SpecError(f"{where}: {name}.{prop} is sensitive: reference a sensitive parameter (#{{name}}) "
                                "whose value comes from a secret scope, never a value in the flow")
            if SECRET_VALUE.search(value):
                raise SpecError(f"{where}: {name}.{prop} looks like a credential: put it in a secret scope and "
                                "reference it as a sensitive parameter")
    for cname, ctx in (defn.get("parameterContexts") or {}).items():
        for p in (ctx or {}).get("parameters") or []:
            if not isinstance(p, dict):
                continue
            pname = str(p.get("name") or "")
            if p.get("sensitive") and p.get("value") not in (None, ""):
                raise SpecError(f"parameter context {cname}: {pname} is sensitive and has a value: sensitive values "
                                "come only from secret scopes (the flow's secrets)")
            if isinstance(p.get("value"), str) and SECRET_VALUE.search(p["value"]):
                raise SpecError(f"parameter context {cname}: {pname} looks like a credential")
    return defn


def referenced_parameters(defn: dict) -> set[str]:
    out: set[str] = set()
    for _, _, c in components(defn):
        for v in (c.get("properties") or {}).values():
            if isinstance(v, str):
                out.update(m.group(1) for m in PARAM_REF.finditer(v))
    return out


def sensitive_references(defn: dict) -> set[str]:
    """Parameters referenced from sensitive properties: they must be sensitive (NiFi refuses otherwise)."""
    out: set[str] = set()
    for _, _, c in components(defn):
        descriptors = c.get("propertyDescriptors") or {}
        for prop, v in (c.get("properties") or {}).items():
            if isinstance(v, str) and (descriptors.get(prop) or {}).get("sensitive"):
                out.update(m.group(1) for m in PARAM_REF.finditer(v))
    return out


def context_name(flow: str) -> str:
    return f"cairn-{flow}"


def prepare(defn: dict, flow: str, parameters: dict[str, str], sensitive: list[str],
            cairn_params: Optional[dict[str, str]] = None) -> dict:
    """The definition to upload: the root group named after the flow and bound to Cairn's parameter context, which
    declares every parameter (sensitive ones without a value: Cairn sets them after the upload, from secret scopes).
    A parameter the flow references and nobody gives is refused here, not left for NiFi to call invalid."""
    out = copy.deepcopy(defn)
    fc = out["flowContents"]
    fc["name"] = f"cairn-{flow}"
    name = context_name(flow)
    fc["parameterContextName"] = name
    cairn_params = cairn_params or {}
    params = [{"name": k, "description": "", "sensitive": False, "provided": False, "value": v}
              for k, v in sorted({**parameters, **{k: v for k, v in cairn_params.items()
                                                    if k not in LAKEHOUSE_SENSITIVE}}.items())]
    params += [{"name": k, "description": "from a Cairn secret scope", "sensitive": True, "provided": False,
                "value": None} for k in sorted({*sensitive, *(k for k in cairn_params if k in LAKEHOUSE_SENSITIVE)})]
    known = {p["name"] for p in params}
    # Parameters of the definition's own contexts that Cairn does not set: kept (non-sensitive ones with their value).
    for ctx in (defn.get("parameterContexts") or {}).values():
        for p in (ctx or {}).get("parameters") or []:
            if isinstance(p, dict) and p.get("name") and p["name"] not in known:
                params.append({"name": p["name"], "description": str(p.get("description") or ""),
                               "sensitive": bool(p.get("sensitive")), "provided": False,
                               "value": None if p.get("sensitive") else p.get("value")})
                known.add(p["name"])
    missing = sorted(referenced_parameters(defn) - known)
    if missing:
        raise SpecError(f"the flow references parameters nobody sets: {', '.join(missing)} (give them in parameters, "
                        "or in secrets when sensitive)")
    plain = {p["name"] for p in params if not p["sensitive"]}
    wrong = sorted(sensitive_references(defn) & plain)
    if wrong:
        raise SpecError(f"sensitive properties reference {', '.join(wrong)}: make them secrets (sensitive parameters)")
    for p in params:                                 # NiFi gives sensitive parameters no value in a definition
        if p["sensitive"]:
            p.pop("value", None)
    out["parameterContexts"] = {name: {"name": name, "description": f"Cairn flow {flow}", "parameters": params,
                                       "inheritedParameterContexts": [], "componentType": "PARAMETER_CONTEXT"}}
    out.setdefault("flowEncodingVersion", "1.0")
    out.setdefault("parameterProviders", {})
    out.setdefault("externalControllerServices", {})
    return out


# ---- lineage ------------------------------------------------------------------------------------------------------
def _resolve(v: Any, params: dict[str, str]) -> Optional[str]:
    """A setting with its parameters filled in; None when it is empty or computed per FlowFile (``${...}``)."""
    if not isinstance(v, str) or not v.strip():
        return None
    out = PARAM_REF.sub(lambda m: params.get(m.group(1), "\x00"), v).strip()
    if "\x00" in out or "${" in out:
        return None
    return out


def _jdbc(url: Optional[str]) -> Optional[tuple[str, str]]:
    """``jdbc:postgresql://host:5432/db`` -> (``postgres://host:5432``, ``db``)."""
    if not url or not url.startswith("jdbc:"):
        return None
    rest = url[5:]
    scheme = rest.split(":", 1)[0].lower()
    scheme = {"postgresql": "postgres", "sqlserver": "sqlserver", "mariadb": "mysql"}.get(scheme, scheme)
    if scheme == "sqlserver":
        host = rest.split("//", 1)[-1].split(";", 1)[0]
        m = re.search(r"databaseName=([^;]+)", rest, re.I)
        return f"sqlserver://{host}", (m.group(1) if m else "")
    parts = urlsplit(rest.split(":", 1)[1] if ":" in rest else rest)
    return f"{scheme}://{parts.netloc}", parts.path.strip("/").split("/")[0].split("?")[0]


def _table_name(db: str, table: str, default_schema: str) -> str:
    bits = [b for b in table.replace('"', "").split(".") if b]
    if len(bits) == 1:
        bits = [default_schema, *bits] if default_schema else bits
    if len(bits) == 2 and db:
        bits = [db, *bits]
    return ".".join(bits)


def _sql_tables(sql: str) -> list[str]:
    try:
        import sqlglot

        return sorted({".".join(p for p in (t.catalog, t.db, t.name) if p)
                       for t in sqlglot.parse_one(sql).find_all(sqlglot.exp.Table) if t.name})
    except Exception:  # noqa: BLE001 - SQL Cairn cannot parse names no table
        m = re.findall(r"(?:from|join|into|update)\s+([A-Za-z0-9_.\"]+)", sql, re.I)
        return sorted({x.replace('"', "") for x in m})


def lineage(defn: dict, params: dict[str, str]) -> dict[tuple[str, ...], dict[str, list[dict]]]:
    """Per process group (its path): the OpenLineage datasets (``namespace``, ``name``) its processors read
    (``inputs``) and write (``outputs``), as far as their settings say. ``lakehouse`` outputs name Iceberg tables of
    Cairn's lakehouse (``namespace`` "lakehouse", ``name`` "<schema>.<table>")."""
    services: dict[str, dict] = {}
    for _, kind, c in components(defn):
        if kind == "controllerServices":
            services[str(c.get("identifier"))] = c
    out: dict[tuple[str, ...], dict[str, list[dict]]] = {}

    def add(path, side, ns, name):
        if ns and name:
            d = out.setdefault(path, {"inputs": [], "outputs": []})
            item = {"namespace": ns, "name": name}
            if item not in d[side]:
                d[side].append(item)

    for path, kind, c in components(defn):
        if kind != "processors":
            continue
        t = _short(c.get("type"))
        side = "inputs" if t in SOURCES else "outputs" if t in SINKS else None
        if side is None:
            continue
        what = SOURCES.get(t) or SINKS.get(t)
        props = {k: _resolve(v, params) for k, v in (c.get("properties") or {}).items()}

        def prop(*names, props=props):
            return next((props[n] for n in names if props.get(n)), None)

        if what in ("table", "sql"):
            pool = services.get(str(prop("Database Connection Pooling Service", "dbcp-service") or "")) or {}
            pprops = {k: _resolve(v, params) for k, v in (pool.get("properties") or {}).items()}
            db = _jdbc(pprops.get("Database Connection URL"))
            if not db:
                continue
            ns, dbname = db
            schema = "public" if ns.startswith("postgres") else "dbo" if ns.startswith("sqlserver") else ""
            if what == "table":
                tbl = prop("Table Name", "put-db-record-table-name")
                if tbl:
                    sch = prop("Schema Name", "put-db-record-schema-name")
                    add(path, side, ns, _table_name(dbname, f"{sch}.{tbl}" if sch else tbl, schema))
            else:
                sql = prop("SQL Query", "SQL select query", "sql-pre-query", "putsql-sql-statement")
                for tbl in _sql_tables(sql or ""):
                    add(path, side, ns, _table_name(dbname, tbl, schema))
        elif what == "kafka":
            topics = prop("Topics", "Topic Name", "topic")
            servers = ""
            conn = services.get(str(prop("Kafka Connection Service") or "")) or {}
            servers = _resolve((conn.get("properties") or {}).get("bootstrap.servers"), params) or \
                prop("bootstrap.servers", "Kafka Brokers") or "kafka"
            for topic in (topics or "").split(","):
                if topic.strip():
                    add(path, side, f"kafka://{servers.split(',')[0]}", topic.strip())
        elif what == "s3":
            bucket = prop("Bucket", "bucket")
            if bucket:
                raw = {k: _resolve(str(v).split("${", 1)[0], params) if isinstance(v, str) else None
                       for k, v in (c.get("properties") or {}).items()}   # the fixed part of a computed key
                names = ("Prefix", "prefix", "Object Key") if t != "PutS3Object" else ("Object Key",)
                key = next((raw[n] for n in names if raw.get(n)), None)
                prefix = (key or "").rsplit("/", 1)[0] if key and "/" in key else ""
                add(path, side, f"s3://{bucket}", prefix or "/")
        elif what == "file":
            d = prop("Input Directory", "Directory", "File to Fetch")
            if d:
                add(path, side, "file://", d)
        elif what == "mongo":
            dbn, coll = prop("Mongo Database Name"), prop("Mongo Collection Name")
            if dbn and coll:
                add(path, side, "mongodb://mongo", f"{dbn}.{coll}")
        elif what == "iceberg":
            ns, tbl = prop("Namespace"), prop("Table Name")
            if ns and tbl:
                add(path, side, "lakehouse", f"{ns}.{tbl}")
    return out


# ---- the lakehouse sink -------------------------------------------------------------------------------------------
def _id() -> str:
    return str(uuid.uuid4())


def _bundle(artifact: str, version: str) -> dict:
    return {"group": "org.apache.nifi", "artifact": artifact, "version": version}


def _processor(group: str, name: str, type_: str, artifact: str, version: str, props: dict, x: float, y: float,
               sensitive: tuple[str, ...] = (), period: str = "0 sec", auto_terminate: tuple[str, ...] = ()) -> dict:
    return {"identifier": _id(), "name": name, "comments": "", "position": {"x": x, "y": y}, "type": type_,
            "bundle": _bundle(artifact, version), "properties": props,
            "propertyDescriptors": {k: {"name": k, "displayName": k, "identifiesControllerService": False,
                                        "sensitive": k in sensitive, "dynamic": False} for k in props},
            "style": {}, "schedulingPeriod": period, "schedulingStrategy": "TIMER_DRIVEN", "executionNode": "ALL",
            "penaltyDuration": "30 sec", "yieldDuration": "1 sec", "bulletinLevel": "WARN", "runDurationMillis": 0,
            "concurrentlySchedulableTaskCount": 1, "autoTerminatedRelationships": list(auto_terminate),
            "scheduledState": "ENABLED", "retryCount": 10, "retriedRelationships": [], "backoffMechanism":
            "PENALIZE_FLOWFILE", "maxBackoffPeriod": "10 mins", "componentType": "PROCESSOR", "groupIdentifier": group}


def _service(group: str, name: str, type_: str, artifact: str, version: str, props: dict,
             sensitive: tuple[str, ...] = (), services: tuple[str, ...] = (), dynamic: tuple[str, ...] = ()) -> dict:
    return {"identifier": _id(), "name": name, "comments": "", "type": type_, "bundle": _bundle(artifact, version),
            "properties": props,
            "propertyDescriptors": {k: {"name": k, "displayName": k, "identifiesControllerService": k in services,
                                        "sensitive": k in sensitive, "dynamic": k in dynamic} for k in props},
            "controllerServiceApis": [], "scheduledState": "DISABLED", "bulletinLevel": "WARN",
            "componentType": "CONTROLLER_SERVICE", "groupIdentifier": group}


def lakehouse_template(version: str, source: Optional[dict] = None) -> dict:
    """A flow that lands JSON records in a lakehouse table as the flow's owner: a source (by default
    ``GenerateFlowFile`` with the records in the parameter ``sample.records``, one JSON array; replace it with any
    processor that emits records), ``PutIcebergRecord`` with a JSON reader, Parquet writer and the Iceberg REST catalog
    (Cairn Catalog, storage keys vended per table).

    The catalog client signs in with a token of the owner's lakehouse session (``Bearer Token``), which it renews
    itself by token exchange at Cairn (``oauth2-server-uri``, a property NiFi hands to Iceberg's client as it is) and
    Cairn replaces before it ends. Not OAuth 2 client credentials: with them NiFi 2.12 wraps Iceberg's HTTP client in
    one that cannot pass response headers back, which Iceberg 1.11's table loads need, so no table ever loads."""
    gid = _id()
    reader = _service(gid, "JSON records", "org.apache.nifi.json.JsonTreeReader",
                      "nifi-record-serialization-services-nar", version, {})
    fileio = _service(gid, "Cairn Store (vended keys)", "org.apache.nifi.services.iceberg.aws.S3IcebergFileIOProvider",
                      "nifi-iceberg-aws-nar", version,
                      {"Authentication Strategy": "VENDED_CREDENTIALS",
                       "Endpoint URL": "#{cairn.lakehouse.store_endpoint}", "Path Style Access": "true"})
    catalog = _service(gid, "Cairn Catalog (as the flow's owner)",
                       "org.apache.nifi.services.iceberg.catalog.RESTIcebergCatalog", "nifi-iceberg-rest-catalog-nar",
                       version,
                       {"Catalog URI": "#{cairn.lakehouse.catalog_uri}", "File IO Provider": fileio["identifier"],
                        "Access Delegation Strategy": "vended-credentials", "Authentication Strategy": "BEARER",
                        "Bearer Token": "#{cairn.lakehouse.token}", "oauth2-server-uri": "#{cairn.lakehouse.token_uri}"},
                       sensitive=("Bearer Token",), services=("File IO Provider",), dynamic=("oauth2-server-uri",))
    writer = _service(gid, "Parquet files", "org.apache.nifi.services.iceberg.parquet.ParquetIcebergWriter",
                      "nifi-iceberg-parquet-writer-nar", version, {})
    src = source or _processor(gid, "Sample records", "org.apache.nifi.processors.standard.GenerateFlowFile",
                               "nifi-standard-nar", version,
                               {"Custom Text": "#{sample.records}", "Data Format": "Text", "Batch Size": "1",
                                "Unique FlowFiles": "false", "File Size": "0B", "Mime Type": "application/json"},
                               0, 0, period="1 day")
    src = {**src, "groupIdentifier": gid}
    put = _processor(gid, "Land in the lakehouse", "org.apache.nifi.processors.iceberg.PutIcebergRecord",
                     "nifi-iceberg-processors-nar", version,
                     {"Iceberg Catalog": catalog["identifier"], "Iceberg Writer": writer["identifier"],
                      "Record Reader": reader["identifier"], "Namespace": "#{cairn.lakehouse.namespace}",
                      "Table Name": "#{cairn.lakehouse.table}"}, 0, 260, auto_terminate=("success",))
    for p in ("Iceberg Catalog", "Iceberg Writer", "Record Reader"):
        put["propertyDescriptors"][p]["identifiesControllerService"] = True
    failures = {"identifier": _id(), "name": "failed records", "position": {"x": 420, "y": 260}, "componentType": "FUNNEL",
                "groupIdentifier": gid}
    conns = [
        {"identifier": _id(), "name": "", "source": {"id": src["identifier"], "type": "PROCESSOR", "groupId": gid,
                                                      "name": src["name"]},
         "destination": {"id": put["identifier"], "type": "PROCESSOR", "groupId": gid, "name": put["name"]},
         "selectedRelationships": ["success"]},
        {"identifier": _id(), "name": "", "source": {"id": put["identifier"], "type": "PROCESSOR", "groupId": gid,
                                                      "name": put["name"]},
         "destination": {"id": failures["identifier"], "type": "FUNNEL", "groupId": gid, "name": "failed records"},
         "selectedRelationships": ["failure"]},
    ]
    for c in conns:
        c.update({"labelIndex": 1, "zIndex": 0, "backPressureObjectThreshold": 10000,
                  "backPressureDataSizeThreshold": "1 GB", "flowFileExpiration": "0 sec", "prioritizers": [],
                  "bends": [], "loadBalanceStrategy": "DO_NOT_LOAD_BALANCE", "partitioningAttribute": "",
                  "loadBalanceCompression": "DO_NOT_COMPRESS", "componentType": "CONNECTION", "groupIdentifier": gid})
    return {"flowContents": {
        "identifier": gid, "name": "lakehouse-sink", "comments": "Records into a table of Cairn's lakehouse, as the "
        "flow's owner", "position": {"x": 0, "y": 0}, "processGroups": [], "remoteProcessGroups": [],
        "processors": [src, put], "inputPorts": [], "outputPorts": [], "connections": conns, "labels": [],
        "funnels": [failures], "controllerServices": [reader, fileio, catalog, writer],
        "defaultFlowFileExpiration": "0 sec", "defaultBackPressureObjectThreshold": 10000,
        "defaultBackPressureDataSizeThreshold": "1 GB", "flowFileConcurrency": "UNBOUNDED",
        "flowFileOutboundPolicy": "STREAM_WHEN_AVAILABLE", "executionEngine": "INHERITED", "maxConcurrentTasks": 1,
        "statelessFlowTimeout": "1 min", "componentType": "PROCESS_GROUP"},
        "externalControllerServices": {}, "parameterContexts": {}, "parameterProviders": {},
        "flowEncodingVersion": "1.0"}
