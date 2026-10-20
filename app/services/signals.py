import json
import logging
import uuid

from app.database import init_db, recent_closes, save_strategy_state, strategy_is_above
from app.settings import get_settings
from app.streams import BAR_STREAM, SIGNAL_STREAM, client, ensure_group, publish, read_group

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("signals")
settings = get_settings()


def rsi(closes: list[float], period: int = 14) -> float:
    changes = [b - a for a, b in zip(closes[-period - 1:], closes[-period:])]
    gains = sum(max(delta, 0) for delta in changes) / period
    losses = sum(max(-delta, 0) for delta in changes) / period
    if losses == 0:
        return 100.0
    return 100.0 - (100.0 / (1.0 + gains / losses))


def main():
    init_db()
    redis_client = client()
    ensure_group(redis_client, BAR_STREAM, "signal-workers")
    consumer = f"signals-{uuid.uuid4()}"
    while True:
        batches = read_group(redis_client, "signal-workers", consumer, BAR_STREAM,
                             count=50, block=5000)
        for _, messages in batches:
            for message_id, fields in messages:
                try:
                    event = json.loads(fields["data"])
                    closes = recent_closes(event["symbol"], settings.signal_slow_bars + 1)
                    values = [float(row["close"]) for row in closes]
                    if len(values) >= settings.signal_slow_bars:
                        fast = sum(values[-settings.signal_fast_bars:]) / settings.signal_fast_bars
                        slow = sum(values[-settings.signal_slow_bars:]) / settings.signal_slow_bars
                        above = fast > slow
                        previous = strategy_is_above(event["symbol"])
                        save_strategy_state(event["symbol"], above)
                        if above != previous:
                            action = "buy" if above else "sell"
                            publish(SIGNAL_STREAM, {
                                "id": message_id, "symbol": event["symbol"], "action": action,
                                "pool": "intraday", "price": float(event["close"]),
                                "stop_loss_pct": settings.stop_loss_pct,
                                "take_profit_pct": settings.take_profit_pct,
                                "fast_sma": fast, "slow_sma": slow,
                                "rsi_14": rsi(values), "ts": event["ts"],
                            })
                    redis_client.xack(BAR_STREAM, "signal-workers", message_id)
                except Exception:
                    log.exception("Unable to process bar event %s", message_id)


if __name__ == "__main__":
    main()

