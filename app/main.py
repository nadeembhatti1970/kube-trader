from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Query, Request
from pydantic import BaseModel

from app.market_data import get_daily_bars
from app.database import (connect, extended_hours_report, get_risk_state, latest_account_snapshot, latest_backtest,
                          latest_broker_activities, latest_universe_snapshot, managed_fills,
                          broker_activity_summary, broker_activity_sync_status, recent_closes)
from app.export_names import export_filename
from app.research import evaluate_walk_forward
from app.settings import get_settings, strategy_config
from app.storage import store_bars
from app.trade_accounting import build_realized_trade_report, reconcile_open_quantities

settings = get_settings()
app = FastAPI(title="Kube Trader", version=settings.app_version)
SNAPSHOT_STALE_SECONDS = max(120, settings.reconcile_interval_seconds * 4)


@app.middleware("http")
async def name_exported_reports(request: Request, call_next):
    response = await call_next(request)
    filename = export_filename(request.url.path) if request.method == "GET" else None
    if filename and response.status_code == 200:
        wants_download = request.query_params.get("download", "").lower() in {"1", "true", "yes"}
        disposition = "attachment" if wants_download else "inline"
        response.headers["Content-Disposition"] = f'{disposition}; filename="{filename}"'
    return response


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
            "app_version": settings.app_version,
            "cost_bps_per_side": costs,
            "train_fraction": split,
            "intraday_buy_confirmation_filter": {
                "min_spread_pct": settings.signal_min_spread_pct,
                "min_rsi_14": settings.signal_min_buy_rsi,
            },
            "intraday_data_bars": len(minute_closes),
            "daily_data_bars": len(daily_closes),
            "strategies": {
                "intraday": evaluate_walk_forward(
                    minute_closes, "intraday", costs, split,
                    settings.signal_min_spread_pct, settings.signal_min_buy_rsi,
                ),
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


@app.get("/account/broker")
def broker_account_report(limit: int = Query(default=200, ge=1, le=1000)):
    try:
        snapshot = latest_account_snapshot()
        if snapshot is None:
            raise HTTPException(status_code=404, detail="No broker reconciliation has run yet")
        activity_types = broker_activity_summary()
        sync_status = broker_activity_sync_status()
        captured_at = snapshot["captured_at"]
        age_seconds = (datetime.now(timezone.utc) - captured_at).total_seconds()
        warnings = list(snapshot["reconciliation"].get("warnings", []))
        if age_seconds > SNAPSHOT_STALE_SECONDS:
            warnings.append(
                f"Account snapshot is {age_seconds:.0f}s old; check that the execution worker "
                "is running before relying on this report")
        if snapshot["trading_mode"] is None or not snapshot["alpaca_account"]:
            warnings.append(
                "Snapshot has no recorded trading mode or account details; it predates those "
                "fields or the worker has not completed a reconciliation since upgrading")
        synced_after = [row["last_success_at"] for row in sync_status
                        if row.get("last_success_at") and row["last_success_at"] > captured_at]
        if synced_after:
            warnings.append(
                "Activity ledger was synced after the account snapshot was captured; "
                "the sections are not from the same instant")
        return {
            "captured_at": captured_at,
            "snapshot_age_seconds": age_seconds,
            "warnings": warnings,
            "trading_mode": snapshot["trading_mode"],
            "configured_trading_mode": "paper" if settings.alpaca_paper else "live",
            "account": snapshot["alpaca_account"],
            "equity": snapshot["equity"],
            "buying_power": snapshot["buying_power"],
            "positions": snapshot["positions"],
            "open_orders": snapshot["open_orders"],
            "orders": snapshot["orders"],
            "orders_may_be_truncated": len(snapshot["orders"]) >= 500,
            "reconciliation": snapshot["reconciliation"],
            "activity_ledger": {
                "total_stored": sum(row["count"] for row in activity_types),
                "by_type": activity_types,
                "sync_status": sync_status,
                "recent_activities": latest_broker_activities(limit),
            },
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Broker report is unavailable") from exc


@app.get("/research/extended-hours")
def extended_hours_liquidity(days: int = Query(default=7, ge=1, le=30)):
    """Read-only quote spreads and overnight eligibility sampled outside regular hours."""
    try:
        report = extended_hours_report(days)
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Extended-hours report is unavailable") from exc
    report["sampling_enabled"] = settings.extended_hours_sampling_enabled
    report["note"] = ("Spreads are in basis points of the mid price from the latest quote; "
                      "overnight uses Alpaca's overnight feed, other sessions the configured feed. "
                      "Exchange holidays are not modelled.")
    return report


@app.get("/trades")
def trade_report(limit: int = Query(default=100, ge=1, le=1000)):
    try:
        report = build_realized_trade_report(managed_fills())
        report["report_metadata"] = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "app_version": settings.app_version,
            "trading_mode": "paper" if settings.alpaca_paper else "live",
            "current_strategy_config": strategy_config(settings),
            "note": (
                "The current configuration applies to new orders only. Each trade also includes "
                "the configuration recorded with its entry and exit orders; null means that "
                "historical order predates configuration attribution."
            ),
        }
        all_trades = report["trades"]
        report["total_closed_trade_slices"] = len(all_trades)
        report["trades"] = all_trades[-limit:]
        snapshot = latest_account_snapshot()
        report["ledger_open_quantities"] = report["open_managed_quantities"]
        if snapshot is None:
            report["ledger_discrepancies"] = None
            report["open_quantities_basis"] = "order ledger only; no Alpaca snapshot available"
        else:
            check = reconcile_open_quantities(report["ledger_open_quantities"],
                                              snapshot["positions"])
            report["open_managed_quantities"] = check["open_quantities"]
            report["ledger_discrepancies"] = check["discrepancies"]
            report["open_quantities_basis"] = (
                "Alpaca positions at " + snapshot["captured_at"].isoformat()
                + "; ledger quantities above broker holdings are excluded")
        return report
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Trade report is unavailable") from exc


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
