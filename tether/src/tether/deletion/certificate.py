"""Deletion certificates: canonical JSON content plus its SHA-256 hash."""

from typing import Any

from tether.canonical import canonical_json, sha256_hex

CANONICALIZATION = "JSON with sorted keys, separators (',', ':'), UTF-8, no ASCII escaping"


def certificate_hash(content: dict[str, Any]) -> str:
    return sha256_hex(content)


def verify_certificate(content: dict[str, Any], expected_sha256: str) -> bool:
    return certificate_hash(content) == expected_sha256


def render(content: dict[str, Any], sha256: str) -> str:
    """The downloadable form: ``{"certificate": ..., "sha256": ...}``."""
    return canonical_json({"certificate": content, "sha256": sha256})
