"""``/catalog/glossary``, ``/catalog/domains`` and ``/catalog/assets``: the glossary and domains in Cairn Core, made,
changed and applied to assets through Cairn (cairn.marketplace.glossary, cairn.marketplace.domains)."""

from __future__ import annotations

from typing import Any, Callable, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from pydantic import BaseModel, ValidationError
from starlette.requests import Request  # module level: FastAPI resolves the handlers' annotations here

from cairn.common.security import Principal
from cairn.marketplace.domains import DomainIn, Domains
from cairn.marketplace.glossary import EditError, Glossary, NodeIn, TermIn

FORWARD = ("authorization", "cookie", "x-cairn-actor")


class TermOnAsset(BaseModel):
    asset: str
    term: str
    field: Optional[str] = None


class DomainOfAsset(BaseModel):
    asset: str
    domain: Optional[str] = None


def _wrap(fn: Callable[[], Any]) -> Any:
    try:
        return fn()
    except EditError as e:
        raise HTTPException(e.status, str(e))
    except ValueError as e:
        raise HTTPException(422, str(e))


def _spec(model, body: dict, ident: Optional[str] = None):
    try:
        return model.model_validate({**body, "id": ident} if ident is not None else body)
    except ValidationError as e:
        raise HTTPException(422, e.errors(include_url=False, include_context=False))


def build_router(glossary: Glossary, domains: Domains, current_principal,
                 change_requests: Callable[[], Any] = lambda: None) -> APIRouter:
    """``change_requests()``: the IaC change requests (cairn.iac.changes) a term change is proposed through, if any."""
    router = APIRouter(prefix="/catalog", tags=["catalog"])

    # ---- term groups ------------------------------------------------------------------------------------------
    @router.get("/glossary/nodes")
    def list_nodes(p: Principal = Depends(current_principal)):
        return {"nodes": _wrap(lambda: glossary.list_nodes(p))}

    @router.get("/glossary/nodes/{nid}")
    def get_node(nid: str, p: Principal = Depends(current_principal)):
        return _wrap(lambda: glossary.get_node(p, nid))

    @router.post("/glossary/nodes", status_code=201)
    def create_node(body: dict = Body(...), p: Principal = Depends(current_principal)):
        spec = _spec(NodeIn, body)
        return _wrap(lambda: glossary.save_node(p, spec, create=True))

    @router.put("/glossary/nodes/{nid}")
    def update_node(nid: str, body: dict = Body(...), p: Principal = Depends(current_principal)):
        spec = _spec(NodeIn, body, nid)
        return _wrap(lambda: glossary.save_node(p, spec, create=False))

    @router.delete("/glossary/nodes/{nid}", status_code=204)
    def delete_node(nid: str, p: Principal = Depends(current_principal)):
        _wrap(lambda: glossary.delete_node(p, nid))

    # ---- terms ------------------------------------------------------------------------------------------------
    @router.get("/glossary/terms")
    def list_terms(p: Principal = Depends(current_principal)):
        return {"terms": _wrap(lambda: glossary.list_terms(p))}

    @router.get("/glossary/terms/{tid}")
    def get_term(tid: str, p: Principal = Depends(current_principal)):
        return _wrap(lambda: glossary.get_term(p, tid))

    @router.post("/glossary/terms", status_code=201)
    def create_term(body: dict = Body(...), p: Principal = Depends(current_principal)):
        spec = _spec(TermIn, body)
        return _wrap(lambda: glossary.save_term(p, spec, create=True))

    @router.put("/glossary/terms/{tid}")
    def update_term(tid: str, body: dict = Body(...), p: Principal = Depends(current_principal)):
        spec = _spec(TermIn, body, tid)
        return _wrap(lambda: glossary.save_term(p, spec, create=False))

    @router.delete("/glossary/terms/{tid}", status_code=204)
    def delete_term(tid: str, force: bool = Query(False), p: Principal = Depends(current_principal)):
        _wrap(lambda: glossary.delete_term(p, tid, force))

    @router.get("/glossary/terms/{tid}/assets")
    def term_assets(tid: str, p: Principal = Depends(current_principal)):
        return {"assets": _wrap(lambda: glossary.term_assets(p, tid))}

    @router.get("/glossary/rights")
    def glossary_rights(urn: Optional[str] = Query(None), p: Principal = Depends(current_principal)):
        out = _wrap(lambda: glossary.rights(p, urn))
        out["may_propose"] = (change_requests() is not None and p.via != "agent"
                              and bool(urn) and str(urn).startswith("urn:li:glossaryTerm:"))
        return out

    @router.post("/glossary/proposals", status_code=201)
    def propose_term(request: Request, body: dict = Body(...), p: Principal = Depends(current_principal)):
        """A term change (or a new term) proposed for approval: a change request of kind glossary_terms."""
        import yaml

        from cairn.iac.changes import ChangeError

        svc = change_requests()
        if svc is None:
            raise HTTPException(503, "change requests are not set up in this Cairn")
        body = dict(body)
        intent = str(body.pop("intent", "") or "").strip()
        title = str(body.pop("title", "") or "").strip()
        spec = _spec(TermIn, body)
        title = title or f"Glossary term {spec.name}"
        doc = {"bundle": f"glossary-{spec.id}", "glossary_terms": [spec.model_dump()]}
        headers = {k: v for k, v in request.headers.items() if k.lower() in FORWARD}
        try:
            out = svc.propose(p, headers, title, intent, bundle_yaml=yaml.safe_dump(doc, sort_keys=False))
        except ChangeError as e:
            raise HTTPException(e.status, str(e))
        return {"id": out["id"], "status": out["status"], "title": out["title"], "plan": out["plan"]}

    # ---- terms and domains on assets --------------------------------------------------------------------------
    @router.get("/assets/labels")
    def asset_labels(urn: str = Query(...), p: Principal = Depends(current_principal)):
        return _wrap(lambda: glossary.labels(p, urn))

    @router.post("/assets/terms")
    def apply_term(body: TermOnAsset, p: Principal = Depends(current_principal)):
        return _wrap(lambda: glossary.apply(p, body.asset, body.term, body.field, on=True))

    @router.post("/assets/terms/remove")
    def remove_term(body: TermOnAsset, p: Principal = Depends(current_principal)):
        return _wrap(lambda: glossary.apply(p, body.asset, body.term, body.field, on=False))

    @router.put("/assets/domain")
    def set_domain(body: DomainOfAsset, p: Principal = Depends(current_principal)):
        return _wrap(lambda: domains.set_domain(p, body.asset, body.domain))

    # ---- domains ----------------------------------------------------------------------------------------------
    @router.get("/domains")
    def list_domains(p: Principal = Depends(current_principal)):
        return {"domains": _wrap(lambda: domains.list(p))}

    @router.get("/domains/rights")
    def domain_rights(urn: Optional[str] = Query(None), p: Principal = Depends(current_principal)):
        return _wrap(lambda: domains.rights(p, urn))

    @router.get("/domains/{did}")
    def get_domain(did: str, p: Principal = Depends(current_principal)):
        return _wrap(lambda: domains.get(p, did))

    @router.get("/domains/{did}/assets")
    def domain_assets(did: str, p: Principal = Depends(current_principal)):
        return _wrap(lambda: domains.contents(p, did))

    @router.post("/domains", status_code=201)
    def create_domain(body: dict = Body(...), p: Principal = Depends(current_principal)):
        spec = _spec(DomainIn, body)
        return _wrap(lambda: domains.save(p, spec, create=True))

    @router.put("/domains/{did}")
    def update_domain(did: str, body: dict = Body(...), p: Principal = Depends(current_principal)):
        spec = _spec(DomainIn, body, did)
        return _wrap(lambda: domains.save(p, spec, create=False))

    @router.delete("/domains/{did}", status_code=204)
    def delete_domain(did: str, force: bool = Query(False), p: Principal = Depends(current_principal)):
        _wrap(lambda: domains.delete(p, did, force))

    return router


__all__ = ["build_router"]
