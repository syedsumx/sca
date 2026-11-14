"""What a connector gives Cairn, and the catalog metadata it becomes."""

from __future__ import annotations

import importlib
import re
from typing import Any, ClassVar, Iterable, Optional

from pydantic import BaseModel, Field as PField, field_validator

from cairn.common.proposals import make_mcp

TYPES = {"string": "StringType", "number": "NumberType", "boolean": "BooleanType", "date": "DateType",
         "time": "TimeType", "bytes": "BytesType", "array": "ArrayType", "map": "MapType", "record": "RecordType",
         "null": "NullType"}


class Field(BaseModel):
    path: str = PField(min_length=1, max_length=500)
    type: str = "string"                     # string, number, boolean, date, time, bytes, array, map, record
    native_type: Optional[str] = None
    description: Optional[str] = None
    nullable: bool = True
    tags: list[str] = PField(default_factory=list)

    @field_validator("type")
    @classmethod
    def _type(cls, v: str) -> str:
        if v not in TYPES:
            raise ValueError(f"type is one of {sorted(TYPES)}")
        return v


class Lineage(BaseModel):
    upstream: str = PField(description="the dataset urn this one is built from")
    type: str = "TRANSFORMED"                # TRANSFORMED | COPY | VIEW


class Dataset(BaseModel):
    name: str = PField(min_length=1, max_length=500, description="e.g. db.schema.table")
    description: Optional[str] = None
    fields: list[Field] = PField(default_factory=list)
    owners: list[str] = PField(default_factory=list, description="user names or urns")
    tags: list[str] = PField(default_factory=list)
    properties: dict[str, str] = PField(default_factory=dict)
    upstreams: list[Lineage] = PField(default_factory=list)
    url: Optional[str] = None
    subtype: Optional[str] = None            # Table, View, Topic, File…


class Source:
    """Subclass and set ``platform`` and ``config_model``; implement ``datasets`` (and ``test_connection``)."""

    platform: ClassVar[str] = ""
    config_model: ClassVar[type[BaseModel]] = BaseModel

    def test_connection(self, config: BaseModel) -> tuple[bool, str]:
        """``(connected, reason when not)``. Called before ingestion and by **Test connection**."""
        return True, ""

    def datasets(self, config: BaseModel) -> Iterable[Dataset]:
        raise NotImplementedError


def load(entrypoint: str) -> type[Source]:
    """``package.module:ClassName`` → the connector class."""
    module, _, name = entrypoint.partition(":")
    if not module or not name:
        raise ValueError("an entrypoint is package.module:ClassName")
    cls = getattr(importlib.import_module(module), name)
    if not (isinstance(cls, type) and issubclass(cls, Source)):
        raise TypeError(f"{entrypoint} is not a cairn.sdk.Source")
    return cls


def dataset_urn(platform: str, name: str, env: str = "PROD") -> str:
    return f"urn:li:dataset:(urn:li:dataPlatform:{platform},{name},{env})"


def _owner(o: str) -> str:
    return o if o.startswith("urn:li:") else f"urn:li:corpuser:{o}"


def _tag(t: str) -> str:
    return t if t.startswith("urn:li:tag:") else f"urn:li:tag:{re.sub(r'[^A-Za-z0-9_.-]', '_', t)}"


def to_mcps(platform: str, datasets: Iterable[Dataset], env: str = "PROD") -> list[dict]:
    """Catalog metadata for each dataset: properties, schema, owners, tags, lineage, subtype, status."""
    out: list[dict] = []
    for d in datasets:
        urn = dataset_urn(platform, d.name, env)
        props: dict[str, Any] = {"name": d.name.split(".")[-1], "customProperties": dict(d.properties)}
        if d.description:
            props["description"] = d.description
        if d.url:
            props["externalUrl"] = d.url
        out.append(make_mcp(urn, "datasetProperties", props))
        if d.fields:
            fields = []
            for f in d.fields:
                field: dict[str, Any] = {"fieldPath": f.path, "nativeDataType": f.native_type or f.type,
                                         "type": {"type": {f"com.linkedin.schema.{TYPES[f.type]}": {}}},
                                         "nullable": f.nullable}
                if f.description:
                    field["description"] = f.description
                if f.tags:
                    field["globalTags"] = {"tags": [{"tag": _tag(t)} for t in f.tags]}
                fields.append(field)
            out.append(make_mcp(urn, "schemaMetadata", {
                "schemaName": d.name, "platform": f"urn:li:dataPlatform:{platform}", "version": 0, "hash": "",
                "platformSchema": {"com.linkedin.schema.OtherSchema": {"rawSchema": ""}}, "fields": fields}))
        if d.owners:
            out.append(make_mcp(urn, "ownership", {"owners": [{"owner": _owner(o), "type": "DATAOWNER"} for o in d.owners],
                                                   "lastModified": {"time": 0, "actor": "urn:li:corpuser:cairn-sdk"}}))
        if d.tags:
            out.append(make_mcp(urn, "globalTags", {"tags": [{"tag": _tag(t)} for t in d.tags]}))
        if d.upstreams:
            out.append(make_mcp(urn, "upstreamLineage", {"upstreams": [
                {"dataset": u.upstream, "type": u.type, "auditStamp": {"time": 0, "actor": "urn:li:corpuser:cairn-sdk"}}
                for u in d.upstreams]}))
        if d.subtype:
            out.append(make_mcp(urn, "subTypes", {"typeNames": [d.subtype]}))
        out.append(make_mcp(urn, "status", {"removed": False}))
    return out
