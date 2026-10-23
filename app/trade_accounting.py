from collections import defaultdict, deque
from datetime import datetime, timezone


def _sort_timestamp(fill: dict) -> datetime:
    return (fill.get("filled_at") or fill.get("submitted_at")
            or datetime.min.replace(tzinfo=timezone.utc))


def build_realized_trade_report(fills: list[dict]) -> dict:
    """Match managed buy/sell fills FIFO and report gross realized trade slices."""
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
            inventory[key].append({"fill": fill, "remaining_qty": quantity})
            continue
        if fill["side"] != "sell":
            continue

        remaining_exit = quantity
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
                if entry_reference else None
            )
            exit_slippage = (
                (float(exit_reference) - price) / float(exit_reference) * 10_000
                if exit_reference else None
            )
            entry_time = entry_fill.get("filled_at") or entry_fill.get("submitted_at")
            exit_time = fill.get("filled_at") or fill.get("submitted_at")
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
                "entry_reference_price": entry_reference,
                "exit_reference_price": exit_reference,
                "entry_fill_price": entry_price,
                "exit_fill_price": price,
                "entry_adverse_slippage_bps": entry_slippage,
                "exit_adverse_slippage_bps": exit_slippage,
                "gross_realized_pnl": pnl,
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

    summary = {}
    for trade in trades:
        pool = trade["pool"]
        stats = summary.setdefault(pool, {
            "closed_trade_slices": 0,
            "closed_quantity": 0.0,
            "gross_realized_pnl": 0.0,
            "wins": 0,
            "losses": 0,
        })
        stats["closed_trade_slices"] += 1
        stats["closed_quantity"] += trade["quantity"]
        stats["gross_realized_pnl"] += trade["gross_realized_pnl"]
        if trade["gross_realized_pnl"] > 0:
            stats["wins"] += 1
        elif trade["gross_realized_pnl"] < 0:
            stats["losses"] += 1
    for stats in summary.values():
        closed = stats["wins"] + stats["losses"]
        stats["win_rate_pct"] = stats["wins"] / closed * 100 if closed else None

    open_positions = [
        {
            "symbol": key[0],
            "pool": key[1],
            "open_quantity": sum(lot["remaining_qty"] for lot in lots),
        }
        for key, lots in inventory.items() if lots
    ]
    return {
        "basis": "gross realized P&L; excludes fees, taxes, and other account adjustments",
        "matching": "FIFO by symbol and strategy pool using broker-reported aggregate order fills",
        "trades": trades,
        "summary_by_pool": summary,
        "open_managed_quantities": open_positions,
        "unmatched_exit_fills": unmatched_exits,
    }
