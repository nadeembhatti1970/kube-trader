from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Query, Request
from pydantic import BaseModel

from app.market_data import get_daily_bars
from app.database import (account_fee_activities, broker_activity_summary,
                          broker_activity_sync_status, connect, extended_hours_report,
                          get_risk_state, latest_account_snapshot, latest_backtest,
                          latest_broker_activities, latest_universe_snapshot, managed_fills,
                          minute_bar_coverage, recent_bars, recent_daily_bars,
                          save_position_assignment, save_strategy_validation)
from app.export_names import export_filename
from app.portfolio_daily_sim import simulate_daily_portfolio
from app.portfolio_sim import simulate_portfolio
from app.research import evaluate_ohlc_walk_forward
from app.settings import get_settings, strategy_config
from app.storage import store_bars
from app.trade_accounting import build_realized_trade_report, reconcile_open_quantities
from app.universe import sector_for_symbol
from app.validation import config_key, validation_config_key, validation_verdict

settings = get_settings()
app = FastAPI(title="Kube Trader", version=settings.app_version)
SNAPSHOT_STALE_SECONDS = max(120, settings.reconcile_interval_seconds * 4)
BENCHMARK_CACHE: dict[str, dict | None] = {}


def benchmark_return(trades: list[dict]) -> dict | None:
    """Close-to-close return of the ETF benchmark over the closed-trade window."""
    stamps = []
    for trade in trades:
        for key in ("entry_filled_at", "exit_filled_at"):
            value = trade.get(key)
            if value is None:
                continue
            if isinstance(value, str):
                value = datetime.fromisoformat(value)
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            stamps.append(value)
    if not stamps:
        return None
    start = min(stamps).date().isoformat()
    end = max(stamps).date().isoformat()
    symbol = (settings.etf_holdings_symbol or "SPY").upper()
    cache_key = f"{symbol}:{start}:{end}:{settings.market_data_provider}"
    if cache_key in BENCHMARK_CACHE:
        return BENCHMARK_CACHE[cache_key]
    result = None
    try:
        bars = get_daily_bars(symbol, 120, settings)
        window = [bar for bar in bars if start <= str(bar["time"])[:10] <= end]
        if len(window) >= 2:
            first = float(window[0]["close"])
            last = float(window[-1]["close"])
            result = {
                "symbol": symbol,
                "source": settings.market_data_provider,
                "start_date": str(window[0]["time"])[:10],
                "end_date": str(window[-1]["time"])[:10],
                "start_close": first,
                "end_close": last,
                "return_pct": (last / first - 1) * 100 if first else None,
                "note": (
                    "Close-to-close benchmark over the first and last closed-trade dates. "
                    "It is a reference point, not a portfolio simulation."
                ),
            }
    except Exception:
        result = None
    BENCHMARK_CACHE[cache_key] = result
    return result


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
    candidate_min_spread_pct: float | None = Query(default=None, ge=0),
    candidate_min_buy_rsi: float | None = Query(default=None, ge=0, le=100),
):
    ticker = symbol.upper()
    costs = settings.backtest_cost_bps if cost_bps is None else cost_bps
    split = settings.backtest_train_fraction if train_fraction is None else train_fraction
    try:
        minute_rows = recent_bars(ticker, settings.intraday_backtest_bars)
        daily_rows = get_daily_bars(ticker, days, settings)
        model_config = {
            "stop_loss_pct": settings.stop_loss_pct,
            "take_profit_pct": settings.take_profit_pct,
            "intraday_flatten_minutes": settings.intraday_flatten_minutes,
            "intraday_entry_cutoff_minutes": settings.intraday_entry_cutoff_minutes,
            "swing_atr_period": settings.swing_atr_period,
            "swing_atr_multiple": settings.swing_atr_multiple,
            "core_stop_loss_pct": settings.core_stop_loss_pct,
        }
        report = {
            "symbol": ticker,
            "app_version": settings.app_version,
            "strategy_config": strategy_config(settings),
            "backtest_model": "ohlc-next-open-v1",
            "cost_bps_per_side": costs,
            "train_fraction": split,
            "intraday_buy_confirmation_filter": {
                "min_spread_pct": settings.signal_min_spread_pct,
                "min_rsi_14": settings.signal_min_buy_rsi,
            },
            "intraday_data_bars": len(minute_rows),
            "daily_data_bars": len(daily_rows),
            "strategies": {
                "intraday": evaluate_ohlc_walk_forward(
                    minute_rows, "intraday", costs, split,
                    settings.signal_min_spread_pct, settings.signal_min_buy_rsi,
                    fast_period=settings.signal_fast_bars,
                    slow_period=settings.signal_slow_bars,
                    **model_config,
                ),
                "swing": evaluate_ohlc_walk_forward(
                    daily_rows, "swing", costs, split, **model_config,
                ),
                "core": evaluate_ohlc_walk_forward(
                    daily_rows, "core", costs, split, **model_config,
                ),
            },
        }
        if candidate_min_spread_pct is not None or candidate_min_buy_rsi is not None:
            spread = (settings.signal_min_spread_pct if candidate_min_spread_pct is None
                      else candidate_min_spread_pct)
            rsi = (settings.signal_min_buy_rsi if candidate_min_buy_rsi is None
                   else candidate_min_buy_rsi)
            report["intraday_candidate"] = {
                "note": (
                    "Research only; simulated with the same bars, costs, and risk assumptions "
                    "as the configured intraday baseline. No settings or orders are changed."
                ),
                "result": evaluate_ohlc_walk_forward(
                    minute_rows, "intraday", costs, split, spread, rsi,
                    fast_period=settings.signal_fast_bars,
                    slow_period=settings.signal_slow_bars,
                    **model_config,
                ),
            }
        return report
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Research backtest unavailable: {exc}") from exc


@app.get("/research/portfolio")
def research_portfolio(
    days: int = Query(default=5000, ge=100, le=50000),
    symbols: str | None = Query(default=None),
    persist: bool = Query(default=False),
    min_spread_pct: float | None = Query(default=None, ge=0),
    min_buy_rsi: float | None = Query(default=None, ge=0, le=100),
    fast_bars: int | None = Query(default=None, ge=1),
    slow_bars: int | None = Query(default=None, ge=2),
    cost_bps: float | None = Query(default=None, ge=0, le=100),
    min_hold_minutes: float | None = Query(default=None, ge=0),
    exit_confirmation_spread_pct: float | None = Query(default=None, ge=0),
    exit_style: str | None = Query(default=None),
    trailing_stop_pct: float | None = Query(default=None, ge=0, le=1),
    resample_minutes: int | None = Query(default=None, ge=1, le=60),
    split_fraction: float | None = Query(default=None, ge=0.5, le=0.9),
    premise: str | None = Query(default=None),
    opening_range_minutes: int | None = Query(default=None, ge=1, le=120),
    gap_up_pct: float | None = Query(default=None, ge=0, le=0.2),
):
    """Portfolio-level walk-forward research over the selected watchlist.

    Applies the live capital, concentration, timing, and cost constraints to every
    symbol at once and reports the uncertainty around the result. Nothing here
    changes orders or settings.
    """
    selected_premise = (premise or "sma_crossover").strip().lower()
    if selected_premise not in {"sma_crossover", "opening_range_breakout", "gap_up"}:
        raise HTTPException(
            status_code=400,
            detail="premise must be sma_crossover, opening_range_breakout, or gap_up")
    selected_exit_style = (exit_style or "signal").strip().lower()
    if selected_exit_style not in {"signal", "protective_only", "trailing"}:
        raise HTTPException(
            status_code=400,
            detail="exit_style must be signal, protective_only, or trailing")
    if selected_exit_style == "trailing" and not (trailing_stop_pct or 0) > 0:
        raise HTTPException(
            status_code=400,
            detail="trailing exit_style requires trailing_stop_pct > 0")
    if symbols:
        requested = [item.strip().upper() for item in symbols.split(",") if item.strip()]
    else:
        requested = [item["symbol"] for item in latest_universe_snapshot()
                     if item.get("selected")]
    requested = requested[:settings.portfolio_backtest_max_symbols]
    if not requested:
        raise HTTPException(status_code=404,
                            detail="No symbols selected; pass ?symbols= or run the universe screen")
    try:
        bars_by_symbol = {}
        for symbol in requested:
            rows = recent_bars(symbol, days)
            if rows:
                bars_by_symbol[symbol] = rows
        if not bars_by_symbol:
            raise HTTPException(status_code=404,
                                detail="No stored minute bars for the requested symbols")
        snapshot = latest_account_snapshot()
        starting_equity = (float(snapshot["equity"]) if snapshot else 100_000.0)
        config = {
            "starting_equity": starting_equity,
            "intraday_allocation_pct": settings.intraday_allocation_pct,
            "fast_bars": settings.signal_fast_bars if fast_bars is None else fast_bars,
            "slow_bars": settings.signal_slow_bars if slow_bars is None else slow_bars,
            "min_spread_pct": (settings.signal_min_spread_pct
                               if min_spread_pct is None else min_spread_pct),
            "min_buy_rsi": (settings.signal_min_buy_rsi
                            if min_buy_rsi is None else min_buy_rsi),
            "stop_loss_pct": settings.stop_loss_pct,
            "take_profit_pct": settings.take_profit_pct,
            "cost_bps_per_side": (settings.backtest_cost_bps
                                  if cost_bps is None else cost_bps),
            "max_trade_risk_pct": min(settings.max_trade_risk_pct, 0.01),
            "max_portfolio_open_risk_pct": settings.max_portfolio_open_risk_pct,
            "max_symbol_exposure_pct": settings.max_symbol_exposure_pct,
            "max_sector_exposure_pct": settings.max_sector_exposure_pct,
            "max_open_positions": settings.max_open_positions,
            "intraday_entry_start_minutes": settings.intraday_entry_start_minutes,
            "intraday_entry_cutoff_minutes": settings.intraday_entry_cutoff_minutes,
            "intraday_flatten_minutes": settings.intraday_flatten_minutes,
            "min_hold_minutes": min_hold_minutes or 0.0,
            "exit_confirmation_spread_pct": exit_confirmation_spread_pct or 0.0,
            "exit_style": selected_exit_style,
            "trailing_stop_pct": trailing_stop_pct or 0.0,
            "resample_minutes": resample_minutes or 1,
            "split_fraction": split_fraction if split_fraction is not None else 0.7,
            "premise": selected_premise,
            "opening_range_minutes": (opening_range_minutes
                                      if opening_range_minutes is not None else 15),
            "gap_up_pct": gap_up_pct if gap_up_pct is not None else 0.005,
        }
        result = simulate_portfolio(
            bars_by_symbol, config,
            sectors={symbol: sector_for_symbol(symbol, settings) for symbol in bars_by_symbol},
            validation={"confidence": settings.validation_confidence},
        )
        candidate = strategy_config(settings)
        candidate.update({
            "signal_fast_bars": config["fast_bars"],
            "signal_slow_bars": config["slow_bars"],
            "signal_min_spread_pct": config["min_spread_pct"],
            "signal_min_buy_rsi": config["min_buy_rsi"],
            "min_hold_minutes": config["min_hold_minutes"],
            "exit_confirmation_spread_pct": config["exit_confirmation_spread_pct"],
            "exit_style": config["exit_style"],
            "trailing_stop_pct": config["trailing_stop_pct"],
            "resample_minutes": config["resample_minutes"],
            "split_fraction": config["split_fraction"],
            "premise": config["premise"],
            "opening_range_minutes": config["opening_range_minutes"],
            "gap_up_pct": config["gap_up_pct"],
        })
        identity = validation_config_key(candidate)
        full_significance = result.get("significance", {})
        oos_significance = (result.get("out_of_sample") or {}).get("significance", {})
        validated = validation_verdict(oos_significance, settings.validation_min_trades)
        result["validation"] = {
            "config_hash": identity["key"],
            "config": identity["config"],
            "validated": validated,
            "basis": "out_of_sample",
            "scheme_version": identity["scheme_version"],
            "namespace": identity["namespace"],
            "minimum_trades": settings.validation_min_trades,
            "required_by_execution": settings.require_validated_strategy,
            "note": (
                "Validated means the out-of-sample bootstrap interval for the mean net "
                "trade return is entirely above zero and the out-of-sample sample meets "
                "the minimum trade count. It is not a guarantee of future returns."
            ),
        }
        if persist:
            save_strategy_validation(
                identity["key"], candidate.get("strategy", "unknown"), identity["config"],
                int(oos_significance.get("n") or 0), oos_significance.get("mean"),
                oos_significance.get("ci_low"), oos_significance.get("ci_high"), validated,
                {
                    "metrics": result.get("metrics"),
                    "significance_full_window": full_significance,
                    "significance_out_of_sample": oos_significance,
                    "in_sample": result.get("in_sample"),
                    "out_of_sample": result.get("out_of_sample"),
                    "period": result.get("period"),
                    "exit_reasons": result.get("exit_reasons"),
                    "benchmark": result.get("benchmark"),
                    "config": config,
                },
                scheme_version=identity["scheme_version"],
            )
        result["persisted"] = bool(persist)
        return result
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=503, detail=f"Portfolio research unavailable: {exc}") from exc


@app.get("/research/portfolio/daily")
def research_daily_portfolio(
    days: int = Query(default=400, ge=220, le=2520),
    symbols: str | None = Query(default=None),
    cost_bps: float | None = Query(default=None, ge=0, le=100),
    split_fraction: float | None = Query(default=None, ge=0.5, le=0.9),
    persist: bool = Query(default=False),
):
    """Portfolio-level simulation of the daily swing and core pools.

    Runs both pools together under the live pool budgets, concentration caps,
    risk sizing, and costs. It never places orders or changes settings.
    """
    if symbols:
        requested = [item.strip().upper() for item in symbols.split(",") if item.strip()]
    else:
        requested = [row["symbol"] for row in latest_universe_snapshot()
                     if row.get("selected")]
    requested = requested[:settings.portfolio_backtest_max_symbols]
    if not requested:
        raise HTTPException(status_code=404,
                            detail="No symbols selected; pass ?symbols= or run the universe screen")
    try:
        bars_by_symbol = {}
        for symbol in requested:
            rows = recent_daily_bars(symbol, days)
            if len(rows) >= 220:
                bars_by_symbol[symbol] = rows
        if not bars_by_symbol:
            raise HTTPException(
                status_code=404,
                detail="No stored daily bars; run the backfill with --timeframe day")
        snapshot = latest_account_snapshot()
        config = {
            "starting_equity": float(snapshot["equity"]) if snapshot else 100_000.0,
            "swing_allocation_pct": settings.swing_allocation_pct,
            "core_allocation_pct": settings.core_allocation_pct,
            "swing_sma": 20,
            "swing_atr_period": settings.swing_atr_period,
            "swing_atr_multiple": settings.swing_atr_multiple,
            "core_fast": 50,
            "core_slow": 200,
            "core_stop_loss_pct": settings.core_stop_loss_pct,
            "max_trade_risk_pct": min(settings.max_trade_risk_pct, 0.01),
            "max_portfolio_open_risk_pct": 0.05,
            "max_symbol_exposure_pct": settings.max_symbol_exposure_pct,
            "max_sector_exposure_pct": settings.max_sector_exposure_pct,
            "max_open_positions": settings.max_open_positions,
            "cost_bps_per_side": (settings.backtest_cost_bps
                                  if cost_bps is None else cost_bps),
            "split_fraction": split_fraction if split_fraction is not None else 0.7,
        }
        result = simulate_daily_portfolio(
            bars_by_symbol, config,
            sectors={symbol: sector_for_symbol(symbol, settings)
                     for symbol in bars_by_symbol},
            validation={"confidence": settings.validation_confidence},
        )
        identity = config_key("daily-swing-core", {
            "swing_sma": config["swing_sma"],
            "swing_atr_period": config["swing_atr_period"],
            "swing_atr_multiple": config["swing_atr_multiple"],
            "core_fast": config["core_fast"],
            "core_slow": config["core_slow"],
            "core_stop_loss_pct": config["core_stop_loss_pct"],
            "cost_bps_per_side": config["cost_bps_per_side"],
            "split_fraction": config["split_fraction"],
        })
        oos = (result.get("out_of_sample") or {}).get("significance", {})
        validated = validation_verdict(oos, settings.validation_min_trades)
        result["validation"] = {
            "config_hash": identity["key"],
            "config": identity["config"],
            "namespace": identity["namespace"],
            "scheme_version": identity["scheme_version"],
            "validated": validated,
            "basis": "out_of_sample",
            "minimum_trades": settings.validation_min_trades,
            "required_by_execution": settings.require_validated_strategy,
            "note": (
                "Validated means the out-of-sample bootstrap interval for the mean net "
                "trade return is entirely above zero and the out-of-sample sample meets "
                "the minimum trade count. It is not a guarantee of future returns."
            ),
        }
        if persist:
            save_strategy_validation(
                identity["key"], "daily-swing-core-v1", identity["config"],
                int(oos.get("n") or 0), oos.get("mean"),
                oos.get("ci_low"), oos.get("ci_high"), validated,
                {
                    "basis": "out_of_sample",
                    "config": config,
                    "in_sample": result.get("in_sample"),
                    "out_of_sample": result.get("out_of_sample"),
                    "metrics": result.get("metrics"),
                    "period": result.get("period"),
                },
                scheme_version=identity["scheme_version"],
            )
        result["persisted"] = bool(persist)
        return result
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=503, detail=f"Daily portfolio research unavailable: {exc}") from exc


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
        try:
            account_fees = account_fee_activities()
        except Exception:
            account_fees = None
        try:
            bar_coverage = minute_bar_coverage()
        except Exception:
            bar_coverage = None
        report = build_realized_trade_report(
            managed_fills(),
            account_fees=account_fees,
            latency_warn_seconds=settings.execution_latency_warn_seconds,
            bar_coverage=bar_coverage,
        )
        report["benchmark"] = benchmark_return(report["trades"])
        report["report_metadata"] = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "app_version": settings.app_version,
            "trading_mode": "paper" if settings.alpaca_paper else "live",
            "alpaca_data_feed": settings.alpaca_data_feed,
            "execution_latency_warn_seconds": settings.execution_latency_warn_seconds,
            "current_strategy_config": strategy_config(settings),
            "note": (
                "The current configuration applies to new orders only. Each trade also includes "
                "the configuration recorded with its entry and exit orders; null means no "
                "configuration was recorded and must not be inferred. Attribution fields identify "
                "whether each fill came from the managed ledger, a recorded parent order, or only "
                "a bot client-order ID."
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


@app.post("/admin/positions/{symbol}/adopt")
def adopt_position(symbol: str, pool: str = Query(...),
                   note: str | None = Query(default=None)):
    """Assign an existing broker position to a bot pool without placing any order.

    This is how a bot-created position that lost its provenance becomes managed again;
    it never adopts or alters unrelated holdings automatically.
    """
    if pool not in {"intraday", "swing", "core"}:
        raise HTTPException(status_code=400, detail="pool must be intraday, swing, or core")
    ticker = symbol.upper()
    try:
        snapshot = latest_account_snapshot()
        if snapshot is None:
            raise HTTPException(status_code=404,
                                detail="No broker reconciliation has run yet")
        held = {item["symbol"] for item in snapshot["positions"]}
        if ticker not in held:
            raise HTTPException(
                status_code=404, detail=f"{ticker} is not a current broker position")
        save_position_assignment(ticker, pool, note)
        return {
            "symbol": ticker,
            "pool": pool,
            "note": note,
            "applies_to": (
                "Reconciliation, stop repair, and end-of-day flattening from the next "
                "cycle. No position, order, or stop is created, moved, or cancelled."
            ),
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=503,
                            detail="Position assignment store is unavailable") from exc
