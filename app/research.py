from datetime import date, datetime, time, timedelta
from math import sqrt
from zoneinfo import ZoneInfo

from app.services.signals import rsi


STRATEGIES = {
    "intraday": {"fast": 5, "slow": 20, "periods_per_year": 390 * 252,
                 "frequency": "minute_bar"},
    "swing": {"fast": 1, "slow": 20, "periods_per_year": 252,
              "frequency": "daily"},
    "core": {"fast": 50, "slow": 200, "periods_per_year": 252,
             "frequency": "daily"},
}
NY = ZoneInfo("America/New_York")


def position_at(closes: list[float], index: int, strategy: str) -> bool | None:
    config = STRATEGIES[strategy]
    fast, slow = config["fast"], config["slow"]
    if index + 1 < slow:
        return None
    if strategy == "swing":
        return closes[index] > sum(closes[index - slow + 1:index + 1]) / slow
    return (sum(closes[index - fast + 1:index + 1]) / fast
            > sum(closes[index - slow + 1:index + 1]) / slow)


def _position_at_periods(closes: list[float], index: int, strategy: str,
                         fast: int, slow: int) -> bool | None:
    if index + 1 < slow:
        return None
    if strategy == "swing":
        return closes[index] > sum(closes[index - slow + 1:index + 1]) / slow
    return (sum(closes[index - fast + 1:index + 1]) / fast
            > sum(closes[index - slow + 1:index + 1]) / slow)


def positions_for(closes: list[float], strategy: str, min_spread_pct: float = 0.0,
                  min_buy_rsi: float = 0.0) -> list[bool | None]:
    positions = []
    previous = False
    for index in range(len(closes)):
        position = position_at(closes, index, strategy)
        if position is None:
            positions.append(None)
            continue
        if strategy == "intraday" and position and not previous:
            config = STRATEGIES[strategy]
            fast = sum(closes[index - config["fast"] + 1:index + 1]) / config["fast"]
            slow = sum(closes[index - config["slow"] + 1:index + 1]) / config["slow"]
            window = closes[max(0, index - config["slow"] + 1):index + 1]
            spread = (fast - slow) / slow if slow else 0.0
            if spread < min_spread_pct or rsi(window) < min_buy_rsi:
                position = False
        positions.append(position)
        previous = position
    return positions


def summarize(returns: list[float], benchmark_returns: list[float], trades: int,
             periods_per_year: int) -> dict:
    if not returns:
        return {"observations": 0, "trades": trades, "total_return": None,
                "buy_and_hold_return": None, "max_drawdown": None,
                "sharpe_annualized": None}
    equity = peak = 1.0
    max_drawdown = 0.0
    for value in returns:
        equity *= max(0.0, 1.0 + value)
        peak = max(peak, equity)
        max_drawdown = min(max_drawdown, equity / peak - 1.0)
    mean = sum(returns) / len(returns)
    variance = sum((value - mean) ** 2 for value in returns) / max(1, len(returns) - 1)
    stddev = variance ** 0.5
    sharpe = (mean / stddev * sqrt(periods_per_year)) if stddev else 0.0
    return {"observations": len(returns), "trades": trades,
            "total_return": equity - 1.0,
            "buy_and_hold_return": _compound(benchmark_returns) - 1.0,
            "max_drawdown": max_drawdown,
            "sharpe_annualized": sharpe}


def _compound(returns: list[float]) -> float:
    equity = 1.0
    for value in returns:
        equity *= max(0.0, 1.0 + value)
    return equity


def _bar_timestamp(bar: dict) -> datetime | None:
    value = bar.get("ts", bar.get("time"))
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime.combine(value, time.min, tzinfo=NY)
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=NY)


def _intraday_minutes_to_close(bar: dict) -> float | None:
    timestamp = _bar_timestamp(bar)
    if timestamp is None:
        return None
    local = timestamp.astimezone(NY)
    return 16 * 60 - (local.hour * 60 + local.minute)


def _swing_atr_fraction(bars: list[dict], end: int, period: int) -> float:
    start = end - period + 1
    if start < 1:
        return 0.0
    ranges = []
    for index in range(start, end + 1):
        bar = bars[index]
        previous_close = float(bars[index - 1]["close"])
        ranges.append(max(
            float(bar["high"]) - float(bar["low"]),
            abs(float(bar["high"]) - previous_close),
            abs(float(bar["low"]) - previous_close),
        ))
    close = float(bars[end]["close"])
    return sum(ranges) / period / close if close > 0 else 0.0


def simulate_ohlc(
    bars: list[dict],
    strategy: str,
    cost_bps: float,
    min_spread_pct: float = 0.0,
    min_buy_rsi: float = 0.0,
    stop_loss_pct: float = 0.02,
    take_profit_pct: float = 0.04,
    intraday_flatten_minutes: int = 0,
    intraday_entry_cutoff_minutes: int = 0,
    swing_atr_period: int = 14,
    swing_atr_multiple: float = 2.0,
    core_stop_loss_pct: float = 0.20,
    fast_period: int | None = None,
    slow_period: int | None = None,
) -> dict:
    """Simulate long-only signals with next-open fills and protective OHLC exits."""
    if strategy not in STRATEGIES:
        raise ValueError(f"Unknown strategy: {strategy}")
    if not 0 <= cost_bps <= 100:
        raise ValueError("cost_bps must be between 0 and 100")
    if min_spread_pct < 0:
        raise ValueError("min_spread_pct cannot be negative")
    if not 0 <= min_buy_rsi <= 100:
        raise ValueError("min_buy_rsi must be between 0 and 100")
    if stop_loss_pct <= 0 or take_profit_pct <= 0:
        raise ValueError("stop_loss_pct and take_profit_pct must be positive")
    if intraday_flatten_minutes < 0 or intraday_entry_cutoff_minutes < 0:
        raise ValueError("intraday time limits cannot be negative")
    if swing_atr_period < 1 or swing_atr_multiple <= 0:
        raise ValueError("swing ATR settings must be positive")
    if not bars:
        return {"bar_returns": [], "benchmark_returns": [], "trades": [],
                "open_trade": None, "entry_count": 0}

    normalized = []
    for bar in bars:
        item = dict(bar)
        item["close"] = float(item["close"])
        item["open"] = float(item.get("open", item["close"]))
        item["high"] = float(item.get("high", item["close"]))
        item["low"] = float(item.get("low", item["close"]))
        if (min(item["open"], item["high"], item["low"], item["close"]) <= 0
                or item["high"] < max(item["open"], item["close"], item["low"])
                or item["low"] > min(item["open"], item["close"], item["high"])):
            raise ValueError("OHLC bars must be positive and internally consistent")
        normalized.append(item)

    if strategy == "intraday" and (
        intraday_flatten_minutes or intraday_entry_cutoff_minutes
    ):
        timestamps = [_bar_timestamp(bar) for bar in normalized]
        if any(timestamp is None for timestamp in timestamps):
            raise ValueError("Intraday time controls require timestamps on every bar")
        if timestamps != sorted(timestamps):
            raise ValueError("OHLC bars must be ordered chronologically")

    closes = [bar["close"] for bar in normalized]
    fast_period = (STRATEGIES[strategy]["fast"]
                   if fast_period is None else fast_period)
    slow_period = (STRATEGIES[strategy]["slow"]
                   if slow_period is None else slow_period)
    if fast_period < 1 or slow_period < fast_period:
        raise ValueError("SMA periods must satisfy 1 <= fast_period <= slow_period")
    first_decision = slow_period if strategy in ("swing", "core") else slow_period - 1
    if len(closes) <= first_decision:
        return {"bar_returns": [0.0] * len(bars),
                "benchmark_returns": [0.0] * len(bars),
                "trades": [], "open_trade": None, "entry_count": 0}

    signal_state = bool(_position_at_periods(
        closes, first_decision - 1, strategy, fast_period, slow_period,
    ))
    actions: list[dict | None] = [None] * len(normalized)
    for index in range(first_decision, len(normalized)):
        above = _position_at_periods(closes, index, strategy, fast_period, slow_period)
        if above is None:
            continue
        if strategy == "intraday" and above and not signal_state:
            fast = sum(closes[index - fast_period + 1:index + 1]) / fast_period
            slow = sum(closes[index - slow_period + 1:index + 1]) / slow_period
            window = closes[max(0, index - slow_period + 1):index + 1]
            spread = (fast - slow) / slow if slow else 0.0
            above = spread >= min_spread_pct and rsi(window) >= min_buy_rsi
        if above != signal_state:
            event_stop = stop_loss_pct
            if strategy == "swing":
                atr_fraction = _swing_atr_fraction(normalized, index, swing_atr_period)
                event_stop = min(max(swing_atr_multiple * atr_fraction, 0.005), 0.25)
            elif strategy == "core":
                event_stop = core_stop_loss_pct
            actions[index] = {
                "action": "buy" if above else "sell",
                "stop_loss_pct": min(max(event_stop, 0.005), 0.25),
            }
            signal_state = bool(above)

    cost_rate = cost_bps / 10_000
    bar_returns = [0.0] * len(normalized)
    benchmark_returns = [0.0] * len(normalized)
    closed_trades = []
    position = None
    entry_count = 0

    for index, bar in enumerate(normalized):
        if index:
            benchmark_returns[index] = bar["close"] / normalized[index - 1]["close"] - 1.0
        if index == 0:
            continue
        previous_close = normalized[index - 1]["close"]
        pending = actions[index - 1]
        open_position = position
        entry_cost = 0.0
        base_price = previous_close if open_position else None
        exit_price = None
        exit_reason = None

        if position and pending and pending["action"] == "sell":
            exit_price, exit_reason = bar["open"], "signal_next_open"
        elif position and strategy == "intraday" and intraday_flatten_minutes:
            remaining = _intraday_minutes_to_close(bar)
            if remaining is not None and remaining <= intraday_flatten_minutes:
                exit_price, exit_reason = bar["open"], "end_of_day_flatten"

        if exit_price is not None:
            gross = exit_price / position["entry_price"] - 1.0
            closed_trades.append({
                **position, "exit_index": index, "exit_price": exit_price,
                "exit_reason": exit_reason, "gross_return_pct": gross * 100,
                "net_return_pct": (gross - 2 * cost_rate) * 100,
            })
            bar_returns[index] = exit_price / base_price - 1.0 - cost_rate
            position = None
            continue

        if pending and pending["action"] == "buy" and position is None:
            remaining = _intraday_minutes_to_close(bar) if strategy == "intraday" else None
            entry_cutoff = max(intraday_entry_cutoff_minutes,
                               intraday_flatten_minutes if strategy == "intraday" else 0)
            if (remaining is None or not entry_cutoff or remaining > entry_cutoff):
                entry_price = bar["open"]
                stop_pct = min(max(pending["stop_loss_pct"], 0.005), 0.25)
                stop_price = max(round(entry_price * (1 - stop_pct), 2), 0.01)
                target_price = None
                if strategy == "intraday":
                    target_pct = max(take_profit_pct, stop_pct * 2)
                    target_price = max(
                        round(entry_price * (1 + target_pct), 2), stop_price + 0.01,
                    )
                position = {
                    "entry_index": index, "entry_price": entry_price,
                    "stop_price": stop_price,
                    "target_price": target_price,
                    "stop_loss_pct": stop_pct,
                }
                entry_count += 1
                entry_cost = cost_rate
                base_price = entry_price

        if position is None:
            continue

        stop = position["stop_price"]
        target = position["target_price"]
        if bar["open"] <= stop:
            exit_price, exit_reason = bar["open"], "stop_gap"
        elif target is not None and bar["open"] >= target:
            exit_price, exit_reason = target, "target"
        else:
            stop_hit = bar["low"] <= stop
            target_hit = target is not None and bar["high"] >= target
            if stop_hit:
                exit_price, exit_reason = stop, "stop"
            elif target_hit:
                exit_price, exit_reason = target, "target"

        if exit_price is not None:
            gross = exit_price / position["entry_price"] - 1.0
            closed_trades.append({
                **position, "exit_index": index, "exit_price": exit_price,
                "exit_reason": exit_reason, "gross_return_pct": gross * 100,
                "net_return_pct": (gross - 2 * cost_rate) * 100,
            })
            bar_returns[index] = exit_price / base_price - 1.0 - entry_cost - cost_rate
            position = None
        else:
            bar_returns[index] = bar["close"] / base_price - 1.0 - entry_cost

    return {
        "bar_returns": bar_returns,
        "benchmark_returns": benchmark_returns,
        "trades": closed_trades,
        "open_trade": position,
        "entry_count": entry_count,
    }


def _simulation_summary(bar_returns: list[float], benchmark_returns: list[float],
                        trades: list[dict], periods_per_year: int) -> dict:
    result = summarize(bar_returns, benchmark_returns, len(trades), periods_per_year)
    wins = [trade for trade in trades if trade["net_return_pct"] > 0]
    losses = [trade for trade in trades if trade["net_return_pct"] < 0]
    result.update({
        "closed_trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": len(wins) / len(trades) * 100 if trades else None,
        "average_net_trade_return_pct": (
            sum(trade["net_return_pct"] for trade in trades) / len(trades)
            if trades else None
        ),
        "exit_reasons": {
            reason: sum(trade["exit_reason"] == reason for trade in trades)
            for reason in sorted({trade["exit_reason"] for trade in trades})
        },
    })
    return result


def evaluate_ohlc_walk_forward(
    bars: list[dict],
    strategy: str,
    cost_bps: float,
    train_fraction: float,
    min_spread_pct: float = 0.0,
    min_buy_rsi: float = 0.0,
    stop_loss_pct: float = 0.02,
    take_profit_pct: float = 0.04,
    intraday_flatten_minutes: int = 0,
    intraday_entry_cutoff_minutes: int = 0,
    swing_atr_period: int = 14,
    swing_atr_multiple: float = 2.0,
    core_stop_loss_pct: float = 0.20,
    fast_period: int | None = None,
    slow_period: int | None = None,
) -> dict:
    """Evaluate live-style signals and OHLC protective exits over a chronological split."""
    if strategy not in STRATEGIES:
        raise ValueError(f"Unknown strategy: {strategy}")
    if not 0.5 <= train_fraction <= 0.9:
        raise ValueError("train_fraction must be between 0.5 and 0.9")
    effective_slow = STRATEGIES[strategy]["slow"] if slow_period is None else slow_period
    required = effective_slow + 2
    if len(bars) < required:
        return {"bars": len(bars), "status": "insufficient_history",
                "required_bars": required}

    simulation_args = (
        min_spread_pct, min_buy_rsi,
        stop_loss_pct, take_profit_pct, intraday_flatten_minutes,
        intraday_entry_cutoff_minutes, swing_atr_period, swing_atr_multiple,
        core_stop_loss_pct, fast_period, slow_period,
    )

    def simulate_at_cost(cost: float) -> dict:
        return simulate_ohlc(
            bars, strategy, cost, *simulation_args,
        )

    simulation = simulate_at_cost(cost_bps)
    split = int(len(bars) * train_fraction)
    periods = STRATEGIES[strategy]["periods_per_year"]
    trades = simulation["trades"]
    out_of_sample_trades = [
        trade for trade in trades if trade["exit_index"] >= split
    ]
    cost_sensitivity = []
    for additional_cost_bps in (5, 10):
        scenario_cost = cost_bps + additional_cost_bps
        if scenario_cost > 100:
            cost_sensitivity.append({
                "additional_cost_bps_per_side": additional_cost_bps,
                "status": "skipped",
                "reason": "scenario exceeds the 100 bps per-side simulator limit",
            })
            continue
        scenario = simulate_at_cost(scenario_cost)
        cost_sensitivity.append({
            "additional_cost_bps_per_side": additional_cost_bps,
            "cost_bps_per_side": scenario_cost,
            "status": "computed",
            "out_of_sample": _simulation_summary(
                scenario["bar_returns"][split:],
                scenario["benchmark_returns"][split:],
                [trade for trade in scenario["trades"]
                 if trade["exit_index"] >= split],
                periods,
            ),
        })
    return {
        "bars": len(bars),
        "frequency": STRATEGIES[strategy]["frequency"],
        "warmup_bars": effective_slow,
        "signal_periods": {
            "fast": STRATEGIES[strategy]["fast"] if fast_period is None else fast_period,
            "slow": effective_slow,
        },
        "cost_bps_per_side": cost_bps,
        "train_fraction": train_fraction,
        "buy_confirmation_filter": (
            {"min_spread_pct": min_spread_pct, "min_rsi_14": min_buy_rsi}
            if strategy == "intraday" else None
        ),
        "risk_and_exit_config": {
            "stop_loss_pct": stop_loss_pct,
            "take_profit_pct": take_profit_pct if strategy == "intraday" else None,
            "intraday_flatten_minutes": intraday_flatten_minutes,
            "intraday_entry_cutoff_minutes": intraday_entry_cutoff_minutes,
            "swing_atr_period": swing_atr_period,
            "swing_atr_multiple": swing_atr_multiple,
            "core_stop_loss_pct": core_stop_loss_pct,
        },
        "execution_model": {
            "signal_fill": "next bar open",
            "protective_exit_fill": "stop/target price, or open on a stop gap",
            "same_bar_stop_and_target": "stop first",
            "market_exit_fill": "next bar open",
            "costs": "configured basis points charged on each entry and exit",
            "risk_control_scope": (
                "account-wide limits and position sizing are recorded but not simulated"
            ),
        },
        "in_sample": _simulation_summary(
            simulation["bar_returns"][:split],
            simulation["benchmark_returns"][:split],
            [trade for trade in trades if trade["exit_index"] < split],
            periods,
        ),
        "out_of_sample": _simulation_summary(
            simulation["bar_returns"][split:],
            simulation["benchmark_returns"][split:],
            out_of_sample_trades,
            periods,
        ),
        "out_of_sample_cost_sensitivity": {
            "interpretation": (
                "Recomputed out-of-sample results with 5 and 10 additional basis points "
                "charged per side; these are cost stresses, not predictions of actual costs."
            ),
            "scenarios": cost_sensitivity,
        },
        "open_trade_at_end": simulation["open_trade"],
        "entry_count": simulation["entry_count"],
        "limitations": [
            ("Only one long position per symbol is simulated; sizing, partial fills, "
             "liquidity, and market impact are not modeled."),
            "Per-symbol cooldowns, daily loss limits, and the account drawdown circuit breaker are not simulated.",
            "OHLC cannot reveal within-bar price ordering; when both protective levels are touched, the stop is assumed first.",
            "Signal and market exits are filled at the next bar open; this approximates live order timing.",
            "Intraday flattening and entry cutoffs use New York time and assume a 16:00 regular-session close; holidays are not modeled.",
            "Costs are a configurable per-side estimate; actual spread, slippage, fees, and taxes vary.",
            "No survivorship-bias control, corporate-action universe history, or parameter search is applied.",
        ],
    }
