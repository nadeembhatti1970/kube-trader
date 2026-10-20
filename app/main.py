from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel

from app.market_data import get_daily_bars
from app.database import connect, get_risk_state, latest_account_snapshot, latest_backtest
from app.settings import get_settings
from app.storage import store_bars

settings = get_settings()
app = FastAPI(title="Kube Trader", version="0.1.0")


class Signal(BaseModel):
    symbol: str
    signal: str
    fast_sma: float
    slow_sma: float
    last_close: float
    generated_at: datetime
    is_recommendation: bool = False


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.get("/readyz")
def readyz():
    try:
        with connect() as db:
            db.execute("SELECT 1")
        return {"status": "ready", "provider": settings.market_data_provider}
    except Exception as exc:
        raise HTTPException(status_code=503, detail="PostgreSQL is unavailable") from exc


@app.get("/symbols")
def symbols():
    return {"symbols": [s.strip().upper() for s in settings.default_symbols.split(",") if s.strip()]}


@app.get("/market-data/{symbol}")
def market_data(symbol: str, days: int = Query(default=90, ge=30, le=365)):
    try:
        bars = get_daily_bars(symbol.upper(), days, settings)
        store_bars(settings.database_path, symbol.upper(), bars)
        return {"symbol": symbol.upper(), "bars": bars}
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/signals/{symbol}", response_model=Signal)
def signal(symbol: str):
    try:
        bars = get_daily_bars(symbol.upper(), 60, settings)
        store_bars(settings.database_path, symbol.upper(), bars)
        closes = [bar["close"] for bar in bars]
        if len(closes) < 50:
            raise ValueError("At least 50 daily bars are required to calculate the SMA signal")
        fast = sum(closes[-20:]) / 20
        slow = sum(closes[-50:]) / 50
        return Signal(
            symbol=symbol.upper(), signal="above_sma" if fast > slow else "below_sma",
            fast_sma=round(fast, 4), slow_sma=round(slow, 4), last_close=closes[-1],
            generated_at=datetime.now(timezone.utc),
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/backtests/{symbol}")
def backtest_metrics(symbol: str):
    try:
        result = latest_backtest(symbol.upper())
        if result is None:
            raise HTTPException(status_code=404, detail="No rolling backtest metrics available yet")
        return result
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Backtest database is unavailable") from exc


@app.get("/account/reconciliation")
def account_reconciliation():
    try:
        result = latest_account_snapshot()
        if result is None:
            raise HTTPException(status_code=404, detail="No broker reconciliation has run yet")
        return result
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Account database is unavailable") from exc


@app.get("/risk")
def risk_status():
    try:
        from zoneinfo import ZoneInfo

        today = datetime.now(ZoneInfo("America/New_York")).date()
        result = get_risk_state(today)
        if result is None:
            return {"halted": False, "status": "no broker snapshot yet"}
        return result
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Risk database is unavailable") from exc

