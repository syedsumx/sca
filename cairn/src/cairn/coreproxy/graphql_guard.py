"""Checks a GraphQL request document before the proxy forwards it.

The masking and view walkers (:mod:`masking`, :mod:`visibility`) find what to mask by the keys of the
response: ``urn`` gives the dataset context, ``fieldProfiles``/``sampleValues``/``statement`` carry the
values. GraphQL lets the caller rename any response key with an alias (``fp: fieldProfiles``,
``id: urn``), which would hide a governed value from those walkers. So the document is parsed here and
an alias on, or onto, a governed field is refused, in operations and fragments alike. A document that
cannot be parsed is refused too: the proxy cannot tell what it asks for (fail closed).
"""

from __future__ import annotations

import json
from typing import Optional
from urllib.parse import parse_qs

from cairn.coreproxy.masking import PROFILE_VALUE_KEYS, SQL_KEYS

# Response keys the walkers rely on: identity and dataset context, profile values and SQL text.
GOVERNED_FIELDS = frozenset({
    "urn", "_cairnUrn", "fieldProfiles", "fieldPath", "datasetProfiles", "datasetProfile", "statement", "value",
    *PROFILE_VALUE_KEYS, *SQL_KEYS,
})


class GraphQLRefused(Exception):
    """The request may not be forwarded; ``str(e)`` says why."""


def _documents(method: str, body: bytes, query_string: str) -> list[str]:
    if method == "GET":
        queries = parse_qs(query_string).get("query") or []
        if len(queries) != 1:
            raise GraphQLRefused("a GraphQL GET request needs exactly one query parameter")
        return queries
    try:
        payload = json.loads(body or b"null")
    except (ValueError, UnicodeDecodeError) as e:
        raise GraphQLRefused("the GraphQL request body is not JSON") from e
    items = payload if isinstance(payload, list) else [payload]
    docs = []
    for item in items:
        query = item.get("query") if isinstance(item, dict) else None
        if not isinstance(query, str):
            # e.g. a persisted query sent by hash only: the proxy cannot see what it selects.
            raise GraphQLRefused("the GraphQL request carries no query document")
        docs.append(query)
    if not docs:
        raise GraphQLRefused("the GraphQL request carries no query document")
    return docs


def governed_alias(document: str) -> Optional[str]:
    """The first ``alias: field`` in ``document`` that renames a governed field (or renames a field onto a
    governed key), as text; ``None`` if there is none. Raises :class:`GraphQLRefused` if it does not parse."""
    from graphql import GraphQLError, parse
    from graphql.language import FieldNode, Visitor, visit

    try:
        ast = parse(document, no_location=True)
    except (GraphQLError, RecursionError) as e:
        raise GraphQLRefused("the GraphQL document does not parse") from e
    found: list[str] = []

    class _Aliases(Visitor):
        def enter_field(self, node: FieldNode, *_args):
            if node.alias is None:
                return None
            alias, name = node.alias.value, node.name.value
            if alias != name and (alias in GOVERNED_FIELDS or name in GOVERNED_FIELDS):
                found.append(f"{alias}: {name}")
                return Visitor.BREAK
            return None

    visit(ast, _Aliases())   # fields inside fragment definitions and inline fragments are visited too
    return found[0] if found else None


def check_request(method: str, body: bytes, query_string: str = "") -> None:
    """Raise :class:`GraphQLRefused` if the request must not be forwarded."""
    for document in _documents(method, body, query_string):
        alias = governed_alias(document)
        if alias:
            raise GraphQLRefused(f"aliases on governed fields are not allowed ({alias})")


# ----------------------------------------------------------------------------- urn injection (view restriction)
# View restriction (authoritative mode) finds governed entities by their `urn` key, so a query that does not select
# `urn` would escape it. With Cairn Core's schema (introspected by the proxy), `urn` is added under this alias to every
# selection set whose type has a `urn` field, and through inline fragments to every interface or union whose possible
# types do; what the caller did not ask for is stripped from the answer.
INJECTED = "_cairnUrn"


def _has_urn(gtype) -> bool:
    from graphql import get_named_type, is_leaf_type, is_non_null_type

    fields = getattr(gtype, "fields", None) or {}
    f = fields.get("urn")
    return f is not None and is_leaf_type(get_named_type(f.type)) and not any(
        is_non_null_type(a.type) and a.default_value is None for a in f.args.values())


def inject_urn_document(document: str, schema) -> str:
    """``document`` with ``_cairnUrn: urn`` in every selection set of ``schema`` that can hold an entity: directly
    where the selection set's type has ``urn``, as ``... on T { _cairnUrn: urn }`` for each possible type ``T`` that
    has it where the type is an interface or union without it. Fragment definitions are covered too."""
    from graphql import TypeInfo, TypeInfoVisitor, get_named_type, is_abstract_type, parse, print_ast, visit
    from graphql.language import (FieldNode, InlineFragmentNode, NamedTypeNode, NameNode, SelectionSetNode,
                                  Visitor)

    ast = parse(document, no_location=True)
    info = TypeInfo(schema)
    plans: list[tuple[SelectionSetNode, list]] = []

    def urn_field() -> FieldNode:
        return FieldNode(alias=NameNode(value=INJECTED), name=NameNode(value="urn"), arguments=(), directives=(),
                         selection_set=None)

    class _Collect(Visitor):
        def enter_selection_set(self, node, *_args):
            gtype = info.get_parent_type()
            if gtype is None:
                return None
            gtype = get_named_type(gtype)
            if _has_urn(gtype):
                plans.append((node, [urn_field()]))
            elif is_abstract_type(gtype):
                extra = [InlineFragmentNode(type_condition=NamedTypeNode(name=NameNode(value=t.name)), directives=(),
                                            selection_set=SelectionSetNode(selections=(urn_field(),)))
                         for t in schema.get_possible_types(gtype) if _has_urn(t)]
                if extra:
                    plans.append((node, extra))
            return None

    visit(ast, TypeInfoVisitor(info, _Collect()))
    for node, extra in plans:                     # after the walk: the visitor must see the document unchanged
        node.selections = (*node.selections, *extra)
    return print_ast(ast)


def inject_urn(method: str, body: bytes, query_string: str, schema) -> tuple[bytes, str]:
    """The request (body, query string) with :func:`inject_urn_document` applied to each document."""
    from urllib.parse import urlencode

    if method == "GET":
        params = parse_qs(query_string, keep_blank_values=True)
        params["query"] = [inject_urn_document(params["query"][0], schema)]
        return body, urlencode(params, doseq=True)
    payload = json.loads(body)
    items = payload if isinstance(payload, list) else [payload]
    for item in items:
        item["query"] = inject_urn_document(item["query"], schema)
    return json.dumps(payload).encode(), query_string


def restore_injected(doc) -> list[dict]:
    """Move each injected urn to ``urn`` for the walkers; returns the objects whose ``urn`` the caller did not ask
    for (to strip once the answer is restricted and masked). The injected keys themselves are removed."""
    added: list[dict] = []

    def walk(node) -> None:
        if isinstance(node, dict):
            if INJECTED in node:
                value = node.pop(INJECTED)
                if "urn" not in node:
                    node["urn"] = value
                    added.append(node)
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(doc)
    return added
