from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    market_data_provider: str = "yfinance"
    finnhub_api_key: str = ""
    alpaca_api_key: str = ""
    alpaca_secret_key: str = ""
    alpaca_paper: bool = True
    database_path: str = "/data/market.db"
    postgres_dsn: str = "postgresql://kube_trader:kube_trader@postgres:5432/kube_trader"
    redis_url: str = "redis://redis:6379/0"
    default_symbols: str = "AAPL,MSFT,SPY"
    execution_enabled: bool = False
    stop_management_enabled: bool = False
    market_stream_enabled: bool = False
    max_trade_risk_pct: float = 0.01
    daily_drawdown_limit_pct: float = 0.03
    stop_loss_pct: float = 0.02
    take_profit_pct: float = 0.04
    intraday_allocation_pct: float = 0.20
    swing_allocation_pct: float = 0.30
    core_allocation_pct: float = 0.50
    signal_fast_bars: int = 5
    signal_slow_bars: int = 20
    swing_atr_period: int = 14
    swing_atr_multiple: float = 2.0
    core_stop_loss_pct: float = 0.20
    alpaca_data_feed: str = "iex"
    reconcile_interval_seconds: int = 30
    max_signal_age_seconds: int = 180
    alert_webhook_url: str = ""
    log_level: str = "INFO"
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


@lru_cache
def get_settings() -> Settings:
    return Settings()

