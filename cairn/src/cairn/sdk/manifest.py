"""``cairn-source.yaml``: a connector's manifest, which puts it in the source gallery::

    name: csv-folder
    version: 1.0.0
    entrypoint: cairn_csv_folder.source:CsvFolder   # package.module:Class, installed with pip
    description: Every CSV file in a folder, with its header as the schema.
    category: other
    fields:                                            # the gallery form; secrets by name, as elsewhere
      - {key: folder, label: Folder, required: true}
    example_config: {folder: ./tests/data}             # used by cairn source check

Manifests are read from ``CAIRN_SOURCE_PLUGINS`` (default ``/etc/cairn/config/source-plugins``): a folder
holding one folder (or one ``*.yaml`` file) per connector.
"""

from __future__ import annotations

import os
from typing import Any, Literal, Optional

import yaml
from pydantic import BaseModel, Field

BRIDGE = "cairn.sdk.bridge.SdkSource"


class ManifestField(BaseModel):
    key: str = Field(pattern=r"^[a-z][a-z0-9_]{0,62}$")
    label: str
    kind: Literal["text", "secret", "number", "bool", "select", "list"] = "text"
    required: bool = False
    default: Any = None
    help: str = ""
    options: list[str] = Field(default_factory=list)


class SourceManifest(BaseModel):
    name: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,62}$")
    version: str
    entrypoint: str = Field(pattern=r"^[A-Za-z_][\w.]*:[A-Za-z_]\w*$")
    description: str = ""
    category: Literal["warehouse", "database", "lake", "bi", "transformation", "streaming", "ml", "other"] = "other"
    fields: list[ManifestField] = Field(default_factory=list)
    example_config: dict[str, Any] = Field(default_factory=dict)


def read(path: str) -> SourceManifest:
    file = os.path.join(path, "cairn-source.yaml") if os.path.isdir(path) else path
    with open(file) as fh:
        return SourceManifest.model_validate(yaml.safe_load(fh) or {})


def load_all(root: Optional[str] = None) -> list[SourceManifest]:
    root = root or os.environ.get("CAIRN_SOURCE_PLUGINS", "/etc/cairn/config/source-plugins")
    if not root or not os.path.isdir(root):
        return []
    out = []
    for entry in sorted(os.listdir(root)):
        p = os.path.join(root, entry)
        if (os.path.isdir(p) and os.path.exists(os.path.join(p, "cairn-source.yaml"))) or \
                (entry.endswith((".yaml", ".yml")) and ".example." not in entry):
            out.append(read(p))
    return out
