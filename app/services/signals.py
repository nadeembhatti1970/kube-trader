import json
import logging
import time
import uuid
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
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
# Signals are evaluated on completed one-minute bars, so the reference price is only
# knowable one minute after the bar opens. Execution latency is measured from there.
SIGNAL_BAR_SECONDS = 60
# Symbols are evaluated concurrently because a minute of bars arrives in one batch.
SIGNAL_WORKERS = 4


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
        fetched = get_daily_bars(symbol, max(260, settings.regime_sma + 60), settings)
        bars = [bar for bar in fetched if date.fromisoformat(bar["time"]) < today]
        DAILY_CACHE[symbol] = {"session": today, "attempted_at": now, "bars": bars}
        return bars
    except Exception:
        DAILY_CACHE[symbol] = {"session": today, "attempted_at": now, "bars": None}
        log.exception("Could not refresh daily bars for %s strategies", symbol)
        return []


def regime_allows_entry() -> bool:
    """Block new daily entries while the regime symbol is below its SMA.

    Uses completed daily bars and fails closed when the regime cannot be evaluated.
    """
    if not settings.regime_filter_enabled:
        return True
    try:
        bars = daily_bars_for_signal(settings.regime_symbol.upper())
        closes = [float(bar["close"]) for bar in bars]
        period = settings.regime_sma
        if len(closes) < period + 1:
            log.warning("Regime history for %s is too short; blocking new daily entries",
                        settings.regime_symbol)
            return False
        return closes[-1] > sum(closes[-period:]) / period
    except Exception:
        log.exception("Unable to evaluate the market regime; blocking new daily entries")
        return False


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
            closes = [float(bar["close"]) for bar in bars]
            if pool == "swing":
                signal_values = {
                    "signal_price": closes[-1],
                    "signal_reference_sma": sum(closes[-20:]) / 20,
                    "atr_fraction": atr_fraction,
                }
            else:
                signal_values = {
                    "fast_sma": sum(closes[-50:]) / 50,
                    "slow_sma": sum(closes[-200:]) / 200,
                }
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
            if current and not regime_allows_entry():
                log.info("Suppressing daily %s buy for %s: market regime is risk-off",
                         pool, symbol)
                continue
            stop_loss_pct = (min(max(settings.swing_atr_multiple * atr_fraction, 0.005), 0.25)
                             if pool == "swing" else settings.core_stop_loss_pct)
            signals.append({
                "symbol": symbol, "action": "buy" if current else "sell",
                "pool": pool, "price": price, "stop_loss_pct": stop_loss_pct,
                "daily_trend": "above" if current else "below",
                "strategy_bar_date": bar_date.isoformat(), "ts": timestamp,
                "signal_bar_seconds": SIGNAL_BAR_SECONDS,
                **signal_values,
            })
        except (ValueError, IndexError):
            # Inadequate history is normal while a new symbol is warming up.
            if bars:
                log.info("Daily %s strategy not ready for %s", pool, symbol)
        except Exception:
            log.exception("Could not evaluate daily %s strategy for %s", pool, symbol)
    return signals


def process_bars(redis_client, symbol: str, messages: list[tuple[str, dict]]):
    """Evaluate one symbol's bars in arrival order; other symbols run concurrently."""
    for message_id, fields in messages:
        try:
            event = json.loads(fields["data"])
            output_signals = []
            closes = recent_closes(symbol, settings.signal_slow_bars + 1)
            values = [float(row["close"]) for row in closes]
            if len(values) >= settings.signal_slow_bars:
                fast = sum(values[-settings.signal_fast_bars:]) / settings.signal_fast_bars
                slow = sum(values[-settings.signal_slow_bars:]) / settings.signal_slow_bars
                above = fast > slow
                previous = strategy_is_above(symbol)
                rsi_value = rsi(values)
                # Leave the state unchanged for an unconfirmed buy so a later bar can confirm it.
                if above and not previous and not entry_confirmed(fast, slow, rsi_value):
                    above = previous
                save_strategy_state(symbol, above)
                if above != previous:
                    action = "buy" if above else "sell"
                    output_signals.append({
                        "id": message_id, "symbol": symbol, "action": action,
                        "pool": "intraday", "price": float(event["close"]),
                        "stop_loss_pct": settings.stop_loss_pct,
                        "take_profit_pct": settings.take_profit_pct,
                        "fast_sma": fast, "slow_sma": slow,
                        "rsi_14": rsi_value, "ts": event["ts"],
                        "signal_bar_seconds": SIGNAL_BAR_SECONDS,
                    })
            output_signals.extend(daily_signals(
                symbol, float(event["close"]), event["ts"]
            ))
            for signal in output_signals:
                signal.setdefault("id", message_id)
                publish(SIGNAL_STREAM, signal)
            redis_client.xack(BAR_STREAM, "signal-workers", message_id)
        except Exception:
            log.exception("Unable to process bar event %s", message_id)


def group_by_symbol(messages) -> dict[str, list[tuple[str, dict]]]:
    """Keep each symbol's bars in order so state changes are applied in sequence."""
    grouped: dict[str, list[tuple[str, dict]]] = defaultdict(list)
    for message_id, fields in messages:
        try:
            symbol = json.loads(fields["data"])["symbol"]
        except Exception:
            symbol = ""
        grouped[symbol].append((message_id, fields))
    return grouped


def main():
    init_db()
    redis_client = client()
    ensure_group(redis_client, BAR_STREAM, "signal-workers")
    consumer = f"signals-{uuid.uuid4()}"
    while True:
        batches = read_group(redis_client, "signal-workers", consumer, BAR_STREAM,
                             count=50, block=1000)
        for _, messages in batches:
            grouped = group_by_symbol(messages)
            if len(grouped) <= 1:
                for symbol, items in grouped.items():
                    process_bars(redis_client, symbol, items)
                continue
            # A minute's worth of bars arrives at once; evaluating symbols serially
            # puts the last symbols several seconds behind their own bar close.
            with ThreadPoolExecutor(max_workers=SIGNAL_WORKERS) as pool:
                futures = [pool.submit(process_bars, redis_client, symbol, items)
                           for symbol, items in grouped.items()]
                for future in futures:
                    future.result()


if __name__ == "__main__":
    main()
