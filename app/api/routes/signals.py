from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.db import get_db_session
from app.services.repository import PredictionRepository


router = APIRouter(prefix="/signals", tags=["signals"])


@router.get("/latest")
def latest_signals(limit: int = 20, db: Session = Depends(get_db_session)) -> list[dict]:
    repo = PredictionRepository(db)
    return repo.list_latest_predictions(limit=limit)
