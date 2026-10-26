"""Brand lint: user-facing text in Cairn's own code never names the engine or another vendor.

User-facing text = string literals that read as prose (contain a space): notification titles and
bodies, API error messages, UI labels, assistant prompts, docs, Helm notes. Identifiers (URNs, module
paths, aspect and plugin names, header names) are not prose and are not checked. Docstrings and
comments are for developers and are not checked either.
"""

from __future__ import annotations

import ast
import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
LEAK = re.compile(r"data\s?hub|acryl|atlan(?![a-z])", re.I)
URN = re.compile(r"urn:li:[^\s\"'<>,)]+")
# Identifier-shaped tokens that may appear inside prose (module paths, plugin and aspect names).
IDENT = re.compile(r"\b(?:datahub|acryl)[\w.-]*\b|\b\w*(?:dataHub|DataHub)\w+\b|__datahub_system|DATAHUB_\w+")


def leaks(text: str) -> bool:
    if " " not in text:
        return False
    return bool(LEAK.search(IDENT.sub("", URN.sub("", text))))


def _not_text(tree: ast.AST) -> set[int]:
    """Docstrings, and regex patterns (``re.compile(...)``) such as the relabel rules themselves."""
    ids = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "compile":
            ids |= {id(n) for arg in node.args for n in ast.walk(arg) if isinstance(n, ast.Constant)}
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                ids.add(id(first.value))
    return ids


def python_findings() -> list[str]:
    out = []
    for path in sorted((ROOT / "src" / "cairn").rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        skip = _not_text(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in skip:
                if leaks(node.value):
                    out.append(f"{path.relative_to(ROOT)}:{node.lineno}: {node.value[:100]!r}")
    return out


STRING = re.compile(r"(['\"`])((?:\\.|(?!\1).)*)\1")
JSX_TEXT = re.compile(r">([^<>{}]+)<")


def ts_findings() -> list[str]:
    out = []
    for path in sorted((ROOT / "ui" / "src").rglob("*.ts*")):
        if "__tests__" in path.parts:
            continue
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            code = line.split("//", 1)[0] if not line.lstrip().startswith("*") else ""
            texts = [m.group(2) for m in STRING.finditer(code)] + JSX_TEXT.findall(code)
            out += [f"{path.relative_to(ROOT)}:{lineno}: {t[:100]!r}" for t in texts if leaks(t)]
    return out


def text_findings() -> list[str]:
    files = [*(ROOT / "docs" / "site").glob("*.md"), ROOT / "helm" / "cairn" / "templates" / "NOTES.txt",
             ROOT / "helm" / "cairn-remote-agent" / "templates" / "NOTES.txt"]
    out = []
    for path in files:
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            if leaks(line):
                out.append(f"{path.relative_to(ROOT)}:{lineno}: {line[:100]!r}")
    return out


@pytest.mark.parametrize("finder", [python_findings, ts_findings, text_findings], ids=["python", "ui", "docs"])
def test_no_vendor_names_in_user_facing_text(finder):
    assert finder() == []


def test_lint_catches_prose_but_not_identifiers():
    assert leaks("Synced from DataHub")
    assert leaks("Please contact your Datahub admin")
    assert not leaks("urn:li:dataHubPolicy:0 was imported")
    assert not leaks("from datahub.emitter import x")
    assert not leaks("datahub-rest")                     # no space: identifier
    assert not leaks("set DATAHUB_GMS_HOST for the engine")
