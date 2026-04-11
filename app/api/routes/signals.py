from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.core.db import get_db_session
from app.services.auth import is_authenticated, login_redirect
from app.services.repository import PredictionRepository


router = APIRouter(prefix="/signals", tags=["signals"])


@router.get("/latest")
def latest_signals(request: Request, limit: int = 20, db: Session = Depends(get_db_session)):
    if not is_authenticated(request):
        return login_redirect("/dashboard")
    repo = PredictionRepository(db)
    return repo.list_latest_predictions(limit=limit)
