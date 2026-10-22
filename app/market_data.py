from datetime import date, datetime, timedelta, timezone

import finnhub
import yfinance as yf

from app.settings import Settings


def get_daily_bars(symbol: str, days: int, settings: Settings) -> list[dict]:
    """Fetch daily OHLCV bars; provider availability and terms vary."""
    end = date.today() + timedelta(days=1)
    start = end - timedelta(days=days * 2)
    if settings.market_data_provider.lower() == "finnhub":
        if not settings.finnhub_api_key:
            raise ValueError("FINNHUB_API_KEY is required for MARKET_DATA_PROVIDER=finnhub")
        client = finnhub.Client(api_key=settings.finnhub_api_key)
        to_ts = int(datetime.combine(end, datetime.min.time(), timezone.utc).timestamp())
        from_ts = int(datetime.combine(start, datetime.min.time(), timezone.utc).timestamp())
        result = client.stock_candles(symbol, "D", from_ts, to_ts)
        if result.get("s") != "ok":
            raise ValueError(f"Finnhub returned status {result.get('s', 'unknown')} for {symbol}")
        rows = [
            {"time": datetime.fromtimestamp(ts, timezone.utc).date().isoformat(), "open": o,
             "high": h, "low": low, "close": c, "volume": v}
            for ts, o, h, low, c, v in zip(
                result["t"], result["o"], result["h"], result["l"], result["c"], result["v"]
            )
        ]
    elif settings.market_data_provider.lower() == "yfinance":
        provider_symbol = symbol.replace(".", "-")
        frame = yf.download(provider_symbol, start=start.isoformat(), end=end.isoformat(),
                            progress=False, auto_adjust=True, threads=False)
        if frame.empty:
            raise ValueError(f"No market data returned for {symbol}")
        if getattr(frame.columns, "nlevels", 1) > 1:
            frame.columns = frame.columns.get_level_values(0)
        rows = [
            {"time": index.date().isoformat(), "open": float(row["Open"]),
             "high": float(row["High"]), "low": float(row["Low"]),
             "close": float(row["Close"]), "volume": int(row["Volume"])}
            for index, row in frame.tail(days).iterrows()
        ]
    else:
        raise ValueError("MARKET_DATA_PROVIDER must be 'yfinance' or 'finnhub'")
    return rows[-days:]
