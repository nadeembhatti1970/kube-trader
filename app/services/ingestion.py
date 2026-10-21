import asyncio
import logging
from datetime import timezone

from alpaca.data.enums import DataFeed
from alpaca.data.live import StockDataStream

from app.database import init_db, record_bar, save_universe_snapshot
from app.settings import get_settings
from app.streams import BAR_STREAM, publish
from app.universe import select_symbols

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("ingestion")
settings = get_settings()


async def main():
    while not settings.market_stream_enabled:
        log.warning("Market stream is disabled; set MARKET_STREAM_ENABLED=true to start ingestion")
        await asyncio.sleep(60)
    if not settings.alpaca_api_key or not settings.alpaca_secret_key:
        raise RuntimeError("Alpaca API credentials are required for the intraday stream")
    init_db()
    feed = DataFeed.SIP if settings.alpaca_data_feed.lower() == "sip" else DataFeed.IEX
    stream = StockDataStream(settings.alpaca_api_key, settings.alpaca_secret_key, feed=feed,
                             data_timeout=90)

    async def on_bar(bar):
        ts = bar.timestamp.astimezone(timezone.utc)
        event = {"symbol": bar.symbol, "ts": ts.isoformat(), "open": float(bar.open),
                 "high": float(bar.high), "low": float(bar.low), "close": float(bar.close),
                 "volume": int(bar.volume)}
        record_bar(event)
        publish(BAR_STREAM, event)

    if settings.universe_refresh_seconds < 3600:
        raise ValueError("UNIVERSE_REFRESH_SECONDS must be at least 3600 seconds")

    symbols, screen = await asyncio.to_thread(select_symbols, settings)
    save_universe_snapshot(screen)
    log.info("ETF %s universe selected %s/%s symbols: %s",
             settings.etf_holdings_symbol, len(symbols), len(screen), symbols)
    if not symbols:
        raise RuntimeError("No symbols selected for Alpaca market data stream")
    stream.subscribe_bars(on_bar, *symbols)

    async def refresh_universe():
        nonlocal symbols
        while True:
            await asyncio.sleep(settings.universe_refresh_seconds)
            try:
                next_symbols, next_screen = await asyncio.to_thread(select_symbols, settings)
                if not next_symbols:
                    raise RuntimeError("ETF universe refresh selected no symbols")
                added = sorted(set(next_symbols) - set(symbols))
                removed = sorted(set(symbols) - set(next_symbols))
                if added:
                    stream.subscribe_bars(on_bar, *added)
                if removed:
                    stream.unsubscribe_bars(*removed)
                symbols = next_symbols
                save_universe_snapshot(next_screen)
                log.info("Refreshed ETF %s universe: %s symbols (%s added, %s removed)",
                         settings.etf_holdings_symbol, len(symbols), len(added), len(removed))
            except Exception:
                log.exception("ETF universe refresh failed; retaining current subscriptions")

    asyncio.create_task(refresh_universe())
    log.info("Starting Alpaca %s minute-bar stream for ETF %s holdings: %s",
             feed.value, settings.etf_holdings_symbol, symbols)
    await asyncio.to_thread(stream.run)


if __name__ == "__main__":
    asyncio.run(main())

