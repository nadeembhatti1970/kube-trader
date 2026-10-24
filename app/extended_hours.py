"""Read-only sampling of extended-hours and overnight liquidity. It never places orders."""
import logging
from datetime import datetime, time
from zoneinfo import ZoneInfo

from alpaca.data.enums import DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestQuoteRequest
from alpaca.trading.client import TradingClient

log = logging.getLogger("extended_hours")
NY = ZoneInfo("America/New_York")


def session_for(moment: datetime) -> str:
    """Classify a moment into an Alpaca session. Exchange holidays are not modelled."""
    local = moment.astimezone(NY)
    weekday, clock = local.weekday(), local.time()
    if clock >= time(20):
        # Overnight runs Sunday to Thursday evening; Friday and Saturday evenings are closed.
        return "closed" if weekday in {4, 5} else "overnight"
    if clock < time(4):
        return "overnight" if weekday <= 4 else "closed"
    if weekday >= 5:
        return "closed"
    if clock < time(9, 30):
        return "premarket"
    if clock < time(16):
        return "regular"
    return "afterhours"


def spread_bps(bid: float | None, ask: float | None) -> float | None:
    if not bid or not ask or bid <= 0 or ask <= 0 or ask < bid:
        return None
    return (ask - bid) / ((ask + bid) / 2) * 10_000


def build_sample_rows(symbols: list[str], quotes: dict, assets: dict,
                      sampled_at: datetime, session: str, feed: str) -> list[dict]:
    rows = []
    for symbol in symbols:
        quote = quotes.get(symbol)
        asset = assets.get(symbol) or {}
        attributes = asset.get("attributes") or []
        bid = float(quote.bid_price) if quote is not None and quote.bid_price else None
        ask = float(quote.ask_price) if quote is not None and quote.ask_price else None
        rows.append({
            "sampled_at": sampled_at, "session": session, "symbol": symbol, "feed": feed,
            "overnight_tradable": ("overnight_tradable" in attributes) if asset else None,
            "overnight_halted": ("overnight_halted" in attributes) if asset else None,
            "bid": bid, "ask": ask,
            "bid_size": float(quote.bid_size) if quote is not None and quote.bid_size is not None else None,
            "ask_size": float(quote.ask_size) if quote is not None and quote.ask_size is not None else None,
            "spread_bps": spread_bps(bid, ask),
            "quote_ts": quote.timestamp if quote is not None else None,
        })
    return rows


def fetch_assets(trading: TradingClient, symbols: list[str]) -> dict:
    assets = {}
    for symbol in symbols:
        try:
            assets[symbol] = trading.get_asset(symbol)
        except Exception:
            log.exception("Unable to read asset %s", symbol)
    return assets


def sample_once(trading: TradingClient, data: StockHistoricalDataClient,
                symbols: list[str], configured_feed: str, now: datetime) -> list[dict]:
    session = session_for(now)
    if session in {"closed", "regular"} or not symbols:
        return []
    feed = DataFeed.OVERNIGHT if session == "overnight" else (
        DataFeed.SIP if configured_feed.lower() == "sip" else DataFeed.IEX)
    quotes = data.get_stock_latest_quote(StockLatestQuoteRequest(
        symbol_or_symbols=symbols, feed=feed))
    assets = fetch_assets(trading, symbols)
    return build_sample_rows(symbols, quotes, assets, now, session, feed.value)
