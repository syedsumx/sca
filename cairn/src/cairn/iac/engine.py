"""Reads and writes registry kinds (:mod:`cairn.iac.registry`) through Cairn's API, the same way the Terraform
provider does (``terraform-provider-cairn/internal/provider/resource.go``): one implementation of the path
templates, ids and bodies for bundles, ``cairn export`` and ``GET /v1/iac/export``."""

from __future__ import annotations

from typing import Any, Callable, Optional
from urllib.parse import quote, urlencode

from cairn.iac.registry import PLACEHOLDER, Kind, Op

Call = Callable[..., Any]       # call(method, path, body=None) -> the answer's JSON (None when empty)


class EngineError(Exception):
    pass


def esc(v: Any) -> str:
    return quote(str(v), safe=":*")


def expand(tmpl: str, values: dict, ident: Optional[str] = None) -> str:
    def one(m):
        name = m.group(1)
        if name == "id":
            if ident is None:
                raise EngineError(f"{tmpl}: the resource's id is not known yet")
            return esc(ident)
        if values.get(name) in (None, ""):
            raise EngineError(f"{tmpl}: {name} is not set")
        return esc(values[name])
    return PLACEHOLDER.sub(one, tmpl)


def dig(rec: dict, name: str) -> tuple[bool, Any]:
    """A record's field by its (dotted) name: (present, value)."""
    cur: Any = rec
    for part in name.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return False, None
        cur = cur[part]
    return True, cur


def path_of(k: Kind, o: Op, values: dict, ident: Optional[str]) -> str:
    """The op's path, with its query parameters."""
    p = expand(o.path, values, ident)
    q = {n: ("true" if values[n] is True else "false" if values[n] is False else str(values[n]))
         for n in o.query if values.get(n) is not None}
    return p + ("?" + urlencode(q) if q else "")


def body(k: Kind, o: Op, values: dict) -> Optional[dict]:
    if o.no_body:
        return None
    names = [f.name for f in k.fields if not f.once] + list(k.bundle_fields)
    out = {n: values[n] for n in names
           if n in values and n not in o.omit and n not in o.query and (not o.only or n in o.only)}
    if o.body:
        out.update(o.body)
    return out


def record_id(k: Kind, rec: dict) -> str:
    if k.singleton:
        return ""
    return str(rec.get(k.id))


def tf_id(k: Kind, rec: dict) -> str:
    """Terraform's id: the parents' values and Cairn's id, joined by / (a setting without parents: its kind)."""
    parts = [str(rec.get(p)) for p in k.parents] + ([] if k.singleton else [record_id(k, rec)])
    return "/".join(parts) or k.name


def split_id(k: Kind, ident: str) -> tuple[dict, str]:
    """The parents and Cairn's id in a Terraform id."""
    if k.singleton and not k.parents:
        return {}, ""
    n = len(k.parents) + (0 if k.singleton else 1)
    parts = ident.split("/", n - 1) if n > 1 else [ident]
    if len(parts) != n:
        want = "/".join([*k.parents, *([] if k.singleton else [k.id])])
        raise EngineError(f"cairn_{k.tf}: an id is {want}")
    return dict(zip(k.parents, parts)), ("" if k.singleton else parts[-1])


def declared(k: Kind, rec: dict) -> dict:
    """A record as its fields (Cairn's names for them), without what Cairn adds."""
    out = {}
    for f in k.fields:
        if f.write_only or f.once:
            continue
        present, v = dig(rec, f.read_name)
        if present:
            out[f.name] = v
    return out


def key_values(k: Kind, rec: dict) -> list[str]:
    return [str(dig(rec, k.field(f).read_name)[1]) for f in k.key]


class Engine:
    def __init__(self, call: Call):
        self.call = call

    # ---- reading ------------------------------------------------------------------------------------------------
    def records(self, k: Kind, parents: Optional[dict] = None) -> list[dict]:
        """The kind's records (under ``parents``), with the parents' values filled in."""
        parents = parents or {}
        res = self.call("GET", expand(k.read, parents))
        if k.singleton:
            items = [res] if isinstance(res, dict) else []
        else:
            if k.list_key:
                res = (res or {}).get(k.list_key) if isinstance(res, dict) else None
            items = [x for x in (res or []) if isinstance(x, dict)]
        out = []
        for r in items:
            if all(r.get(a) == b for a, b in k.match) and not any(str(r.get(a, "")).startswith(b) for a, b in k.skip):
                out.append({**r, **{p: parents[p] for p in k.parents}})
        return out

    def find(self, k: Kind, parents: dict, ident: str) -> Optional[dict]:
        for r in self.records(k, parents):
            if k.singleton or record_id(k, r) == ident:
                return r
        return None

    def all(self, k: Kind, kinds: dict[str, Kind]) -> list[dict]:
        """Every record of the kind, its parents' records listed first (for export)."""
        if not k.parents:
            return self.records(k)
        parent = self.parent_kind(k, kinds)
        out = []
        for pr in self.all(parent, kinds):
            values = {p: pr.get(p) for p in k.parents[:-1]}
            values[k.parents[-1]] = record_id(parent, pr)
            try:
                out += self.records(k, values)
            except EngineError:
                continue
        return out

    @staticmethod
    def parent_kind(k: Kind, kinds: dict[str, Kind]) -> Kind:
        """The kind whose records the last parent field names (its read path is this kind's read path's prefix)."""
        prefix = k.read.split("{" + k.parents[-1] + "}")[0].rstrip("/")
        for other in kinds.values():
            if other is not k and other.read.split("?")[0].rstrip("/") == prefix:     # its list may be filtered
                return other
        raise EngineError(f"{k.name}: no kind lists its parents ({prefix})")

    # ---- writing ------------------------------------------------------------------------------------------------
    def create(self, k: Kind, values: dict) -> dict:
        ident = values.get(k.id) if k.id in values else None
        res = self.call(k.create.method, path_of(k, k.create, values, None if ident is None else str(ident)),
                        body(k, k.create, values))
        parents = {p: values[p] for p in k.parents}
        if isinstance(res, dict) and res.get(k.id) not in (None, ""):
            ident = res[k.id]
        if k.singleton:
            return self.find(k, parents, "") or {}
        if ident is not None:
            found = self.find(k, parents, str(ident))
            if found is not None:
                return found
        for r in self.records(k, parents):                 # known by its key instead
            if key_values(k, r) == [str(values.get(f)) for f in k.key]:
                return r
        raise EngineError(f"{k.name}: created, but Cairn does not list it")

    def update(self, k: Kind, rec: dict, values: dict) -> dict:
        if k.update is None:
            raise EngineError(f"{k.name}: created once; a change replaces it")
        ident = record_id(k, rec)
        values = {**{p: rec.get(p) for p in k.parents}, **values}
        self.call(k.update.method, path_of(k, k.update, values, ident), body(k, k.update, values))
        return self.find(k, {p: values[p] for p in k.parents}, ident) or {}

    def delete(self, k: Kind, rec: dict) -> None:
        if k.delete is None:
            return
        values = {**rec, **{p: rec.get(p) for p in k.parents}}
        self.call(k.delete.method, path_of(k, k.delete, values, record_id(k, rec)), body(k, k.delete, values))
