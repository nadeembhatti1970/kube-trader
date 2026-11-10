"""Rank intraday strategy variants on out-of-sample portfolio results.

Loads stored bars once, runs the matrix in-process, and promotes a variant only
when its out-of-sample bootstrap interval for the mean net trade return lies
entirely above zero and it meets the minimum trade count. Reads PostgreSQL and,
with --persist, writes validation records; it never publishes to Redis and never
places orders.

Usage:
    python -m app.services.research_grid
    python -m app.services.research_grid --spreads 0.001,0.0015 --rsis 55,65
    python -m app.services.research_grid --fast-slows 5:20,10:40 --persist
"""
import argparse
import logging

from app.database import (init_db, latest_account_snapshot, latest_universe_snapshot,
                          recent_bars, save_strategy_validation)
from app.portfolio_sim import simulate_portfolio
from app.settings import get_settings, strategy_config
from app.universe import sector_for_symbol
from app.validation import validation_config_key, validation_verdict

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("research-grid")
settings = get_settings()


def _floats(value: str) -> list[float]:
    return [float(item.strip()) for item in value.split(",") if item.strip()]


def _ints(value: str) -> list[int]:
    return [int(item.strip()) for item in value.split(",") if item.strip()]


def _fast_slows(value: str) -> list[tuple[int, int]]:
    pairs = []
    for item in value.split(","):
        if not item.strip():
            continue
        fast, slow = item.split(":")
        pairs.append((int(fast), int(slow)))
    return pairs


def build_variants(spreads: list[float], rsis: list[float],
                   fast_slows: list[tuple[int, int]],
                   exit_styles: list[str]) -> list[dict]:
    variants = []
    for spread in spreads:
        for rsi in rsis:
            for fast, slow in fast_slows:
                for style in exit_styles:
                    variants.append({
                        "min_spread_pct": spread,
                        "min_buy_rsi": rsi,
                        "fast_bars": fast,
                        "slow_bars": slow,
                        "exit_style": style,
                    })
    return variants


def variant_label(variant: dict) -> str:
    suffix = "" if variant["exit_style"] == "signal" else f"|{variant['exit_style']}"
    return (f"{variant['fast_bars']}/{variant['slow_bars']} "
            f"s{variant['min_spread_pct']:g} r{variant['min_buy_rsi']:g}{suffix}")


def _candidate_config(config: dict) -> dict:
    candidate = strategy_config(settings)
    candidate.update({
        "signal_fast_bars": config["fast_bars"],
        "signal_slow_bars": config["slow_bars"],
        "signal_min_spread_pct": config["min_spread_pct"],
        "signal_min_buy_rsi": config["min_buy_rsi"],
        "exit_style": config["exit_style"],
        "trailing_stop_pct": config.get("trailing_stop_pct", 0.0),
        "min_hold_minutes": config.get("min_hold_minutes", 0.0),
        "exit_confirmation_spread_pct": config.get("exit_confirmation_spread_pct", 0.0),
        "resample_minutes": config.get("resample_minutes", 1),
        "split_fraction": config.get("split_fraction", 0.7),
    })
    return candidate


def run_grid(bars_by_symbol: dict[str, list[dict]], sectors: dict[str, str],
             base_config: dict, variants: list[dict], *,
             persist: bool = False, min_oos_trades: int = 30,
             confidence: float = 0.95) -> list[dict]:
    cost = 2.0 * float(base_config["cost_bps_per_side"]) / 100.0
    rows = []
    for variant in variants:
        config = {**base_config, **variant}
        result = simulate_portfolio(
            bars_by_symbol, config, sectors=sectors,
            validation={"confidence": confidence})
        out_of_sample = result.get("out_of_sample") or {}
        significance = out_of_sample.get("significance") or {}
        oos_metrics = out_of_sample.get("metrics") or {}
        in_sample = result.get("in_sample") or {}
        mean = significance.get("mean")
        gross = mean + cost if mean is not None else None
        validated = validation_verdict(significance, min_oos_trades)
        row = {
            "variant": variant,
            "label": variant_label(variant),
            "is_trades": (in_sample.get("significance") or {}).get("n", 0),
            "oos_trades": significance.get("n", 0),
            "oos_gross_pct": gross,
            "oos_net_pct": mean,
            "oos_ci_low": significance.get("ci_low"),
            "oos_ci_high": significance.get("ci_high"),
            "oos_pf": oos_metrics.get("profit_factor"),
            "oos_return_pct": oos_metrics.get("total_return_pct"),
            "oos_exposure_pct": oos_metrics.get("average_exposure_pct"),
            "validated": validated,
        }
        if persist:
            candidate = _candidate_config(config)
            identity = validation_config_key(candidate)
            save_strategy_validation(
                identity["key"], candidate.get("strategy", "unknown"), identity["config"],
                int(significance.get("n") or 0), significance.get("mean"),
                significance.get("ci_low"), significance.get("ci_high"), validated,
                {
                    "basis": "out_of_sample",
                    "config": config,
                    "in_sample": in_sample,
                    "out_of_sample": out_of_sample,
                    "metrics": result.get("metrics"),
                    "period": result.get("period"),
                },
            )
            row["config_hash"] = identity["key"]
        rows.append(row)
    rows.sort(key=lambda item: (
        item["oos_gross_pct"] is None, -(item["oos_gross_pct"] or 0.0)))
    return rows


def print_table(rows: list[dict]) -> None:
    header = (f"{'variant':30s} {'ISn':>4s} {'OOSn':>5s} {'OOS gross':>10s} "
              f"{'OOS net':>9s} {'OOS 95% CI':>19s} {'PF':>5s} {'promoted':>8s}")
    print(header)
    print("-" * len(header))
    for row in rows:
        gross = "-" if row["oos_gross_pct"] is None else f"{row['oos_gross_pct']:+.3f}%"
        net = "-" if row["oos_net_pct"] is None else f"{row['oos_net_pct']:+.3f}%"
        if row["oos_ci_low"] is None:
            interval = "-"
        else:
            interval = f"{row['oos_ci_low']:+.3f} .. {row['oos_ci_high']:+.3f}"
        pf = "-" if row["oos_pf"] is None else f"{row['oos_pf']:.2f}"
        print(f"{row['label']:30s} {row['is_trades']:4d} {row['oos_trades']:5d} "
              f"{gross:>10s} {net:>9s} {interval:>19s} {pf:>5s} "
              f"{'yes' if row['validated'] else 'no':>8s}")


def resolve_symbols(explicit: str | None, limit: int) -> list[str]:
    if explicit:
        symbols = [item.strip().upper() for item in explicit.split(",") if item.strip()]
    else:
        symbols = [row["symbol"] for row in latest_universe_snapshot()
                   if row.get("selected")]
    if not symbols:
        raise ValueError("No symbols selected; run ingestion first or pass --symbols")
    return sorted(dict.fromkeys(symbols))[:max(1, limit)]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=12000,
                        help="Stored minute bars per symbol (default 12000)")
    parser.add_argument("--symbols", default=None,
                        help="Comma-separated symbols; defaults to the selected universe")
    parser.add_argument("--max-symbols", type=int,
                        default=settings.portfolio_backtest_max_symbols)
    parser.add_argument("--spreads", default="0.001,0.0015,0.002,0.003")
    parser.add_argument("--rsis", default="55,60,65")
    parser.add_argument("--fast-slows", default="5:20",
                        help="Comma-separated fast:slow pairs, e.g. 5:20,10:40")
    parser.add_argument("--exit-styles", default="signal",
                        help="Comma-separated signal, protective_only, trailing")
    parser.add_argument("--cost-bps", type=float, default=None,
                        help="Per-side cost; defaults to BACKTEST_COST_BPS")
    parser.add_argument("--split-fraction", type=float, default=0.7)
    parser.add_argument("--min-oos-trades", type=int,
                        default=settings.validation_min_trades)
    parser.add_argument("--persist", action="store_true",
                        help="Store each verdict in strategy_validation")
    args = parser.parse_args()
    if not 0.5 <= args.split_fraction <= 0.9:
        raise SystemExit("--split-fraction must be between 0.5 and 0.9")
    try:
        symbols = resolve_symbols(args.symbols, args.max_symbols)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    init_db()
    bars_by_symbol = {}
    for symbol in symbols:
        rows = recent_bars(symbol, args.days)
        if rows:
            bars_by_symbol[symbol] = rows
    if not bars_by_symbol:
        raise SystemExit("No stored minute bars; run app.services.backfill first")
    snapshot = latest_account_snapshot()
    base_config = {
        "starting_equity": float(snapshot["equity"]) if snapshot else 100_000.0,
        "intraday_allocation_pct": settings.intraday_allocation_pct,
        "stop_loss_pct": settings.stop_loss_pct,
        "take_profit_pct": settings.take_profit_pct,
        "cost_bps_per_side": (settings.backtest_cost_bps if args.cost_bps is None
                              else args.cost_bps),
        "max_trade_risk_pct": min(settings.max_trade_risk_pct, 0.01),
        "max_portfolio_open_risk_pct": settings.max_portfolio_open_risk_pct,
        "max_symbol_exposure_pct": settings.max_symbol_exposure_pct,
        "max_sector_exposure_pct": settings.max_sector_exposure_pct,
        "max_open_positions": settings.max_open_positions,
        "intraday_entry_start_minutes": settings.intraday_entry_start_minutes,
        "intraday_entry_cutoff_minutes": settings.intraday_entry_cutoff_minutes,
        "intraday_flatten_minutes": settings.intraday_flatten_minutes,
        "split_fraction": args.split_fraction,
    }
    variants = build_variants(
        _floats(args.spreads), _floats(args.rsis),
        _fast_slows(args.fast_slows),
        [item.strip() for item in args.exit_styles.split(",") if item.strip()],
    )
    sectors = {symbol: sector_for_symbol(symbol, settings) for symbol in bars_by_symbol}
    log.info("Running %s variants over %s symbols and %s bars",
             len(variants), len(bars_by_symbol),
             sum(len(rows) for rows in bars_by_symbol.values()))
    rows = run_grid(
        bars_by_symbol, sectors, base_config, variants,
        persist=args.persist, min_oos_trades=args.min_oos_trades,
        confidence=settings.validation_confidence)
    print_table(rows)
    promoted = [row for row in rows if row["validated"]]
    print(f"\npromoted {len(promoted)} of {len(rows)} variants "
          f"(out-of-sample CI above zero and at least {args.min_oos_trades} trades)")
    if promoted:
        print("strictly promote on out-of-sample evidence only; this is not a guarantee.")


if __name__ == "__main__":
    main()
