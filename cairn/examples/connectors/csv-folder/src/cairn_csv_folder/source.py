"""An example SDK connector: every CSV file in a folder becomes a dataset, its header the schema.

A ``<name>.md`` file next to ``<name>.csv`` is the dataset's description. Numbers are told apart from
text by the first data row.
"""

from __future__ import annotations

import csv
import os
from typing import Iterable, Optional

from pydantic import BaseModel

from cairn.sdk import Dataset, Field, Source


class Config(BaseModel):
    folder: str
    owner: Optional[str] = None


def _type(value: str) -> str:
    try:
        float(value)
        return "number"
    except ValueError:
        return "boolean" if value.lower() in ("true", "false") else "string"


class CsvFolder(Source):
    platform = "csv"
    config_model = Config

    def test_connection(self, config: Config) -> tuple[bool, str]:
        if not os.path.isdir(config.folder):
            return False, f"{config.folder} is not a folder the executor can read"
        return True, ""

    def datasets(self, config: Config) -> Iterable[Dataset]:
        for entry in sorted(os.listdir(config.folder)):
            if not entry.endswith(".csv"):
                continue
            name = entry[:-4]
            path = os.path.join(config.folder, entry)
            with open(path, newline="") as fh:
                rows = csv.reader(fh)
                header = next(rows, [])
                first = next(rows, [""] * len(header))
            doc = os.path.join(config.folder, f"{name}.md")
            description = open(doc).read().strip() if os.path.exists(doc) else None
            yield Dataset(name=f"exports.{name}", description=description, subtype="File",
                          fields=[Field(path=h, type=_type(v) if v else "string", native_type="csv")
                                  for h, v in zip(header, first + [""] * (len(header) - len(first)))],
                          owners=[config.owner] if config.owner else [], properties={"file": entry})
