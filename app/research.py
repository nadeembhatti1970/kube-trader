from math import sqrt


STRATEGIES = {
    "intraday": {"fast": 5, "slow": 20, "periods_per_year": 390 * 252,
                 "frequency": "minute_bar"},
    "swing": {"fast": 1, "slow": 20, "periods_per_year": 252,
              "frequency": "daily"},
    "core": {"fast": 50, "slow": 200, "periods_per_year": 252,
             "frequency": "daily"},
}


def position_at(closes: list[float], index: int, strategy: str) -> bool | None:
    config = STRATEGIES[strategy]
    fast, slow = config["fast"], config["slow"]
    if index + 1 < slow:
        return None
    if strategy == "swing":
        return closes[index] > sum(closes[index - slow + 1:index + 1]) / slow
    return (sum(closes[index - fast + 1:index + 1]) / fast
            > sum(closes[index - slow + 1:index + 1]) / slow)


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


def evaluate_walk_forward(closes: list[float], strategy: str, cost_bps: float,
                          train_fraction: float) -> dict:
    """Evaluate close-based long-only rules with an in-sample/OOS chronological split."""
    if strategy not in STRATEGIES:
        raise ValueError(f"Unknown strategy: {strategy}")
    if len(closes) < STRATEGIES[strategy]["slow"] + 2:
        return {"bars": len(closes), "status": "insufficient_history",
                "required_bars": STRATEGIES[strategy]["slow"] + 2}
    if not 0 <= cost_bps <= 100:
        raise ValueError("cost_bps must be between 0 and 100")
    if not 0.5 <= train_fraction <= 0.9:
        raise ValueError("train_fraction must be between 0.5 and 0.9")

    positions = [position_at(closes, index, strategy) for index in range(len(closes))]
    split = int(len(closes) * train_fraction)
    cost_rate = cost_bps / 10_000
    in_sample, out_of_sample = [], []
    in_benchmark, out_benchmark = [], []
    in_trades = out_trades = 0
    for index in range(1, len(closes)):
        position = positions[index - 1]
        if position is None:
            continue
        prior_position = positions[index - 2] if index > 1 else None
        prior = bool(prior_position) if prior_position is not None else False
        current = bool(position)
        turnover = int(current != prior)
        market_return = closes[index] / closes[index - 1] - 1.0
        net_return = (market_return if current else 0.0) - cost_rate * turnover
        if index < split:
            in_sample.append(net_return)
            in_benchmark.append(market_return)
            in_trades += turnover
        else:
            out_of_sample.append(net_return)
            out_benchmark.append(market_return)
            out_trades += turnover

    periods = STRATEGIES[strategy]["periods_per_year"]
    return {
        "bars": len(closes), "frequency": STRATEGIES[strategy]["frequency"],
        "warmup_bars": STRATEGIES[strategy]["slow"],
        "cost_bps_per_side": cost_bps, "train_fraction": train_fraction,
        "in_sample": summarize(in_sample, in_benchmark, in_trades, periods),
        "out_of_sample": summarize(out_of_sample, out_benchmark, out_trades, periods),
        "limitations": [
            "Signals are calculated at bar close and assumed filled at that close.",
            "Costs are a configurable per-side estimate; actual spread, slippage, fees, and taxes vary.",
            "Annualized minute-bar Sharpe treats each observation as one regular-session minute and is approximate.",
            "No survivorship-bias control, corporate-action universe history, or parameter search is applied.",
        ],
    }
