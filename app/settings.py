from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    market_data_provider: str = "yfinance"
    finnhub_api_key: str = ""
    alpaca_api_key: str = ""
    alpaca_secret_key: str = ""
    alpaca_paper: bool = True
    trading_enabled: bool = False
    database_path: str = "/data/market.db"
    default_symbols: str = "AAPL,MSFT,SPY"
    log_level: str = "INFO"
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


@lru_cache
def get_settings() -> Settings:
    return Settings()

