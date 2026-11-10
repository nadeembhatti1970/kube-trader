"""Historical minute-bar backfill for research.

Writes bars to PostgreSQL only. It never publishes to the live ``market.bars``
stream, so backfilled history cannot trigger signals, backtests, or orders.

Usage:
    python -m app.services.backfill --days 30
    python -m app.services.backfill --days 60 --symbols AAPL,MSFT --feed sip
"""
import argparse
import logging
from datetime import datetime, timedelta, timezone

from alpaca.data.enums import Adjustment, DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

from app.database import (init_db, latest_universe_snapshot, save_bars,
                          save_daily_bars)
from app.settings import get_settings

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("backfill")
settings = get_settings()


def bars_to_rows(barset) -> list[dict]:
    """Convert an Alpaca BarSet into rows for the bars table."""
    rows = []
    data = getattr(barset, "data", None) or {}
    for symbol, bars in data.items():
        for bar in bars:
            rows.append({
                "symbol": str(symbol).upper(),
                "ts": bar.timestamp,
                "open": float(bar.open),
                "high": float(bar.high),
                "low": float(bar.low),
                "close": float(bar.close),
                "volume": int(bar.volume),
            })
    return rows


def daily_bars_to_rows(barset) -> list[dict]:
    """Convert an Alpaca BarSet into rows for the daily_bars table."""
    rows = []
    data = getattr(barset, "data", None) or {}
    for symbol, bars in data.items():
        for bar in bars:
            rows.append({
                "symbol": str(symbol).upper(),
                "day": bar.timestamp.date(),
                "open": float(bar.open),
                "high": float(bar.high),
                "low": float(bar.low),
                "close": float(bar.close),
                "volume": int(bar.volume),
            })
    return rows


def chunked(items: list, size: int):
    for index in range(0, len(items), max(1, size)):
        yield items[index:index + max(1, size)]


def resolve_symbols(explicit: str | None, limit: int) -> list[str]:
    if explicit:
        symbols = [item.strip().upper() for item in explicit.split(",") if item.strip()]
    else:
        symbols = [row["symbol"] for row in latest_universe_snapshot() if row.get("selected")]
    if not symbols:
        raise ValueError("No symbols selected; run ingestion first or pass --symbols")
    return sorted(dict.fromkeys(symbols))[:max(1, limit)]


def backfill(days: int, symbols: list[str], feed: DataFeed,
             batch_symbols: int = 10, timeframe: str = "minute") -> int:
    """Fetch and store historical bars; returns the number of rows written."""
    if timeframe not in {"minute", "day"}:
        raise ValueError("timeframe must be minute or day")
    if not settings.alpaca_api_key or not settings.alpaca_secret_key:
        raise RuntimeError("Alpaca credentials are required for a historical backfill")
    client = StockHistoricalDataClient(
        settings.alpaca_api_key, settings.alpaca_secret_key)
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    frame = TimeFrame.Day if timeframe == "day" else TimeFrame.Minute
    # Daily strategy signals are computed on adjusted closes, so adjust the history.
    adjustment = Adjustment.ALL if timeframe == "day" else Adjustment.RAW
    stored = 0
    for batch in chunked(symbols, batch_symbols):
        request = StockBarsRequest(
            symbol_or_symbols=batch,
            timeframe=frame,
            start=start,
            end=end,
            feed=feed,
            adjustment=adjustment,
        )
        barset = client.get_stock_bars(request)
        if timeframe == "day":
            rows = daily_bars_to_rows(barset)
            stored += save_daily_bars(rows)
        else:
            rows = bars_to_rows(barset)
            stored += save_bars(rows)
        log.info("Backfilled %s %s bars for %s symbols",
                 len(rows), timeframe, len(batch))
    return stored


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=None,
                        help="Calendar days of history (default 30 minute, 1000 daily)")
    parser.add_argument("--timeframe", choices=["minute", "day"], default="minute",
                        help="Bar size to backfill (default minute)")
    parser.add_argument("--symbols", default=None,
                        help="Comma-separated symbols; defaults to the selected universe")
    parser.add_argument("--batch-symbols", type=int, default=10,
                        help="Symbols per Alpaca request (default 10)")
    parser.add_argument("--feed", default=settings.alpaca_data_feed,
                        choices=["iex", "sip"], help="Alpaca data feed")
    args = parser.parse_args()
    days = args.days if args.days is not None else (
        1000 if args.timeframe == "day" else 30)
    if days < 1:
        raise SystemExit("--days must be at least 1")
    init_db()
    try:
        symbols = resolve_symbols(args.symbols, settings.etf_max_holdings)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    feed = DataFeed.SIP if args.feed.lower() == "sip" else DataFeed.IEX
    log.info("Backfilling %s symbols of %s bars for %s days from the %s feed",
             len(symbols), args.timeframe, days, feed.value)
    stored = backfill(days, symbols, feed, args.batch_symbols, args.timeframe)
    log.info("Backfill complete: %s %s bars across %s symbols",
             stored, args.timeframe, len(symbols))


if __name__ == "__main__":
    main()
