from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_version: str = "dev"
    market_data_provider: str = "yfinance"
    finnhub_api_key: str = ""
    alpaca_api_key: str = ""
    alpaca_secret_key: str = ""
    alpaca_paper: bool = True
    database_path: str = "/data/market.db"
    postgres_dsn: str = "postgresql://kube_trader:kube_trader@postgres:5432/kube_trader"
    redis_url: str = "redis://redis:6379/0"
    etf_holdings_symbol: str = "SPY"
    etf_holdings_url: str = "https://www.ssga.com/library-content/products/fund-data/etfs/us/holdings-daily-us-en-spy.xlsx"
    etf_max_holdings: int = 100
    universe_refresh_seconds: int = 86400
    universe_screen_enabled: bool = True
    min_symbol_price: float = 5.0
    min_avg_dollar_volume: float = 20_000_000.0
    max_watchlist_symbols: int = 40
    symbol_sectors: str = ""
    max_open_positions: int = 20
    max_symbol_exposure_pct: float = 0.05
    max_sector_exposure_pct: float = 0.25
    intraday_flatten_minutes: int = 0
    intraday_entry_cutoff_minutes: int = 0
    symbol_loss_cooldown_seconds: int = 0
    symbol_daily_loss_limit: float = 0.0
    execution_enabled: bool = False
    stop_management_enabled: bool = False
    market_stream_enabled: bool = False
    extended_hours_sampling_enabled: bool = False
    extended_hours_sample_seconds: int = 300
    max_trade_risk_pct: float = 0.01
    daily_drawdown_limit_pct: float = 0.03
    stop_loss_pct: float = 0.02
    take_profit_pct: float = 0.04
    intraday_allocation_pct: float = 0.20
    swing_allocation_pct: float = 0.30
    core_allocation_pct: float = 0.50
    signal_fast_bars: int = 5
    signal_slow_bars: int = 20
    signal_min_spread_pct: float = 0.001
    signal_min_buy_rsi: float = 55.0
    swing_atr_period: int = 14
    swing_atr_multiple: float = 2.0
    core_stop_loss_pct: float = 0.20
    backtest_cost_bps: float = 5.0
    backtest_train_fraction: float = 0.70
    intraday_backtest_bars: int = 50_000
    alpaca_data_feed: str = "iex"
    alpaca_activity_sync_seconds: int = 300
    alpaca_activity_lookback_days: int = 30
    reconcile_interval_seconds: int = 30
    max_signal_age_seconds: int = 180
    alert_webhook_url: str = ""
    log_level: str = "INFO"
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


@lru_cache
def get_settings() -> Settings:
    return Settings()


def strategy_config(settings: Settings | None = None, event: dict | None = None) -> dict:
    current = settings or get_settings()
    result = {
        "app_version": current.app_version,
        "trading_mode": "paper" if current.alpaca_paper else "live",
        "strategy": "sma-crossover-confirmed-v1",
        "signal_fast_bars": current.signal_fast_bars,
        "signal_slow_bars": current.signal_slow_bars,
        "signal_min_spread_pct": current.signal_min_spread_pct,
        "signal_min_buy_rsi": current.signal_min_buy_rsi,
        "stop_loss_pct": current.stop_loss_pct,
        "take_profit_pct": current.take_profit_pct,
        "swing_atr_period": current.swing_atr_period,
        "swing_atr_multiple": current.swing_atr_multiple,
        "core_stop_loss_pct": current.core_stop_loss_pct,
        "intraday_flatten_minutes": current.intraday_flatten_minutes,
        "intraday_entry_cutoff_minutes": current.intraday_entry_cutoff_minutes,
    }
    if event:
        signal_values = {
            key: event[key]
            for key in ("fast_sma", "slow_sma", "rsi_14", "daily_trend",
                        "strategy_bar_date", "stop_loss_pct", "take_profit_pct")
            if event.get(key) is not None
        }
        if signal_values:
            result["signal_values"] = signal_values
    return result
