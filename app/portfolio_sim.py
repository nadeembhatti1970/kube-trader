"""Portfolio-level walk-forward simulation of the intraday strategy.

The per-symbol research endpoint cannot answer whether the strategy is worth
running: it has no capital limits, no concurrency, and no shared cash. This
simulator applies the live constraints (pool budget, symbol and sector caps,
open-risk cap, entry window/cutoff, end-of-day flattening, per-side costs) to all
watchlist symbols at once, and reports the uncertainty around the result.
"""
import math
from collections import defaultdict
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from app.services.signals import rsi
from app.validation import significance_summary

NY = ZoneInfo("America/New_York")
POOL_WEIGHTS = {"intraday": 0.20, "swing": 0.30, "core": 0.50}
PERIODS_PER_YEAR = 390 * 252

DEFAULT_CONFIG = {
    "starting_equity": 100_000.0,
    "pool": "intraday",
    "intraday_allocation_pct": 0.20,
    "fast_bars": 5,
    "slow_bars": 20,
    "min_spread_pct": 0.001,
    "min_buy_rsi": 55.0,
    "stop_loss_pct": 0.02,
    "take_profit_pct": 0.04,
    "cost_bps_per_side": 5.0,
    "max_trade_risk_pct": 0.01,
    "max_portfolio_open_risk_pct": 0.02,
    "max_symbol_exposure_pct": 0.05,
    "max_sector_exposure_pct": 0.25,
    "max_open_positions": 20,
    "max_shares_per_order": 100,
    "intraday_entry_start_minutes": 5,
    "intraday_entry_cutoff_minutes": 30,
    "intraday_flatten_minutes": 10,
    "min_hold_minutes": 0,
    "exit_confirmation_spread_pct": 0.0,
    # signal: exit on the reverse crossover; protective_only: stop/target/flatten
    # only; trailing: protective stop ratcheted behind the high.
    "exit_style": "signal",
    "trailing_stop_pct": 0.0,
    # Aggregate minute bars into N-minute bars before evaluating signals.
    "resample_minutes": 1,
}


def _as_datetime(value) -> datetime:
    if isinstance(value, datetime):
        moment = value
    else:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _minutes_to_close(moment: datetime) -> float:
    local = moment.astimezone(NY)
    return 16 * 60 - (local.hour * 60 + local.minute)


def _minutes_since_open(moment: datetime) -> float:
    local = moment.astimezone(NY)
    return (local.hour * 60 + local.minute) - (9 * 60 + 30)


def _in_regular_session(moment: datetime) -> bool:
    local = moment.astimezone(NY)
    minutes = local.hour * 60 + local.minute
    return local.weekday() < 5 and 9 * 60 + 30 <= minutes < 16 * 60


def _normalize(bars: list[dict]) -> list[dict]:
    normalized = []
    for bar in bars:
        moment = _as_datetime(bar.get("ts", bar.get("time")))
        if not _in_regular_session(moment):
            # The simulator is intraday-only; overnight or stale bars are not tradable.
            continue
        normalized.append({
            "ts": moment,
            "open": float(bar["open"]),
            "high": float(bar.get("high", bar["close"])),
            "low": float(bar.get("low", bar["close"])),
            "close": float(bar["close"]),
        })
    normalized.sort(key=lambda item: item["ts"])
    return normalized


def _rolling(closes: list[float], period: int) -> list[float | None]:
    values: list[float | None] = [None] * len(closes)
    total = 0.0
    for index, close in enumerate(closes):
        total += close
        if index >= period:
            total -= closes[index - period]
        if index >= period - 1:
            values[index] = total / period
    return values


def _resample(bars: list[dict], minutes: int) -> list[dict]:
    """Aggregate normalized minute bars into fixed N-minute OHLC buckets."""
    if minutes <= 1 or not bars:
        return bars
    buckets: dict[int, dict] = {}
    for bar in bars:
        bucket = int(bar["ts"].timestamp() // 60)
        bucket -= bucket % minutes
        item = buckets.get(bucket)
        if item is None:
            buckets[bucket] = {
                "ts": datetime.fromtimestamp(bucket * 60, tz=timezone.utc),
                "open": bar["open"],
                "high": bar["high"],
                "low": bar["low"],
                "close": bar["close"],
            }
            continue
        item["high"] = max(item["high"], bar["high"])
        item["low"] = min(item["low"], bar["low"])
        item["close"] = bar["close"]
    return [buckets[key] for key in sorted(buckets)]


def _prepare(bars: list[dict], config: dict) -> dict:
    closes = [bar["close"] for bar in bars]
    fast = _rolling(closes, int(config["fast_bars"]))
    slow = _rolling(closes, int(config["slow_bars"]))
    above: list[bool | None] = [None] * len(bars)
    confirmed: list[bool] = [False] * len(bars)
    for index in range(len(bars)):
        if fast[index] is None or slow[index] is None:
            continue
        above[index] = fast[index] > slow[index]
        if index and above[index] and not above[index - 1]:
            spread = (fast[index] - slow[index]) / slow[index] if slow[index] else 0.0
            window = closes[max(0, index - int(config["slow_bars"]) + 1):index + 1]
            confirmed[index] = (spread >= float(config["min_spread_pct"])
                                and rsi(window) >= float(config["min_buy_rsi"]))
    index_by_ts = {bar["ts"]: index for index, bar in enumerate(bars)}
    return {"bars": bars, "above": above, "confirmed": confirmed,
            "fast": fast, "slow": slow, "index": index_by_ts}


def _exit_confirmed(frame: dict, index: int, spread_pct: float) -> bool:
    """A bearish crossover must clear the same kind of threshold as a buy."""
    fast = frame["fast"][index]
    slow = frame["slow"][index]
    if fast is None or slow is None or slow <= 0:
        return False
    return (slow - fast) / slow >= spread_pct


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round(fraction * (len(ordered) - 1)))))
    return ordered[index]


def _metrics(equity_points: list[tuple[datetime, float]], exposures: list[float],
             trades: list[dict], turnover: float, cost: float) -> dict:
    if not equity_points:
        return {"total_return_pct": None, "max_drawdown_pct": None,
                "sharpe_annualized": None, "average_exposure_pct": None,
                "turnover_ratio": None, "cost_drag_pct": None}
    equities = [value for _, value in equity_points]
    starting = equities[0]
    final = equities[-1]
    returns = [equities[index] / equities[index - 1] - 1.0
               for index in range(1, len(equities)) if equities[index - 1] > 0]
    mean_return = sum(returns) / len(returns) if returns else 0.0
    variance = (sum((value - mean_return) ** 2 for value in returns) / (len(returns) - 1)
                if len(returns) > 1 else 0.0)
    stddev = math.sqrt(variance)
    peak = equities[0]
    max_drawdown = 0.0
    for value in equities:
        peak = max(peak, value)
        if peak:
            max_drawdown = min(max_drawdown, value / peak - 1.0)
    average_equity = sum(equities) / len(equities)
    return {
        "total_return_pct": (final / starting - 1.0) * 100 if starting else None,
        "max_drawdown_pct": max_drawdown * 100,
        "sharpe_annualized": (mean_return / stddev * math.sqrt(PERIODS_PER_YEAR)
                              if stddev else 0.0),
        "average_exposure_pct": (
            sum(exposures) / len(exposures) * 100 if exposures else None),
        "turnover_ratio": turnover / average_equity if average_equity else None,
        "cost_drag_pct": cost / average_equity * 100 if average_equity else None,
    }


def simulate_portfolio(bars_by_symbol: dict[str, list[dict]], config: dict | None = None,
                       sectors: dict[str, str] | None = None,
                       validation: dict | None = None) -> dict:
    """Run one portfolio simulation over every supplied symbol.

    Signals are evaluated on bar closes, entries and signal exits fill at the next
    bar open, and protective exits fill at the stop/target price (the stop is
    assumed first when a bar touches both). Positions cannot exceed the intraday
    pool budget, symbol, sector, statement-cash, open-risk, or share limits.
    """
    effective = dict(DEFAULT_CONFIG)
    effective.update(config or {})
    sectors = sectors or {}
    pool = str(effective["pool"])
    pool_weight = float(effective["intraday_allocation_pct"])
    if pool != "intraday":
        raise ValueError("The portfolio simulator currently supports the intraday pool only")
    exit_style = str(effective["exit_style"])
    if exit_style not in {"signal", "protective_only", "trailing"}:
        raise ValueError("exit_style must be signal, protective_only, or trailing")
    if float(effective["trailing_stop_pct"]) < 0:
        raise ValueError("trailing_stop_pct cannot be negative")
    if exit_style == "trailing" and float(effective["trailing_stop_pct"]) <= 0:
        raise ValueError("trailing exit_style requires trailing_stop_pct > 0")
    if int(effective["resample_minutes"]) < 1:
        raise ValueError("resample_minutes must be at least 1")

    frames = {}
    for symbol, bars in bars_by_symbol.items():
        normalized = _resample(_normalize(bars), int(effective["resample_minutes"]))
        if not normalized:
            continue
        frames[symbol.upper()] = _prepare(normalized, effective)
    if not frames:
        return {"status": "no_data", "config": effective, "trades": [],
                "metrics": {}, "equity": {}, "significance": significance_summary([])}

    timestamps = sorted({bar["ts"] for frame in frames.values() for bar in frame["bars"]})
    cash = float(effective["starting_equity"])
    positions: dict[str, dict] = {}
    pending: dict[str, str] = {}
    last_close: dict[str, float] = {}
    equity_points: list[tuple[datetime, float]] = []
    exposures: list[float] = []
    trades: list[dict] = []
    turnover = 0.0
    total_cost = 0.0
    cost_rate = float(effective["cost_bps_per_side"]) / 10_000.0

    def position_value() -> float:
        return sum(item["qty"] * last_close.get(symbol, item["entry_price"])
                   for symbol, item in positions.items())

    def open_risk() -> float:
        return sum(item["qty"] * max(item["entry_price"] - item["stop_price"], 0.0)
                   for item in positions.values())

    def exposure_by() -> dict:
        groups = defaultdict(float)
        for symbol, item in positions.items():
            groups["pool"] += item["qty"] * last_close.get(symbol, item["entry_price"])
            groups[f"symbol:{symbol}"] += item["qty"] * last_close.get(symbol, item["entry_price"])
            groups[f"sector:{item['sector']}"] += item["qty"] * last_close.get(
                symbol, item["entry_price"])
        return groups

    def close(symbol: str, price: float, reason: str, moment: datetime) -> None:
        nonlocal cash, turnover, total_cost
        item = positions.pop(symbol)
        notional = item["qty"] * price
        cost = notional * cost_rate
        cash += notional - cost
        turnover += notional
        total_cost += cost
        gross_pct = (price / item["entry_price"] - 1.0) * 100
        net_pct = gross_pct - 2.0 * float(effective["cost_bps_per_side"]) / 100.0
        trades.append({
            "symbol": symbol,
            "pool": pool,
            "quantity": item["qty"],
            "entry_ts": item["entry_ts"].isoformat(),
            "exit_ts": moment.isoformat(),
            "entry_price": item["entry_price"],
            "exit_price": price,
            "gross_return_pct": gross_pct,
            "net_return_pct": net_pct,
            "exit_reason": reason,
            "holding_minutes": (moment - item["entry_ts"]).total_seconds() / 60.0,
        })

    for moment in timestamps:
        active = []
        for symbol, frame in frames.items():
            index = frame["index"].get(moment)
            if index is not None:
                active.append((symbol, frame, index))

        # Exits first so selling frees cash for entries at the same timestamp.
        for symbol, frame, index in active:
            bar = frame["bars"][index]
            if symbol not in positions:
                continue
            item = positions[symbol]
            held_minutes = (moment - item["entry_ts"]).total_seconds() / 60.0
            if pending.get(symbol) == "sell" and index > item["entry_index"]:
                blocked = held_minutes < float(effective["min_hold_minutes"])
                if not blocked and float(effective["exit_confirmation_spread_pct"]) > 0:
                    blocked = not _exit_confirmed(
                        frame, index, float(effective["exit_confirmation_spread_pct"]))
                if not blocked:
                    close(symbol, bar["open"], "signal", moment)
                    pending.pop(symbol, None)
                    continue
                # Keep the sell pending so a confirmed reversal can exit on a later bar.
            remaining = _minutes_to_close(moment)
            if remaining <= float(effective["intraday_flatten_minutes"]):
                close(symbol, bar["open"], "eod_flatten", moment)
                pending.pop(symbol, None)
                continue
            if bar["open"] <= item["stop_price"]:
                close(symbol, bar["open"], item["stop_reason"], moment)
                pending.pop(symbol, None)
                continue
            if bar["open"] >= item["target_price"]:
                close(symbol, item["target_price"], "take_profit", moment)
                pending.pop(symbol, None)
                continue
            if bar["low"] <= item["stop_price"]:
                close(symbol, item["stop_price"], item["stop_reason"], moment)
                pending.pop(symbol, None)
                continue
            if bar["high"] >= item["target_price"]:
                close(symbol, item["target_price"], "take_profit", moment)
                pending.pop(symbol, None)
                continue
            trailing = float(effective["trailing_stop_pct"])
            if trailing > 0:
                # Ratchet from prior bars only, so this bar's high cannot set the
                # stop that this same bar's low is tested against.
                item["high_water"] = max(item["high_water"], bar["high"])
                ratcheted = round(item["high_water"] * (1.0 - trailing), 2)
                if ratcheted > item["stop_price"]:
                    item["stop_price"] = ratcheted
                    item["stop_reason"] = "trailing_stop"

        # Entries from the previous bar's signal.
        for symbol, frame, index in active:
            if pending.get(symbol) != "buy":
                continue
            pending.pop(symbol, None)
            if symbol in positions:
                continue
            bar = frame["bars"][index]
            price = bar["open"]
            if price <= 0:
                continue
            since_open = _minutes_since_open(moment)
            remaining = _minutes_to_close(moment)
            if since_open < float(effective["intraday_entry_start_minutes"]):
                continue
            if remaining <= max(float(effective["intraday_entry_cutoff_minutes"]),
                                float(effective["intraday_flatten_minutes"])):
                continue
            if len(positions) >= int(effective["max_open_positions"]):
                continue
            equity = cash + position_value()
            stop_pct = float(effective["stop_loss_pct"])
            target_pct = max(float(effective["take_profit_pct"]), stop_pct * 2.0)
            per_share_risk = max(price * stop_pct, 0.01)
            risk_budget = equity * float(effective["max_trade_risk_pct"])
            groups = exposure_by()
            available = min(
                equity * pool_weight - groups["pool"],
                cash,
                equity * float(effective["max_symbol_exposure_pct"]),
                equity * float(effective["max_sector_exposure_pct"])
                - groups[f"sector:{sectors.get(symbol, 'unclassified')}"],
            )
            if available <= 0:
                continue
            qty = min(
                math.floor(risk_budget / per_share_risk),
                math.floor(available / price),
                int(effective["max_shares_per_order"]) or math.floor(available / price),
            )
            risk_room = (equity * float(effective["max_portfolio_open_risk_pct"])
                         - open_risk())
            qty = min(qty, math.floor(max(risk_room, 0.0) / per_share_risk))
            if qty < 1:
                continue
            notional = qty * price
            cost = notional * cost_rate
            if notional + cost > cash:
                continue
            cash -= notional + cost
            turnover += notional
            total_cost += cost
            positions[symbol] = {
                "qty": qty,
                "entry_price": price,
                "stop_price": max(round(price * (1 - stop_pct), 2), 0.01),
                "target_price": max(round(price * (1 + target_pct), 2), 0.01),
                "stop_reason": "stop",
                "high_water": price,
                "entry_ts": moment,
                "entry_index": index,
                "sector": sectors.get(symbol, "unclassified"),
            }

        # Evaluate signals at the close and queue execution for the next bar.
        for symbol, frame, index in active:
            bar = frame["bars"][index]
            last_close[symbol] = bar["close"]
            above = frame["above"][index]
            if above is None:
                continue
            previous = frame["above"][index - 1] if index else None
            if previous is None:
                continue
            if above and not previous:
                if not frame["confirmed"][index]:
                    continue
                pending[symbol] = "buy"
            elif not above and previous:
                # Holding filters are applied when the pending sell is executed so
                # the exit can retry on later bars instead of being lost.
                if exit_style == "signal":
                    pending[symbol] = "sell"

        invested = position_value()
        equity_points.append((moment, cash + invested))
        exposures.append(invested / (cash + invested) if cash + invested else 0.0)

    final_equity = cash + position_value()
    metrics = _metrics(equity_points, exposures, trades, turnover, total_cost)
    net_returns = [trade["net_return_pct"] for trade in trades]
    wins = [value for value in net_returns if value > 0]
    losses = [value for value in net_returns if value < 0]
    confidence = float((validation or {}).get("confidence", 0.95))
    significance = significance_summary(
        net_returns, confidence=confidence,
        block_size=int((validation or {}).get("block_size", 5)),
        samples=int((validation or {}).get("samples", 2000)),
        seed=int((validation or {}).get("seed", 7)),
    )
    metrics.update({
        "closed_trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": len(wins) / len(trades) * 100 if trades else None,
        "profit_factor": (sum(wins) / abs(sum(losses)) if losses else None),
        "average_net_trade_return_pct": (
            sum(net_returns) / len(net_returns) if net_returns else None),
    })
    reasons = defaultdict(int)
    for trade in trades:
        reasons[trade["exit_reason"]] += 1
    benchmark = []
    for frame in frames.values():
        bars = frame["bars"]
        if len(bars) >= 2 and bars[0]["close"] > 0:
            benchmark.append(bars[-1]["close"] / bars[0]["close"] - 1.0)
    benchmark_return = (sum(benchmark) / len(benchmark) * 100 if benchmark else None)
    average_exposure = metrics.get("average_exposure_pct")
    benchmark_at_exposure = (
        benchmark_return * average_exposure / 100.0
        if benchmark_return is not None and average_exposure is not None else None)
    return {
        "status": "ok",
        "simulation": "portfolio-intraday-v1",
        "config": effective,
        "period": {
            "symbols": len(frames),
            "bars": sum(len(frame["bars"]) for frame in frames.values()),
            "first_timestamp": timestamps[0].isoformat(),
            "last_timestamp": timestamps[-1].isoformat(),
        },
        "equity": {
            "starting": float(effective["starting_equity"]),
            "final": final_equity,
        },
        "metrics": metrics,
        "exit_reasons": dict(sorted(reasons.items())),
        "significance": significance,
        "benchmark": {
            "basis": "equal_weight_buy_and_hold",
            "return_pct": benchmark_return,
            "return_at_average_exposure_pct": benchmark_at_exposure,
            "excess_return_at_exposure_pct": (
                metrics["total_return_pct"] - benchmark_at_exposure
                if metrics.get("total_return_pct") is not None
                and benchmark_at_exposure is not None else None),
            "note": (
                "Equal-weight first-to-last close of the simulated symbols; not SPY. "
                "The exposure-matched figure scales that return by the strategy's average "
                "invested share, so a mostly-cash run is not credited with avoiding a "
                "market decline."
            ),
        },
        "trades": trades[-500:],
        "limitations": [
            "One long position per symbol; partial fills and market impact are not modelled.",
            "OHLC cannot reveal intrabar ordering; a bar touching stop and target is treated as a stop.",
            "Costs are a per-side estimate covering spread, slippage, and fees.",
            "Only the intraday intraday pool is simulated; swing and core daily strategies are excluded.",
            "Universe membership is taken as fixed over the window; delisted or added symbols are not modelled.",
        ],
    }
