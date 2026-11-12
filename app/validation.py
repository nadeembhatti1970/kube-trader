"""Statistical evidence helpers for strategy validation.

A positive backtest mean is not evidence of an edge. These helpers report the
uncertainty around the mean trade return and the smallest edge the sample could
have detected, so a short or noisy run cannot be presented as validated.
"""
import hashlib
import json
import math
import random
from statistics import mean, pstdev

VALIDATION_KEYS = (
    "strategy", "strategy_pool", "signal_frequency", "signal_fast_bars",
    "signal_slow_bars", "signal_min_spread_pct", "signal_min_buy_rsi",
    "stop_loss_pct", "take_profit_pct", "intraday_flatten_minutes",
    "intraday_entry_cutoff_minutes", "intraday_entry_start_minutes",
    "swing_atr_period", "swing_atr_multiple", "core_stop_loss_pct",
    "entry_order_type", "entry_limit_offset_bps",
    "min_hold_minutes", "exit_confirmation_spread_pct", "exit_style",
    "trailing_stop_pct", "resample_minutes", "split_fraction",
    "premise", "opening_range_minutes", "gap_up_pct", "mandate",
    "backtest_cost_bps",
)
Z_SCORES = {0.90: 1.645, 0.95: 1.960, 0.99: 2.576}
# Bump when the key set or the meaning of a validated verdict changes. The version
# is part of the hash and is stored, so older records cannot satisfy a newer gate.
# 5 adds the cost assumption to the intraday key.
VALIDATION_SCHEME_VERSION = 5
MANDATES = ("relative", "absolute")
# Live provenance records only execution settings, so simulator-only keys are
# normalised here. Without this the live gate could never match a research verdict.
SIMULATOR_DEFAULTS = {
    "min_hold_minutes": 0.0,
    "exit_confirmation_spread_pct": 0.0,
    "exit_style": "signal",
    "trailing_stop_pct": 0.0,
    "resample_minutes": 1,
    "split_fraction": 0.7,
    "premise": "sma_crossover",
    "opening_range_minutes": 15,
    "gap_up_pct": 0.005,
    "mandate": "relative",
}


def config_key(namespace: str, params: dict,
               scheme_version: int | None = None) -> dict:
    """Canonical, versioned key for a research verdict within one namespace."""
    version = (VALIDATION_SCHEME_VERSION if scheme_version is None
               else scheme_version)
    payload = {
        "scheme_version": version,
        "namespace": namespace,
        "params": params,
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode()
    ).hexdigest()
    return {
        "key": digest,
        "config": params,
        "namespace": namespace,
        "scheme_version": version,
    }


def validation_config_key(config: dict, scheme_version: int | None = None) -> dict:
    """Canonical key for the intraday strategy parameters that define an edge."""
    effective = {**SIMULATOR_DEFAULTS, **(config or {})}
    selected = {key: effective.get(key) for key in VALIDATION_KEYS}
    return config_key("intraday", selected, scheme_version)


def _percentile(ordered: list[float], fraction: float) -> float:
    if not ordered:
        raise ValueError("percentile requires at least one value")
    index = min(len(ordered) - 1, max(0, int(round(fraction * (len(ordered) - 1)))))
    return ordered[index]


def block_bootstrap_ci(values: list[float], block_size: int = 5, samples: int = 2000,
                       confidence: float = 0.95, seed: int = 7) -> dict | None:
    """Percentile CI for the mean using a moving-block bootstrap.

    Consecutive trades are not independent, so whole blocks are resampled rather
    than individual trades. The seed keeps reports reproducible.
    """
    if len(values) < 2:
        return None
    if samples < 1:
        raise ValueError("samples must be at least 1")
    if confidence not in Z_SCORES:
        raise ValueError(f"confidence must be one of {sorted(Z_SCORES)}")
    # A block as long as the sample can only ever resample the whole series, which
    # would report a zero-width interval for tiny samples. Leave at least one
    # element of variation between blocks.
    block = max(1, min(block_size, len(values) - 1))
    rng = random.Random(seed)
    count = len(values)
    means = []
    for _ in range(samples):
        sample: list[float] = []
        while len(sample) < count:
            start = rng.randrange(0, count - block + 1)
            sample.extend(values[start:start + block])
        means.append(mean(sample[:count]))
    means.sort()
    tail = (1.0 - confidence) / 2.0
    return {
        "confidence": confidence,
        "block_size": block,
        "samples": samples,
        "seed": seed,
        "ci_low": _percentile(means, tail),
        "ci_high": _percentile(means, 1.0 - tail),
    }


def minimum_detectable_edge(values: list[float], confidence: float = 0.95) -> float | None:
    """Smallest mean edge this sample size and variance could distinguish from zero."""
    if len(values) < 2:
        return None
    z = Z_SCORES.get(confidence)
    if z is None:
        raise ValueError(f"confidence must be one of {sorted(Z_SCORES)}")
    deviation = pstdev(values)
    return z * deviation / math.sqrt(len(values))


def significance_summary(values: list[float], confidence: float = 0.95,
                         block_size: int = 5, samples: int = 2000,
                         seed: int = 7) -> dict:
    if not values:
        return {"n": 0, "mean": None, "std": None, "ci_low": None, "ci_high": None,
                "ci_above_zero": False, "ci_below_zero": False,
                "ci_excludes_zero": False, "minimum_detectable_edge": None,
                "confidence": confidence, "block_size": block_size,
                "samples": samples, "seed": seed}
    bootstrap = block_bootstrap_ci(values, block_size, samples, confidence, seed)
    ci_low = bootstrap["ci_low"] if bootstrap else None
    ci_high = bootstrap["ci_high"] if bootstrap else None
    return {
        "n": len(values),
        "mean": mean(values),
        "std": pstdev(values) if len(values) > 1 else 0.0,
        "ci_low": ci_low,
        "ci_high": ci_high,
        "ci_above_zero": bool(bootstrap and ci_low > 0),
        "ci_below_zero": bool(bootstrap and ci_high < 0),
        "ci_excludes_zero": bool(bootstrap and (ci_low > 0 or ci_high < 0)),
        "minimum_detectable_edge": minimum_detectable_edge(values, confidence),
        "confidence": confidence,
        "block_size": block_size,
        "samples": samples,
        "seed": seed,
    }


def validation_verdict(significance: dict, min_trades: int,
                       benchmark_relative: dict | None = None,
                       mandate: str = "relative") -> bool:
    """Decide whether a configuration is validated under the chosen mandate.

    relative: positive trade edge and a positive edge over the passive
    alternative at the same exposure.
    absolute: positive trade edge only.
    """
    if mandate not in MANDATES:
        raise ValueError(f"mandate must be one of {MANDATES}")
    trade_edge = bool(
        significance
        and significance.get("n", 0) >= max(1, min_trades)
        and significance.get("ci_above_zero")
    )
    if mandate == "absolute":
        return trade_edge
    if benchmark_relative is None:
        return trade_edge
    return trade_edge and bool(benchmark_relative.get("ci_above_zero"))
