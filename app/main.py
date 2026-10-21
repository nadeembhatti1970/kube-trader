from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel

from app.market_data import get_daily_bars
from app.database import (connect, get_risk_state, latest_account_snapshot, latest_backtest,
                          latest_universe_snapshot, recent_closes)
from app.research import evaluate_walk_forward
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
    return {"symbols": sorted({item["symbol"] for item in latest_universe_snapshot() if item.get("selected")})}


@app.get("/universe")
def universe():
    screened = latest_universe_snapshot()
    return {
        "source": {"type": "etf_holdings", "symbol": settings.etf_holdings_symbol},
        "candidates": [item["symbol"] for item in screened],
        "screened": screened,
        "filters": {
            "enabled": settings.universe_screen_enabled,
            "minimum_price": settings.min_symbol_price,
            "minimum_average_dollar_volume": settings.min_avg_dollar_volume,
            "maximum_symbols": settings.max_watchlist_symbols,
            "maximum_holdings_to_screen": settings.etf_max_holdings,
            "refresh_seconds": settings.universe_refresh_seconds,
        },
    }


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


@app.get("/research/backtests/{symbol}")
def research_backtest(
    symbol: str,
    days: int = Query(default=1260, ge=260, le=2520),
    cost_bps: float | None = Query(default=None, ge=0, le=100),
    train_fraction: float | None = Query(default=None, ge=0.5, le=0.9),
):
    ticker = symbol.upper()
    costs = settings.backtest_cost_bps if cost_bps is None else cost_bps
    split = settings.backtest_train_fraction if train_fraction is None else train_fraction
    try:
        minute_rows = recent_closes(ticker, settings.intraday_backtest_bars)
        daily_rows = get_daily_bars(ticker, days, settings)
        minute_closes = [float(row["close"]) for row in minute_rows]
        daily_closes = [float(row["close"]) for row in daily_rows]
        return {
            "symbol": ticker,
            "cost_bps_per_side": costs,
            "train_fraction": split,
            "intraday_data_bars": len(minute_closes),
            "daily_data_bars": len(daily_closes),
            "strategies": {
                "intraday": evaluate_walk_forward(minute_closes, "intraday", costs, split),
                "swing": evaluate_walk_forward(daily_closes, "swing", costs, split),
                "core": evaluate_walk_forward(daily_closes, "core", costs, split),
            },
        }
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Research backtest unavailable: {exc}") from exc


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

