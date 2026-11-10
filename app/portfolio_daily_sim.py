"""Portfolio-level daily simulation of the swing and core strategies.

Runs both daily pools together in one account so shared cash, pool budgets,
symbol and sector caps, and the portfolio open-risk cap are respected. Signals
are evaluated on completed daily closes and fill at the next day's open; stops
are checked against each day's open and low. Nothing here places orders.
"""
import math
from bisect import bisect_left
from collections import defaultdict
from datetime import date, datetime
from statistics import mean, pstdev

from app.validation import significance_summary

PERIODS_PER_YEAR = 252

DEFAULT_CONFIG = {
    "starting_equity": 100_000.0,
    "swing_allocation_pct": 0.30,
    "core_allocation_pct": 0.50,
    "swing_sma": 20,
    "swing_atr_period": 14,
    "swing_atr_multiple": 2.0,
    "core_fast": 50,
    "core_slow": 200,
    "core_stop_loss_pct": 0.20,
    "max_trade_risk_pct": 0.01,
    "max_portfolio_open_risk_pct": 0.05,
    "max_symbol_exposure_pct": 0.05,
    "max_sector_exposure_pct": 0.25,
    "max_open_positions": 20,
    "max_shares_per_order": 100,
    "cost_bps_per_side": 5.0,
    "split_fraction": 0.7,
    # Risk-off regime filter: block new entries while the regime symbol closed
    # below its moving average on the prior session.
    "regime_filter_enabled": False,
    "regime_sma": 200,
}
POOL_WEIGHT_KEYS = {"swing": "swing_allocation_pct", "core": "core_allocation_pct"}


def daily_config(settings, starting_equity: float, cost_bps: float | None = None,
                 split_fraction: float = 0.7, regime_filter_enabled: bool = False,
                 regime_sma: int = 200) -> dict:
    """Build the daily simulation config from runtime settings."""
    return {
        "starting_equity": float(starting_equity),
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
        "split_fraction": split_fraction,
        "regime_filter_enabled": regime_filter_enabled,
        "regime_sma": regime_sma,
    }


def daily_validation_params(config: dict) -> dict:
    """Parameters that identify a daily verdict; shared by the API and the grid."""
    return {
        "swing_allocation_pct": config["swing_allocation_pct"],
        "core_allocation_pct": config["core_allocation_pct"],
        "swing_sma": config["swing_sma"],
        "swing_atr_period": config["swing_atr_period"],
        "swing_atr_multiple": config["swing_atr_multiple"],
        "core_fast": config["core_fast"],
        "core_slow": config["core_slow"],
        "core_stop_loss_pct": config["core_stop_loss_pct"],
        "cost_bps_per_side": config["cost_bps_per_side"],
        "split_fraction": config["split_fraction"],
        "regime_filter_enabled": config["regime_filter_enabled"],
        "regime_sma": config["regime_sma"],
    }


def _as_date(value) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def _normalize(bars: list[dict]) -> list[dict]:
    normalized = []
    for bar in bars:
        day = _as_date(bar.get("day", bar.get("time", bar.get("ts"))))
        close = float(bar["close"])
        if close <= 0:
            continue
        normalized.append({
            "day": day,
            "open": float(bar.get("open", close)),
            "high": float(bar.get("high", close)),
            "low": float(bar.get("low", close)),
            "close": close,
        })
    normalized.sort(key=lambda item: item["day"])
    return normalized


def _sma(values: list[float], period: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    total = 0.0
    for index, value in enumerate(values):
        total += value
        if index >= period:
            total -= values[index - period]
        if index >= period - 1:
            out[index] = total / period
    return out


def _atr_fraction(bars: list[dict], index: int, period: int) -> float:
    start = index - period + 1
    if start < 1:
        return 0.0
    ranges = []
    for position in range(start, index + 1):
        bar = bars[position]
        previous = bars[position - 1]["close"]
        ranges.append(max(bar["high"] - bar["low"],
                          abs(bar["high"] - previous),
                          abs(bar["low"] - previous)))
    close = bars[index]["close"]
    return sum(ranges) / period / close if close > 0 else 0.0


def _prepare(bars: list[dict], config: dict) -> dict:
    closes = [bar["close"] for bar in bars]
    swing_sma = _sma(closes, int(config["swing_sma"]))
    core_fast = _sma(closes, int(config["core_fast"]))
    core_slow = _sma(closes, int(config["core_slow"]))
    swing_above: list[bool | None] = [
        None if value is None else close > value
        for close, value in zip(closes, swing_sma)
    ]
    core_above: list[bool | None] = [
        None if fast is None or slow is None else fast > slow
        for fast, slow in zip(core_fast, core_slow)
    ]
    index_by_day = {bar["day"]: index for index, bar in enumerate(bars)}
    return {
        "bars": bars,
        "swing_above": swing_above,
        "core_above": core_above,
        "index": index_by_day,
    }


def _regime_series(bars: list[dict], period: int) -> tuple[list[date], list[bool | None]]:
    normalized = _normalize(bars)
    closes = [bar["close"] for bar in normalized]
    average = _sma(closes, period)
    days = [bar["day"] for bar in normalized]
    values = [None if average[index] is None else closes[index] > average[index]
              for index in range(len(normalized))]
    return days, values


def _regime_allows(days: list[date], values: list[bool | None], day: date) -> bool:
    """Use the latest regime observation strictly before the session being traded."""
    index = bisect_left(days, day) - 1
    if index < 0:
        return True  # insufficient regime history; do not block entries
    value = values[index]
    return True if value is None else bool(value)


def _metrics(equity_points: list[tuple[date, float]], exposures: list[float],
             turnover: float, cost: float) -> dict:
    if not equity_points:
        return {"total_return_pct": None, "max_drawdown_pct": None,
                "sharpe_annualized": None, "average_exposure_pct": None,
                "turnover_ratio": None, "cost_drag_pct": None}
    equities = [value for _, value in equity_points]
    starting, final = equities[0], equities[-1]
    returns = [equities[index] / equities[index - 1] - 1.0
               for index in range(1, len(equities)) if equities[index - 1] > 0]
    mean_return = mean(returns) if returns else 0.0
    deviation = pstdev(returns) if len(returns) > 1 else 0.0
    peak = equities[0]
    drawdown = 0.0
    for value in equities:
        peak = max(peak, value)
        if peak:
            drawdown = min(drawdown, value / peak - 1.0)
    average_equity = mean(equities)
    return {
        "total_return_pct": (final / starting - 1.0) * 100 if starting else None,
        "max_drawdown_pct": drawdown * 100,
        "sharpe_annualized": (mean_return / deviation * math.sqrt(PERIODS_PER_YEAR)
                              if deviation else 0.0),
        "average_exposure_pct": (
            mean(exposures) * 100 if exposures else None),
        "turnover_ratio": turnover / average_equity if average_equity else None,
        "cost_drag_pct": cost / average_equity * 100 if average_equity else None,
    }


def _trade_summary(trades: list[dict]) -> dict:
    values = [trade["net_return_pct"] for trade in trades]
    wins = [value for value in values if value > 0]
    losses = [value for value in values if value < 0]
    return {
        "closed_trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": len(wins) / len(values) * 100 if values else None,
        "profit_factor": sum(wins) / abs(sum(losses)) if losses else None,
        "average_net_trade_return_pct": mean(values) if values else None,
    }


def simulate_daily_portfolio(bars_by_symbol: dict[str, list[dict]],
                             config: dict | None = None,
                             sectors: dict[str, str] | None = None,
                             validation: dict | None = None,
                             regime_bars: list[dict] | None = None) -> dict:
    """Simulate the swing and core pools together over supplied daily bars."""
    effective = dict(DEFAULT_CONFIG)
    effective.update(config or {})
    sectors = sectors or {}
    if not 0.5 <= float(effective["split_fraction"]) <= 0.9:
        raise ValueError("split_fraction must be between 0.5 and 0.9")
    if float(effective["cost_bps_per_side"]) < 0:
        raise ValueError("cost_bps_per_side cannot be negative")
    regime_enabled = bool(effective["regime_filter_enabled"])
    regime_sma = int(effective["regime_sma"])
    if regime_enabled:
        if not regime_bars:
            raise ValueError("regime_filter_enabled requires regime_bars")
        regime_days, regime_values = _regime_series(regime_bars, regime_sma)
    else:
        regime_days, regime_values = [], []

    frames = {}
    for symbol, bars in bars_by_symbol.items():
        normalized = _normalize(bars)
        if len(normalized) < 2:
            continue
        frames[symbol.upper()] = _prepare(normalized, effective)
    if not frames:
        return {"status": "no_data", "config": effective, "trades": [],
                "metrics": {}, "significance": significance_summary([])}

    days = sorted({bar["day"] for frame in frames.values() for bar in frame["bars"]})
    cash = float(effective["starting_equity"])
    positions: dict[str, dict] = {}
    pending: dict[str, tuple[str, str]] = {}
    last_close: dict[str, float] = {}
    equity_points: list[tuple[date, float]] = []
    exposures: list[float] = []
    trades: list[dict] = []
    cost_events: list[tuple[date, float]] = []
    turnover_events: list[tuple[date, float]] = []
    excess_points: list[tuple[date, float]] = []
    cost_rate = float(effective["cost_bps_per_side"]) / 10_000.0
    weights = {pool: float(effective[key]) for pool, key in POOL_WEIGHT_KEYS.items()}

    def position_value() -> float:
        return sum(item["qty"] * last_close.get(symbol, item["entry_price"])
                   for symbol, item in positions.items())

    def open_risk() -> float:
        return sum(item["qty"] * max(item["entry_price"] - item["stop_price"], 0.0)
                   for item in positions.values())

    def close(symbol: str, price: float, reason: str, moment: date) -> None:
        item = positions.pop(symbol)
        notional = item["qty"] * price
        cash_delta = notional - notional * cost_rate
        cost = notional * cost_rate
        trades.append({
            "symbol": symbol,
            "pool": item["pool"],
            "quantity": item["qty"],
            "entry_day": item["entry_day"].isoformat(),
            "exit_day": moment.isoformat(),
            "entry_price": item["entry_price"],
            "exit_price": price,
            "gross_return_pct": (price / item["entry_price"] - 1.0) * 100,
            "net_return_pct": ((price / item["entry_price"] - 1.0) * 100
                               - 2.0 * float(effective["cost_bps_per_side"]) / 100.0),
            "exit_reason": reason,
            "holding_days": (moment - item["entry_day"]).days,
        })
        cost_events.append((moment, cost))
        turnover_events.append((moment, notional))
        return cash_delta

    for day in days:
        active = [(symbol, frame, frame["index"].get(day))
                  for symbol, frame in frames.items()]
        active = [item for item in active if item[2] is not None]

        for symbol, frame, index in active:
            if symbol not in positions:
                continue
            bar = frame["bars"][index]
            item = positions[symbol]
            action = pending.get(symbol)
            if action and action[0] == "sell" and action[1] == item["pool"]:
                cash += close(symbol, bar["open"], "signal", day)
                pending.pop(symbol, None)
                continue
            if bar["open"] <= item["stop_price"]:
                cash += close(symbol, bar["open"], "stop", day)
                pending.pop(symbol, None)
                continue
            if bar["low"] <= item["stop_price"]:
                cash += close(symbol, item["stop_price"], "stop", day)
                pending.pop(symbol, None)

        for symbol, frame, index in active:
            action = pending.get(symbol)
            if not action or action[0] != "buy" or symbol in positions:
                continue
            pending.pop(symbol, None)
            if not _regime_allows(regime_days, regime_values, day):
                continue
            pool = action[1]
            bar = frame["bars"][index]
            price = bar["open"]
            if price <= 0 or len(positions) >= int(effective["max_open_positions"]):
                continue
            equity = cash + position_value()
            if pool == "swing":
                atr = _atr_fraction(frame["bars"], index, int(effective["swing_atr_period"]))
                stop_pct = min(max(float(effective["swing_atr_multiple"]) * atr,
                                   0.005), 0.25)
            else:
                stop_pct = min(max(float(effective["core_stop_loss_pct"]), 0.005), 0.25)
            sector = sectors.get(symbol, "unclassified")
            pool_exposure = sum(
                item["qty"] * last_close.get(held, item["entry_price"])
                for held, item in positions.items() if item["pool"] == pool)
            sector_exposure = sum(
                item["qty"] * last_close.get(held, item["entry_price"])
                for held, item in positions.items() if item["sector"] == sector)
            available = min(
                equity * weights[pool] - pool_exposure,
                cash,
                equity * float(effective["max_symbol_exposure_pct"]),
                equity * float(effective["max_sector_exposure_pct"]) - sector_exposure,
            )
            if available <= 0:
                continue
            per_share_risk = max(price * stop_pct, 0.01)
            risk_budget = equity * min(float(effective["max_trade_risk_pct"]), 0.01)
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
            cost_events.append((day, cost))
            turnover_events.append((day, notional))
            positions[symbol] = {
                "pool": pool,
                "qty": qty,
                "entry_price": price,
                "stop_price": max(round(price * (1 - stop_pct), 2), 0.01),
                "entry_day": day,
                "sector": sector,
            }

        period_returns = []
        for symbol, frame, index in active:
            bar = frame["bars"][index]
            previous_close = last_close.get(symbol)
            if previous_close and previous_close > 0:
                period_returns.append(bar["close"] / previous_close - 1.0)
            last_close[symbol] = bar["close"]
            signals = []
            for pool, series in (("swing", frame["swing_above"]),
                                 ("core", frame["core_above"])):
                current = series[index]
                previous = series[index - 1] if index else None
                if current is None or previous is None or current == previous:
                    continue
                signals.append((("buy" if current else "sell"), pool))
            for action, pool in signals:
                if action == "sell":
                    pending[symbol] = ("sell", pool)
                elif symbol not in positions:
                    pending[symbol] = ("buy", pool)

        invested = position_value()
        equity = cash + invested
        prior_exposure = exposures[-1] if exposures else 0.0
        strategy_return = (
            equity / equity_points[-1][1] - 1.0
            if equity_points and equity_points[-1][1] > 0 else 0.0)
        universe_return = (mean(period_returns) if period_returns else 0.0)
        excess_points.append(
            (day, strategy_return - universe_return * prior_exposure))
        equity_points.append((day, equity))
        exposures.append(invested / equity if equity else 0.0)

    final_equity = cash + position_value()
    confidence = float((validation or {}).get("confidence", 0.95))
    block_size = int((validation or {}).get("block_size", 5))
    samples = int((validation or {}).get("samples", 2000))
    seed = int((validation or {}).get("seed", 7))

    def metrics_for(equity_segment, exposure_segment, segment_trades,
                    segment_cost, segment_turnover):
        result = _metrics(equity_segment, exposure_segment,
                          segment_turnover, segment_cost)
        result.update(_trade_summary(segment_trades))
        return result

    def significance_for(segment_trades):
        values = [trade["net_return_pct"] for trade in segment_trades]
        return significance_summary(values, confidence=confidence,
                                    block_size=block_size, samples=samples, seed=seed)

    def benchmark_for(start: date, end: date | None):
        values = []
        for frame in frames.values():
            bars = [bar for bar in frame["bars"]
                    if bar["day"] >= start and (end is None or bar["day"] < end)]
            if len(bars) >= 2 and bars[0]["close"] > 0:
                values.append(bars[-1]["close"] / bars[0]["close"] - 1.0)
        return mean(values) * 100 if values else None

    def benchmark_relative_for(start: date, end: date | None):
        values = [value for moment, value in excess_points
                  if moment >= start and (end is None or moment < end)]
        if not values:
            return {"basis": "daily excess return", "granularity": "day",
                    "periods": 0, "mean_excess_pct": None, "ci_low_pct": None,
                    "ci_high_pct": None, "ci_above_zero": False,
                    "minimum_detectable_edge_pct": None}
        summary = significance_summary(
            values, confidence=confidence, block_size=block_size,
            samples=samples, seed=seed)

        def pct(key):
            value = summary.get(key)
            return value * 100 if value is not None else None

        return {
            "basis": ("daily strategy return minus equal-weight universe return "
                      "times the prior period's exposure"),
            "granularity": "day",
            "periods": len(values),
            "mean_excess_pct": pct("mean"),
            "ci_low_pct": pct("ci_low"),
            "ci_high_pct": pct("ci_high"),
            "ci_above_zero": summary["ci_above_zero"],
            "minimum_detectable_edge_pct": pct("minimum_detectable_edge"),
        }

    metrics = metrics_for(equity_points, exposures, trades,
                          sum(value for _, value in cost_events),
                          sum(value for _, value in turnover_events))
    significance = significance_for(trades)
    reasons = defaultdict(int)
    for trade in trades:
        reasons[trade["exit_reason"]] += 1
    benchmark_return = benchmark_for(days[0], None)
    average_exposure = metrics.get("average_exposure_pct")
    benchmark_at_exposure = (
        benchmark_return * average_exposure / 100.0
        if benchmark_return is not None and average_exposure is not None else None)

    def segment(start: date, end: date | None, equity_segment, exposure_segment):
        segment_trades = [
            trade for trade in trades
            if _as_date(trade["exit_day"]) >= start
            and (end is None or _as_date(trade["exit_day"]) < end)
        ]
        segment_cost = sum(
            value for moment, value in cost_events
            if moment >= start and (end is None or moment < end))
        segment_turnover = sum(
            value for moment, value in turnover_events
            if moment >= start and (end is None or moment < end))
        segment_metrics = metrics_for(
            equity_segment, exposure_segment, segment_trades,
            segment_cost, segment_turnover)
        segment_benchmark = benchmark_for(start, end)
        exposure = segment_metrics.get("average_exposure_pct")
        at_exposure = (
            segment_benchmark * exposure / 100.0
            if segment_benchmark is not None and exposure is not None else None)
        return {
            "days": len(equity_segment),
            "trades": len(segment_trades),
            "equity": {
                "starting": equity_segment[0][1] if equity_segment else None,
                "final": equity_segment[-1][1] if equity_segment else None,
            },
            "metrics": segment_metrics,
            "significance": significance_for(segment_trades),
            "benchmark": {
                "return_pct": segment_benchmark,
                "return_at_average_exposure_pct": at_exposure,
                "excess_return_at_exposure_pct": (
                    segment_metrics["total_return_pct"] - at_exposure
                    if segment_metrics.get("total_return_pct") is not None
                    and at_exposure is not None else None),
            },
            "benchmark_relative": benchmark_relative_for(start, end),
        }

    split_fraction = float(effective["split_fraction"])
    split_index = min(max(int(len(days) * split_fraction), 1), len(days) - 1)
    boundary = days[split_index]
    in_sample = segment(days[0], boundary,
                        equity_points[:split_index + 1],
                        exposures[:split_index + 1])
    out_of_sample = segment(boundary, None,
                            equity_points[split_index:], exposures[split_index:])

    return {
        "status": "ok",
        "simulation": "portfolio-daily-v1",
        "config": effective,
        "period": {
            "symbols": len(frames),
            "days": sum(len(frame["bars"]) for frame in frames.values()),
            "first_day": days[0].isoformat(),
            "last_day": days[-1].isoformat(),
        },
        "split_fraction": split_fraction,
        "regime": {
            "enabled": regime_enabled,
            "sma": regime_sma if regime_enabled else None,
            "basis": (
                "new entries are allowed only while the regime symbol closed above "
                "its SMA on the prior session" if regime_enabled else None),
        },
        "in_sample": in_sample,
        "out_of_sample": out_of_sample,
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
            "note": ("Equal-weight first-to-last close of the simulated symbols; not SPY. "
                     "The exposure-matched figure scales that return by the average "
                     "invested share."),
        },
        "benchmark_relative": benchmark_relative_for(days[0], None),
        "trades": trades[-500:],
        "limitations": [
            "Swing and core daily rules only; the intraday pools are not simulated.",
            "One long position per symbol; partial fills, liquidity, and market impact are not modelled.",
            "Signals fill at the next day's open and stops fill at the stop price, "
            "or at the open on a gap through it.",
            "Costs are a per-side estimate covering spread, slippage, and fees.",
            "The in-sample/out-of-sample split is one chronological cut of a continuous "
            "simulation; out-of-sample equity starts at the boundary value and open "
            "positions carry across.",
            "The regime filter gates new entries only; open positions are still managed "
            "by their stops and trend exits.",
        ],
    }
