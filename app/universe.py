import logging

from app.market_data import get_daily_bars
from app.settings import Settings

log = logging.getLogger("universe")

DEFAULT_SECTORS = {
    "AAPL": "technology", "MSFT": "technology", "NVDA": "technology",
    "AVGO": "technology", "ORCL": "technology", "CRM": "technology",
    "AMD": "technology", "AMZN": "consumer_discretionary", "GOOGL": "communication_services",
    "META": "communication_services", "NFLX": "communication_services",
    "TMUS": "communication_services", "JPM": "financials", "V": "financials",
    "XOM": "energy", "CVX": "energy",
    "LLY": "healthcare", "JNJ": "healthcare", "UNH": "healthcare",
    "WMT": "consumer_staples", "COST": "consumer_staples", "PG": "consumer_staples",
    "KO": "consumer_staples", "HD": "consumer_discretionary", "TSLA": "consumer_discretionary",
    "CAT": "industrials", "GE": "industrials", "NEE": "utilities",
    "PLD": "real_estate", "SPY": "broad_market", "QQQ": "broad_market",
    "IWM": "broad_market", "DIA": "broad_market", "GLD": "commodities",
    "TLT": "fixed_income",
}


def candidate_symbols(settings: Settings) -> list[str]:
    return list(dict.fromkeys(
        symbol.strip().upper() for symbol in settings.default_symbols.split(",") if symbol.strip()
    ))


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
    """Screen configured candidates by price and 20-day average dollar volume."""
    if (settings.max_watchlist_symbols < 1 or settings.min_symbol_price < 0
            or settings.min_avg_dollar_volume < 0):
        raise ValueError("Universe limits must be positive; price and dollar-volume floors may be zero")
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
