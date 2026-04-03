from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.db import get_db_session
from app.services.repository import BacktestRepository


router = APIRouter(prefix="/backtests", tags=["backtests"])


@router.get("")
def list_backtests(db: Session = Depends(get_db_session)) -> list[dict]:
    repo = BacktestRepository(db)
    return repo.list_backtests()


@router.get("/latest/curve")
def latest_backtest_curve(db: Session = Depends(get_db_session)) -> list[dict]:
    repo = BacktestRepository(db)
    return repo.get_latest_backtest_curve()
