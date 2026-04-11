from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.core.db import get_db_session
from app.services.auth import is_authenticated, login_redirect
from app.services.repository import BacktestRepository


router = APIRouter(prefix="/backtests", tags=["backtests"])


@router.get("")
def list_backtests(request: Request, db: Session = Depends(get_db_session)):
    if not is_authenticated(request):
        return login_redirect("/dashboard")
    repo = BacktestRepository(db)
    return repo.list_backtests()


@router.get("/latest/curve")
def latest_backtest_curve(request: Request, db: Session = Depends(get_db_session)):
    if not is_authenticated(request):
        return login_redirect("/dashboard")
    repo = BacktestRepository(db)
    return repo.get_latest_backtest_curve()
