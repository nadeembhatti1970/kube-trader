from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from app.market_data import get_daily_bars
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
    return {"status": "ready", "provider": settings.market_data_provider}


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


class PaperOrderRequest(BaseModel):
    symbol: str = Field(pattern=r"^[A-Z.]{1,10}$")
    qty: float = Field(gt=0, le=100)
    side: str = Field(pattern=r"^(buy|sell)$")


@app.post("/paper/orders")
def submit_paper_order(order: PaperOrderRequest):
    """Manual paper order only; disabled by default."""
    if not settings.trading_enabled:
        raise HTTPException(status_code=403, detail="Paper order submission is disabled")
    if not settings.alpaca_paper:
        raise HTTPException(status_code=403, detail="Only Alpaca paper trading is supported")
    if not settings.alpaca_api_key or not settings.alpaca_secret_key:
        raise HTTPException(status_code=503, detail="Alpaca paper API credentials are not configured")
    try:
        from alpaca.trading.client import TradingClient
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import MarketOrderRequest

        client = TradingClient(settings.alpaca_api_key, settings.alpaca_secret_key, paper=True)
        result = client.submit_order(MarketOrderRequest(
            symbol=order.symbol, qty=order.qty,
            side=OrderSide.BUY if order.side == "buy" else OrderSide.SELL,
            time_in_force=TimeInForce.DAY,
        ))
        return {"id": str(result.id), "status": str(result.status), "paper": True}
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Alpaca paper order failed: {exc}") from exc

