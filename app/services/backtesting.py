import json
import logging
import uuid

from app.database import init_db, recent_closes, save_backtest
from app.services.signals import rsi
from app.settings import get_settings
from app.streams import BAR_STREAM, client, ensure_group, read_group

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("backtesting")
settings = get_settings()


def evaluate(closes: list[float], fast_period: int, slow_period: int,
             min_spread_pct: float = 0.0, min_buy_rsi: float = 0.0) -> dict | None:
    """Replay the live crossover rule, including the buy confirmation filter."""
    if len(closes) < slow_period + 1:
        return None
    equity, peak, max_drawdown = 1.0, 1.0, 0.0
    returns = []
    held = False
    held_by_decision = {slow_period - 1: False}
    for decision in range(slow_period, len(closes)):
        window = closes[decision - slow_period - 1 if decision > slow_period else 0:decision]
        fast = sum(closes[decision-fast_period:decision]) / fast_period
        slow = sum(closes[decision-slow_period:decision]) / slow_period
        above = fast > slow
        if above and not held:
            spread = (fast - slow) / slow if slow else 0.0
            above = spread >= min_spread_pct and rsi(window) >= min_buy_rsi
        held = above
        held_by_decision[decision] = held
    for index in range(slow_period, len(closes)):
        previous_position = 1.0 if held_by_decision[index - 1] else 0.0
        bar_return = (closes[index] / closes[index - 1] - 1.0) * previous_position
        returns.append(bar_return)
        equity *= 1.0 + bar_return
        peak = max(peak, equity)
        max_drawdown = min(max_drawdown, equity / peak - 1.0)
    mean = sum(returns) / len(returns)
    variance = sum((value - mean) ** 2 for value in returns) / max(1, len(returns) - 1)
    stddev = variance ** 0.5
    return {"bars": len(returns), "total_return": equity - 1,
            "max_drawdown": max_drawdown, "sharpe": mean / stddev if stddev else 0.0}


def main():
    init_db()
    redis_client = client()
    ensure_group(redis_client, BAR_STREAM, "backtest-workers")
    consumer = f"backtest-{uuid.uuid4()}"
    while True:
        batches = read_group(redis_client, "backtest-workers", consumer, BAR_STREAM,
                             count=50, block=5000)
        for _, messages in batches:
            for message_id, fields in messages:
                try:
                    event = json.loads(fields["data"])
                    history = recent_closes(event["symbol"], 500)
                    metrics = evaluate([float(row["close"]) for row in history],
                                       settings.signal_fast_bars, settings.signal_slow_bars,
                                       settings.signal_min_spread_pct,
                                       settings.signal_min_buy_rsi)
                    if metrics:
                        save_backtest(event["symbol"], metrics)
                    redis_client.xack(BAR_STREAM, "backtest-workers", message_id)
                except Exception:
                    log.exception("Unable to backtest event %s", message_id)


if __name__ == "__main__":
    main()

