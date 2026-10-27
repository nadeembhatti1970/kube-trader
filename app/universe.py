import logging
from io import BytesIO

import httpx
import pandas as pd

from app.market_data import get_daily_bars
from app.settings import Settings

log = logging.getLogger("universe")

DEFAULT_SECTORS = {
    "AAPL": "technology", "MSFT": "technology", "NVDA": "technology",
    "AVGO": "technology", "ORCL": "technology", "CRM": "technology",
    "AMD": "technology", "INTC": "technology", "PLTR": "technology",
    "WDC": "technology", "MA": "financials", "AMZN": "consumer_discretionary", "GOOGL": "communication_services",
    "META": "communication_services", "NFLX": "communication_services",
    "TMUS": "communication_services", "JPM": "financials", "V": "financials",
    "XOM": "energy", "CVX": "energy",
    "LLY": "healthcare", "JNJ": "healthcare", "UNH": "healthcare", "MDT": "healthcare",
    "WMT": "consumer_staples", "COST": "consumer_staples", "PG": "consumer_staples",
    "KO": "consumer_staples", "HD": "consumer_discretionary", "TSLA": "consumer_discretionary",
    "CAT": "industrials", "GE": "industrials", "NEE": "utilities",
    "PLD": "real_estate", "SPY": "broad_market", "QQQ": "broad_market",
    "IWM": "broad_market", "DIA": "broad_market", "GLD": "commodities",
    "TLT": "fixed_income",
}


def candidate_symbols(settings: Settings) -> list[str]:
    """Load the current constituents from the configured ETF issuer workbook."""
    if not settings.etf_holdings_symbol.strip() or not settings.etf_holdings_url.startswith("https://"):
        raise ValueError("ETF holdings symbol and HTTPS workbook URL must be configured")
    response = httpx.get(
        settings.etf_holdings_url, timeout=30, follow_redirects=True,
        headers={"User-Agent": "kube-trader-universe/1.0"},
    )
    response.raise_for_status()
    sheet = pd.read_excel(BytesIO(response.content), header=None, engine="openpyxl")

    # Issuer workbooks include a variable number of metadata rows before the
    # holdings table. Locate its ticker column by its header instead of row number.
    ticker_column = weight_column = header_row = None
    for row_index, row in sheet.head(20).iterrows():
        for column_index, value in row.items():
            if str(value).strip().lower() in {"ticker", "symbol"}:
                header_row, ticker_column = row_index, column_index
            if str(value).strip().lower() in {"weight", "weight (%)", "% of net assets"}:
                weight_column = column_index
        if ticker_column is not None:
            break
    if ticker_column is None or weight_column is None:
        raise ValueError(f"Ticker or weight column not found in {settings.etf_holdings_symbol} holdings workbook")

    holdings = []
    weighted_rows = 0
    for row in sheet.iloc[header_row + 1:].itertuples(index=False, name=None):
        value = row[ticker_column]
        symbol = str(value).strip().upper()
        if symbol.endswith(".0"):
            symbol = symbol[:-2]
        if symbol in {"CASH", "USD", "N/A", "-"}:
            continue
        if symbol and all(char.isalnum() or char in ".-" for char in symbol):
            weight = 0.0
            if weight_column is not None:
                try:
                    weight = float(str(row[weight_column]).strip().replace("%", ""))
                    weighted_rows += 1
                except (TypeError, ValueError):
                    pass
            holdings.append((symbol, weight))
    # Keep the largest constituents so refresh screening remains within the
    # free providers' practical request limits while retaining a broad universe.
    holdings.sort(key=lambda item: (-item[1], item[0]))
    symbols = list(dict.fromkeys(symbol for symbol, _ in holdings))[:settings.etf_max_holdings]
    if len(symbols) < 20 or weighted_rows < 20:
        raise ValueError(
            f"Only {len(symbols)} holdings and {weighted_rows} weights found for "
            f"{settings.etf_holdings_symbol}; refusing to replace universe"
        )
    return symbols


def sector_for_symbol(symbol: str, settings: Settings) -> str:
    sectors = dict(DEFAULT_SECTORS)
    for item in settings.symbol_sectors.split(","):
        if ":" not in item:
            continue
        key, value = item.split(":", 1)
        if key.strip() and value.strip():
            sectors[key.strip().upper()] = value.strip().lower().replace(" ", "_")
    return sectors.get(symbol.upper(), "unclassified")


def select_symbols(settings: Settings) -> tuple[list[str], list[dict]]:
    """Screen current ETF constituents by price and 20-day average dollar volume."""
    if (settings.etf_max_holdings < 20 or settings.max_watchlist_symbols < 1 or settings.min_symbol_price < 0
            or settings.min_avg_dollar_volume < 0):
        raise ValueError("ETF holdings limit must be at least 20; watchlist limit must be positive; price and dollar-volume floors may be zero")
    candidates = candidate_symbols(settings)
    if not settings.universe_screen_enabled:
        return candidates[:settings.max_watchlist_symbols], [
            {"symbol": symbol, "price": None, "avg_dollar_volume": None,
             "selected": index < settings.max_watchlist_symbols,
             "reason": "screen_disabled" if index < settings.max_watchlist_symbols
             else "watchlist_limit"}
            for index, symbol in enumerate(candidates)
        ]

    records = []
    eligible = []
    for symbol in candidates:
        try:
            bars = get_daily_bars(symbol, 30, settings)
            if len(bars) < 20:
                records.append({"symbol": symbol, "price": None, "avg_dollar_volume": None,
                                "selected": False, "reason": "insufficient_history"})
                continue
            recent = bars[-20:]
            price = float(recent[-1]["close"])
            avg_dollar_volume = sum(float(bar["close"]) * float(bar["volume"])
                                    for bar in recent) / len(recent)
            if price < settings.min_symbol_price:
                reason = "below_min_price"
            elif avg_dollar_volume < settings.min_avg_dollar_volume:
                reason = "below_min_dollar_volume"
            else:
                reason = "eligible"
                eligible.append((symbol, price, avg_dollar_volume))
            records.append({"symbol": symbol, "price": price,
                            "avg_dollar_volume": avg_dollar_volume,
                            "selected": False, "reason": reason})
        except Exception:
            log.exception("Universe screen failed for %s", symbol)
            records.append({"symbol": symbol, "price": None, "avg_dollar_volume": None,
                            "selected": False, "reason": "data_unavailable"})

    eligible.sort(key=lambda item: (-item[2], item[0]))
    selected = {symbol for symbol, _, _ in eligible[:settings.max_watchlist_symbols]}
    for record in records:
        if record["symbol"] in selected:
            record["selected"] = True
            record["reason"] = "selected"
        elif record["reason"] == "eligible":
            record["reason"] = "watchlist_limit"
    symbols = [symbol for symbol, _, _ in eligible if symbol in selected]
    if not symbols:
        raise RuntimeError("Universe screen selected no symbols; review candidates and liquidity settings")
    return symbols, records
