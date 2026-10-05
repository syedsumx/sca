"""Model registry with retraining flags."""

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from tether.api import presenters, schemas
from tether.api.deps import get_session
from tether.db.models import ModelVersionRow

router = APIRouter(prefix="/models", tags=["models"])


@router.get("", response_model=list[schemas.ModelOut])
def list_models(session: Annotated[Session, Depends(get_session)]) -> list[schemas.ModelOut]:
    rows = session.scalars(select(ModelVersionRow).order_by(ModelVersionRow.id))
    return [presenters.model(m) for m in rows]
