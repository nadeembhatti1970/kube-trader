import json
import logging
import uuid

from app.database import init_db, recent_bars, save_backtest
from app.research import STRATEGIES, simulate_ohlc, summarize
from app.settings import get_settings, strategy_config
from app.streams import BAR_STREAM, client, ensure_group, read_group

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("backtesting")
settings = get_settings()


def evaluate(bars: list[dict], fast_period: int, slow_period: int,
             min_spread_pct: float = 0.0, min_buy_rsi: float = 0.0,
             cost_bps: float = 5.0, stop_loss_pct: float = 0.02,
             take_profit_pct: float = 0.04, intraday_flatten_minutes: int = 0,
             intraday_entry_cutoff_minutes: int = 0) -> dict | None:
    """Run the same OHLC execution model used by the research endpoint."""
    if len(bars) < slow_period + 2:
        return None
    simulation = simulate_ohlc(
        bars, "intraday", cost_bps, min_spread_pct, min_buy_rsi,
        stop_loss_pct, take_profit_pct, intraday_flatten_minutes,
        intraday_entry_cutoff_minutes, fast_period=fast_period,
        slow_period=slow_period,
    )
    periods = STRATEGIES["intraday"]["periods_per_year"]
    metrics = summarize(
        simulation["bar_returns"], simulation["benchmark_returns"],
        len(simulation["trades"]), periods,
    )
    return {
        "bars": len(bars),
        "total_return": metrics["total_return"],
        "max_drawdown": metrics["max_drawdown"],
        "sharpe": metrics["sharpe_annualized"],
        "closed_trades": len(simulation["trades"]),
        "open_trade_at_end": simulation["open_trade"],
    }


def main():
    init_db()
    redis_client = client()
    ensure_group(redis_client, BAR_STREAM, "backtest-workers")
    consumer = f"backtest-{uuid.uuid4()}"
    while True:
        batches = read_group(redis_client, "backtest-workers", consumer, BAR_STREAM,
                             count=50, block=5000)
        for _, messages in batches:
            for message_id, fields in messages:
                try:
                    event = json.loads(fields["data"])
                    history = recent_bars(event["symbol"], 500)
                    metrics = evaluate(history,
                                       settings.signal_fast_bars, settings.signal_slow_bars,
                                       settings.signal_min_spread_pct,
                                       settings.signal_min_buy_rsi,
                                       settings.backtest_cost_bps,
                                       settings.stop_loss_pct,
                                       settings.take_profit_pct,
                                       settings.intraday_flatten_minutes,
                                       settings.intraday_entry_cutoff_minutes)
                    if metrics:
                        model_config = strategy_config(settings)
                        model_config.update({
                            "backtest_model": "ohlc-next-open-v1",
                            "backtest_cost_bps_per_side": settings.backtest_cost_bps,
                        })
                        save_backtest(event["symbol"], metrics, model_config)
                    redis_client.xack(BAR_STREAM, "backtest-workers", message_id)
                except Exception:
                    log.exception("Unable to backtest event %s", message_id)


if __name__ == "__main__":
    main()
