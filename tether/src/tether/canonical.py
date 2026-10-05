"""Canonical JSON and hashing, used for certificates and the audit chain."""

import hashlib
import json
from datetime import date, datetime
from typing import Any


def _default(value: Any) -> Any:
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, set | frozenset):
        return sorted(value)
    raise TypeError(f"not JSON serialisable: {type(value).__name__}")


def canonical_json(value: Any) -> str:
    """Deterministic JSON: sorted keys, no insignificant whitespace, UTF-8 text."""
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=_default
    )


def sha256_hex(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()
