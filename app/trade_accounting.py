from collections import defaultdict, deque
from datetime import datetime, timezone


def _sort_timestamp(fill: dict) -> datetime:
    return (fill.get("filled_at") or fill.get("submitted_at")
            or datetime.min.replace(tzinfo=timezone.utc))


def _summarize(trades: list[dict]) -> dict:
    summary = {
        "closed_trade_slices": len(trades),
        "closed_quantity": sum(trade["quantity"] for trade in trades),
        "gross_realized_pnl": sum(trade["gross_realized_pnl"] for trade in trades),
        "known_fees": sum(
            (trade["entry_fees"] or 0) + (trade["exit_fees"] or 0)
            for trade in trades
        ),
        "fees_complete": bool(trades) and all(trade["fees"] is not None for trade in trades),
        "wins": sum(trade["gross_realized_pnl"] > 0 for trade in trades),
        "losses": sum(trade["gross_realized_pnl"] < 0 for trade in trades),
    }
    summary["net_realized_pnl"] = (
        summary["gross_realized_pnl"] - summary["known_fees"]
        if summary["fees_complete"] else None
    )
    closed = summary["wins"] + summary["losses"]
    summary["win_rate_pct"] = summary["wins"] / closed * 100 if closed else None
    summary["average_holding_seconds"] = (
        sum(trade["holding_seconds"] for trade in trades
            if trade["holding_seconds"] is not None)
        / sum(trade["holding_seconds"] is not None for trade in trades)
        if any(trade["holding_seconds"] is not None for trade in trades) else None
    )
    for side in ("entry", "exit"):
        available = [trade for trade in trades
                     if trade[f"{side}_adverse_slippage_bps"] is not None]
        quantity = sum(trade["quantity"] for trade in available)
        summary[f"quantity_weighted_{side}_adverse_slippage_bps"] = (
            sum(trade["quantity"] * trade[f"{side}_adverse_slippage_bps"]
                for trade in available) / quantity if quantity else None
        )
    return summary


def _grouped_summary(trades: list[dict], key_for_trade) -> dict:
    groups = defaultdict(list)
    for trade in trades:
        groups[key_for_trade(trade)].append(trade)
    return {key: _summarize(group) for key, group in sorted(groups.items())}


def build_realized_trade_report(fills: list[dict]) -> dict:
    """Match managed buy/sell fills FIFO and report realized execution metrics."""
    inventory = defaultdict(deque)
    trades = []
    unmatched_exits = []

    ordered_fills = sorted(
        fills,
        key=lambda fill: (_sort_timestamp(fill), fill["client_order_id"]),
    )
    for fill in ordered_fills:
        key = (fill["symbol"], fill["pool"])
        quantity = float(fill["filled_qty"])
        price = float(fill["filled_avg_price"])
        if quantity <= 0 or price <= 0:
            continue
        if fill["side"] == "buy":
            fee = fill.get("fees")
            inventory[key].append({
                "fill": fill,
                "remaining_qty": quantity,
                "fee_per_share": float(fee) / quantity if fee is not None else None,
            })
            continue
        if fill["side"] != "sell":
            continue

        remaining_exit = quantity
        exit_fee = fill.get("fees")
        exit_fee_per_share = float(exit_fee) / quantity if exit_fee is not None else None
        while remaining_exit > 0 and inventory[key]:
            entry = inventory[key][0]
            matched_qty = min(remaining_exit, entry["remaining_qty"])
            entry_fill = entry["fill"]
            entry_price = float(entry_fill["filled_avg_price"])
            pnl = matched_qty * (price - entry_price)
            entry_reference = entry_fill.get("reference_price")
            exit_reference = fill.get("reference_price")
            entry_slippage = (
                (entry_price - float(entry_reference)) / float(entry_reference) * 10_000
                if entry_reference is not None and float(entry_reference) > 0 else None
            )
            exit_slippage = (
                (float(exit_reference) - price) / float(exit_reference) * 10_000
                if exit_reference is not None and float(exit_reference) > 0 else None
            )
            entry_time = entry_fill.get("filled_at") or entry_fill.get("submitted_at")
            exit_time = fill.get("filled_at") or fill.get("submitted_at")
            entry_fees = (
                matched_qty * entry["fee_per_share"]
                if entry["fee_per_share"] is not None else None
            )
            exit_fees = (
                matched_qty * exit_fee_per_share
                if exit_fee_per_share is not None else None
            )
            fees = entry_fees + exit_fees if entry_fees is not None and exit_fees is not None else None
            trades.append({
                "symbol": fill["symbol"],
                "pool": fill["pool"],
                "quantity": matched_qty,
                "entry_order_id": entry_fill["broker_order_id"],
                "exit_order_id": fill["broker_order_id"],
                "entry_signal_id": entry_fill["signal_id"],
                "exit_signal_id": fill["signal_id"],
                "entry_signal_timestamp": entry_fill.get("signal_timestamp"),
                "exit_signal_timestamp": fill.get("signal_timestamp"),
                "entry_reference_timestamp": entry_fill.get("reference_timestamp"),
                "exit_reference_timestamp": fill.get("reference_timestamp"),
                "entry_signal_to_fill_seconds": (
                    (entry_fill["filled_at"] - entry_fill["signal_timestamp"]).total_seconds()
                    if entry_fill.get("filled_at") and entry_fill.get("signal_timestamp") else None
                ),
                "exit_signal_to_fill_seconds": (
                    (fill["filled_at"] - fill["signal_timestamp"]).total_seconds()
                    if fill.get("filled_at") and fill.get("signal_timestamp") else None
                ),
                "entry_submitted_at": entry_fill.get("submitted_at"),
                "exit_submitted_at": fill.get("submitted_at"),
                "entry_filled_at": entry_fill.get("filled_at"),
                "exit_filled_at": fill.get("filled_at"),
                "holding_seconds": (
                    (exit_time - entry_time).total_seconds()
                    if entry_time and exit_time else None
                ),
                "entry_order_type": entry_fill.get("order_type"),
                "exit_order_type": fill.get("order_type"),
                "entry_reference_price": entry_reference,
                "exit_reference_price": exit_reference,
                "entry_fill_price": entry_price,
                "exit_fill_price": price,
                "entry_adverse_slippage_bps": entry_slippage,
                "exit_adverse_slippage_bps": exit_slippage,
                "entry_fees": entry_fees,
                "exit_fees": exit_fees,
                "fees": fees,
                "gross_realized_pnl": pnl,
                "net_realized_pnl": pnl - fees if fees is not None else None,
                "gross_return_pct": (price / entry_price - 1) * 100,
            })
            entry["remaining_qty"] -= matched_qty
            remaining_exit -= matched_qty
            if entry["remaining_qty"] <= 1e-9:
                inventory[key].popleft()
        if remaining_exit > 1e-9:
            unmatched_exits.append({
                "symbol": fill["symbol"],
                "pool": fill["pool"],
                "order_id": fill["broker_order_id"],
                "signal_id": fill["signal_id"],
                "unmatched_quantity": remaining_exit,
                "filled_at": fill.get("filled_at"),
            })

    summary_by_pool = _grouped_summary(trades, lambda trade: trade["pool"])
    summary_by_symbol = _grouped_summary(trades, lambda trade: trade["symbol"])
    summary_by_entry_order_type = _grouped_summary(
        trades, lambda trade: trade["entry_order_type"] or "unknown",
    )
    summary_by_exit_order_type = _grouped_summary(
        trades, lambda trade: trade["exit_order_type"] or "unknown",
    )
    summary_by_strategy_symbol_order_type = _grouped_summary(
        trades,
        lambda trade: (
            f'{trade["pool"]}|{trade["symbol"]}|'
            f'{trade["entry_order_type"] or "unknown"}|'
            f'{trade["exit_order_type"] or "unknown"}'
        ),
    )

    open_positions = [
        {
            "symbol": key[0],
            "pool": key[1],
            "open_quantity": sum(lot["remaining_qty"] for lot in lots),
        }
        for key, lots in inventory.items() if lots
    ]
    return {
        "basis": (
            "Realized P&L uses broker fill prices, so execution slippage is already reflected. "
            "Net P&L subtracts recorded fees only and is null when either side's fees are unknown."
        ),
        "matching": "FIFO by symbol and strategy pool using broker-reported aggregate order fills",
        "trades": trades,
        "summary_by_pool": summary_by_pool,
        "summary_by_strategy": summary_by_pool,
        "summary_by_symbol": summary_by_symbol,
        "summary_by_entry_order_type": summary_by_entry_order_type,
        "summary_by_exit_order_type": summary_by_exit_order_type,
        "summary_by_strategy_symbol_order_type": summary_by_strategy_symbol_order_type,
        "fees_complete": bool(trades) and all(trade["fees"] is not None for trade in trades),
        "open_managed_quantities": open_positions,
        "unmatched_exit_fills": unmatched_exits,
    }
