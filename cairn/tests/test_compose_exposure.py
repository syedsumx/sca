"""What the Compose stack publishes on the host's network, and what its edge passes on.

Compose ships working defaults for its shared keys (the Trino access key and internal secret, the store's secret,
the catalog's token, Airflow's JWT secret) so that ``docker compose up`` works on a laptop. Those values are public
(they are in this repository), so nothing that trusts them may be reachable from other machines by default: only the
edge (the UI and the API behind sign-in) is published on every interface. The rest is on loopback, as the core and
web ports already were. The edge must not pass Trino's keyed calls on either: Trino reaches the API directly."""

import os
import re

import yaml

ROOT = os.path.join(os.path.dirname(__file__), "..")
PUBLIC = {"edge"}          # the one way in for users and other machines


class _Loader(yaml.SafeLoader):
    pass


# The overlays use Compose's own tags (!override, !reset): kept as plain values.
_Loader.add_multi_constructor("!", lambda loader, suffix, node: loader.construct_sequence(node)
                              if isinstance(node, yaml.SequenceNode) else loader.construct_scalar(node)
                              if isinstance(node, yaml.ScalarNode) else loader.construct_mapping(node))


def _defaults(text: str) -> str:
    """``${VAR:-default}`` / ``${VAR-default}`` as Compose resolves them with nothing set; ``${VAR}`` as empty."""
    while True:
        out = re.sub(r"\$\{[A-Za-z_][A-Za-z0-9_]*(?::?-([^{}]*))?\}", lambda m: m.group(1) or "", text)
        if out == text:
            return out
        text = out


def _published(port) -> tuple[str, str]:
    """(host address, host port) of a published port, short or long syntax."""
    if isinstance(port, dict):
        return _defaults(str(port.get("host_ip", ""))), _defaults(str(port.get("published", "")))
    parts = _defaults(str(port)).split(":")
    if len(parts) == 3:
        return parts[0], parts[1]
    return "", parts[0] if len(parts) == 2 else ""


def _compose_files():
    return sorted(f for f in os.listdir(ROOT) if f.startswith("docker-compose") and f.endswith((".yml", ".yaml")))


def test_only_the_edge_is_published_beyond_loopback():
    exposed = []
    for f in _compose_files():
        doc = yaml.load(open(os.path.join(ROOT, f)), Loader=_Loader) or {}
        for name, svc in (doc.get("services") or {}).items():
            for p in (svc or {}).get("ports") or []:
                host, _ = _published(p)
                if name not in PUBLIC and host not in ("127.0.0.1", "::1", "localhost"):
                    exposed.append(f"{f}: {name} publishes {p!r} on {host or 'every interface'}")
    assert not exposed, "\n".join(exposed)


def _edge_locations() -> str:
    return open(os.path.join(ROOT, "docker", "nginx", "locations.conf")).read()


def _edge_refuses(path: str) -> bool:
    """Whether a regex location of the edge answers ``path`` with 404 before the API's prefix location gets it
    (nginx tries regex locations after the longest prefix, and the first that matches wins)."""
    for m in re.finditer(r"location\s+~\*?\s+(\S+)\s*\{(.*?)\}", _edge_locations(), re.S):
        if re.search(m.group(1), path):
            return re.search(r"return\s+404\s*;", m.group(2)) is not None
    return False


def test_the_edge_does_not_pass_on_trinos_keyed_calls():
    key = "cairn-trino-dev-access-key-0001"           # Compose's default: public
    for tail in ("allow", "batch", "row-filters", "column-mask", "lineage", "queries", "warehouse/wh1/allow",
                 "warehouse/wh1/admit", "ai/v1/messages", "ai/v1/chat/completions"):
        assert _edge_refuses(f"/cairn/api/v1/trino/{key}/{tail}"), tail
    # What people and engines call through the edge still goes to the API.
    for path in ("/cairn/api/v1/trino/token", "/cairn/api/v1/trino/jwks.json", "/cairn/api/v1/trino/oauth/authorize",
                 "/cairn/api/v1/trino/oauth/token", "/cairn/api/v1/trino/catalog-sync",
                 "/cairn/api/v1/trino/functions/sync", "/cairn/api/v1/trino/functions/ai-search",
                 "/cairn/api/v1/warehouses/wh1/start", "/cairn/api/quality/monitors"):
        assert not _edge_refuses(path), path
