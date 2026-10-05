"""Agent memory reads (policy-filtered) and regeneration of flagged entries."""

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from tether.api import schemas
from tether.api.deps import get_session
from tether.pipelines.memory import regenerate_flagged_memory
from tether.stores.memory import read_memory

router = APIRouter(prefix="/memory", tags=["memory"])
SessionDep = Annotated[Session, Depends(get_session)]


@router.get("", response_model=list[schemas.MemoryOut])
def list_memory(
    session: SessionDep,
    subject_id: str | None = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
) -> list[schemas.MemoryOut]:
    return [
        schemas.MemoryOut(
            id=m.id,
            kind=m.kind,
            content=m.content,
            subject_ids=list(m.subject_ids),
            derived_from=list(m.derived_from),
        )
        for m in read_memory(session, subject_id, limit=limit)
    ]


@router.post("/regenerate", response_model=schemas.RegenerateOut)
def regenerate(session: SessionDep) -> schemas.RegenerateOut:
    """Rebuild multi-subject entries flagged ``needs_regeneration`` from allowed data."""
    return schemas.RegenerateOut(regenerated=regenerate_flagged_memory(session))
