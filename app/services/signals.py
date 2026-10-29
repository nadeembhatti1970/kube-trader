import json
import logging
import time
import uuid
from datetime import date, datetime
from zoneinfo import ZoneInfo

from app.database import (horizon_strategy_state, init_db, recent_closes,
                          save_horizon_strategy_state, save_strategy_state,
                          strategy_is_above)
from app.market_data import get_daily_bars
from app.settings import get_settings
from app.streams import BAR_STREAM, SIGNAL_STREAM, client, ensure_group, publish, read_group

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("signals")
settings = get_settings()
NY = ZoneInfo("America/New_York")
DAILY_CACHE: dict[str, dict] = {}


def rsi(closes: list[float], period: int = 14) -> float:
    changes = [b - a for a, b in zip(closes[-period - 1:], closes[-period:])]
    gains = sum(max(delta, 0) for delta in changes) / period
    losses = sum(max(-delta, 0) for delta in changes) / period
    if losses == 0:
        return 100.0
    return 100.0 - (100.0 / (1.0 + gains / losses))


def entry_confirmed(fast: float, slow: float, rsi_value: float) -> bool:
    """A buy crossover must be decisive and show momentum, which filters minute-bar noise."""
    spread = (fast - slow) / slow if slow else 0.0
    return (spread >= settings.signal_min_spread_pct
            and rsi_value >= settings.signal_min_buy_rsi)


def daily_bars_for_signal(symbol: str) -> list[dict]:
    """Fetch/cache completed daily bars; exclude today's still-forming candle."""
    today = datetime.now(NY).date()
    now = time.monotonic()
    cached = DAILY_CACHE.get(symbol)
    if cached and cached["session"] == today:
        if cached["bars"] is not None or now - cached["attempted_at"] < 900:
            return cached["bars"] or []
    try:
        fetched = get_daily_bars(symbol, 260, settings)
        bars = [bar for bar in fetched if date.fromisoformat(bar["time"]) < today]
        DAILY_CACHE[symbol] = {"session": today, "attempted_at": now, "bars": bars}
        return bars
    except Exception:
        DAILY_CACHE[symbol] = {"session": today, "attempted_at": now, "bars": None}
        log.exception("Could not refresh daily bars for %s strategies", symbol)
        return []


def daily_strategy_state(bars: list[dict], pool: str) -> tuple[bool, bool, float]:
    """Return (previous trend, current trend, ATR fraction) for a daily strategy."""
    closes = [float(bar["close"]) for bar in bars]
    if pool == "swing":
        period = 20
        if len(closes) < period + 1:
            raise ValueError("Swing strategy requires at least 21 completed daily bars")
        previous = closes[-2] > sum(closes[-period - 1:-1]) / period
        current = closes[-1] > sum(closes[-period:]) / period
        atr_period = settings.swing_atr_period
        if len(bars) < atr_period + 1:
            raise ValueError("Swing strategy requires more bars to calculate ATR")
        true_ranges = []
        for index in range(-atr_period, 0):
            bar = bars[index]
            previous_close = float(bars[index - 1]["close"])
            true_ranges.append(max(float(bar["high"]) - float(bar["low"]),
                                   abs(float(bar["high"]) - previous_close),
                                   abs(float(bar["low"]) - previous_close)))
        atr_fraction = (sum(true_ranges) / atr_period) / closes[-1]
        return previous, current, atr_fraction

    if pool == "core":
        if len(closes) < 201:
            raise ValueError("Core strategy requires at least 201 completed daily bars")
        previous_fast = sum(closes[-51:-1]) / 50
        previous_slow = sum(closes[-201:-1]) / 200
        current_fast = sum(closes[-50:]) / 50
        current_slow = sum(closes[-200:]) / 200
        return previous_fast > previous_slow, current_fast > current_slow, 0.0

    raise ValueError(f"Unknown daily strategy pool: {pool}")


def daily_signals(symbol: str, price: float, timestamp: str) -> list[dict]:
    bars = daily_bars_for_signal(symbol)
    signals = []
    for pool in ("swing", "core"):
        try:
            previous, current, atr_fraction = daily_strategy_state(bars, pool)
            bar_date = date.fromisoformat(bars[-1]["time"])
            state = horizon_strategy_state(symbol, pool)
            if state and state["last_bar_date"] >= bar_date:
                continue
            # Compare with durable state after outages; on first boot only detect the
            # latest daily-bar crossover instead of entering every existing uptrend.
            prior_state = bool(state["above_trend"]) if state else previous
            changed = prior_state != current
            save_horizon_strategy_state(symbol, pool, current, bar_date)
            if not changed:
                continue
            stop_loss_pct = (min(max(settings.swing_atr_multiple * atr_fraction, 0.005), 0.25)
                             if pool == "swing" else settings.core_stop_loss_pct)
            signals.append({
                "symbol": symbol, "action": "buy" if current else "sell",
                "pool": pool, "price": price, "stop_loss_pct": stop_loss_pct,
                "daily_trend": "above" if current else "below",
                "strategy_bar_date": bar_date.isoformat(), "ts": timestamp,
            })
        except (ValueError, IndexError):
            # Inadequate history is normal while a new symbol is warming up.
            if bars:
                log.info("Daily %s strategy not ready for %s", pool, symbol)
        except Exception:
            log.exception("Could not evaluate daily %s strategy for %s", pool, symbol)
    return signals


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
                    output_signals = []
                    closes = recent_closes(event["symbol"], settings.signal_slow_bars + 1)
                    values = [float(row["close"]) for row in closes]
                    if len(values) >= settings.signal_slow_bars:
                        fast = sum(values[-settings.signal_fast_bars:]) / settings.signal_fast_bars
                        slow = sum(values[-settings.signal_slow_bars:]) / settings.signal_slow_bars
                        above = fast > slow
                        previous = strategy_is_above(event["symbol"])
                        rsi_value = rsi(values)
                        # Leave the state unchanged for an unconfirmed buy so a later bar can confirm it.
                        if above and not previous and not entry_confirmed(fast, slow, rsi_value):
                            above = previous
                        save_strategy_state(event["symbol"], above)
                        if above != previous:
                            action = "buy" if above else "sell"
                            output_signals.append({
                                "id": message_id, "symbol": event["symbol"], "action": action,
                                "pool": "intraday", "price": float(event["close"]),
                                "stop_loss_pct": settings.stop_loss_pct,
                                "take_profit_pct": settings.take_profit_pct,
                                "fast_sma": fast, "slow_sma": slow,
                                "rsi_14": rsi_value, "ts": event["ts"],
                            })
                    output_signals.extend(daily_signals(
                        event["symbol"], float(event["close"]), event["ts"]
                    ))
                    for signal in output_signals:
                        signal.setdefault("id", message_id)
                        publish(SIGNAL_STREAM, signal)
                    redis_client.xack(BAR_STREAM, "signal-workers", message_id)
                except Exception:
                    log.exception("Unable to process bar event %s", message_id)


if __name__ == "__main__":
    main()

