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
# `urn` would escape it. The proxy adds `urn`, under this alias, to every selection set it knows is a governed entity
# (or the Entity interface), and strips what the caller did not ask for from the answer.
INJECTED = "_cairnUrn"
# GraphQL type names of the governed asset types (visibility.GOVERNED_TYPES) and the interfaces they implement.
ENTITY_TYPENAMES = frozenset({
    "Dataset", "Chart", "Dashboard", "DataJob", "DataFlow", "Container", "DataProduct", "MLModel", "MLModelGroup",
    "MLFeatureTable", "MLFeature", "MLPrimaryKey", "Notebook", "Document", "QueryEntity", "Entity",
    "EntityWithRelationships", "BrowsableEntity",
})
# Fields that return a governed entity (or the Entity interface) whatever their parent.
ENTITY_FIELDS = frozenset({
    "entity", "dataset", "chart", "dashboard", "dataJob", "dataFlow", "container", "containers", "dataProduct",
    "mlModel", "mlModelGroup", "mlFeatureTable", "mlFeature", "mlPrimaryKey", "notebook",
})


def _with_urn(selection_set):
    from graphql.language import FieldNode, NameNode, SelectionSetNode

    extra = FieldNode(alias=NameNode(value=INJECTED), name=NameNode(value="urn"), arguments=(), directives=(),
                      selection_set=None)
    return SelectionSetNode(selections=(*selection_set.selections, extra))


def inject_urn_document(document: str) -> str:
    """``document`` with ``_cairnUrn: urn`` in every selection set of a governed entity: fields that look an entity
    up by urn (``dataset(urn:)``, ``entity(urn:)``), fields that return one (``entity``, ``container``, ...), and
    fragments on a governed type or the Entity interface."""
    from graphql import parse, print_ast
    from graphql.language import FieldNode, FragmentDefinitionNode, InlineFragmentNode

    ast = parse(document, no_location=True)

    def visit(node) -> None:
        sel = getattr(node, "selection_set", None)
        if sel is None:
            for d in getattr(node, "definitions", ()) or ():
                visit(d)
            return
        for child in sel.selections:
            visit(child)
        governed = False
        if isinstance(node, FieldNode):
            governed = node.name.value in ENTITY_FIELDS or any(a.name.value == "urn" for a in node.arguments or ())
        elif isinstance(node, (InlineFragmentNode, FragmentDefinitionNode)):
            governed = node.type_condition is not None and node.type_condition.name.value in ENTITY_TYPENAMES
        if governed:
            node.selection_set = _with_urn(node.selection_set)

    visit(ast)
    return print_ast(ast)


def inject_urn(method: str, body: bytes, query_string: str) -> tuple[bytes, str]:
    """The request (body, query string) with :func:`inject_urn_document` applied to each document."""
    from urllib.parse import urlencode

    if method == "GET":
        params = parse_qs(query_string, keep_blank_values=True)
        params["query"] = [inject_urn_document(params["query"][0])]
        return body, urlencode(params, doseq=True)
    payload = json.loads(body)
    items = payload if isinstance(payload, list) else [payload]
    for item in items:
        item["query"] = inject_urn_document(item["query"])
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
