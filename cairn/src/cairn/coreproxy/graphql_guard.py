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
    "urn", "fieldProfiles", "fieldPath", "datasetProfiles", "datasetProfile", "statement", "value",
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
