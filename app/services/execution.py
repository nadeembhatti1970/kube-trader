import hashlib
import json
import logging
import math
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
from alpaca.common.exceptions import APIError
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderClass, OrderSide, OrderType, QueryOrderStatus, TimeInForce
from alpaca.trading.requests import (GetOrderByIdRequest, GetOrdersRequest, LimitOrderRequest,
                                     MarketOrderRequest, StopLossRequest,
                                     GetPortfolioHistoryRequest, StopOrderRequest,
                                     TakeProfitRequest)

from app.alpaca_reporting import (fetch_account_activities, serialize_account,
                                 serialize_order, serialize_position)
from app.database import (bot_origin_symbols, broker_activity_sync_status,
                          entry_count_since, fill_order_ids_missing_type,
                          get_risk_state, init_db, latest_strategy_validation,
                          managed_fills, managed_order_id, managed_order_stop_loss_pct,
                          managed_order_roots, managed_orders, managed_pool_assignments,
                          managed_symbols, orders_missing_fill_data, reserve_order,
                          save_account_snapshot, save_broker_order_type,
                          save_broker_activities, save_risk_state,
                          symbol_session_bar_count, sync_managed_orders,
                          update_broker_activity_sync, update_latest_account_orders,
                          update_order)
from app.settings import get_settings, strategy_config
from app.streams import SIGNAL_STREAM, client, ensure_group, read_group
from app.trade_accounting import (build_realized_trade_report, reconcile_open_quantities,
                                 minutes_to_close, pool_session_realized,
                                 symbol_entry_block)
from app.universe import sector_for_symbol
from app.validation import validation_config_key

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("execution")
settings = get_settings()
NY = ZoneInfo("America/New_York")
POOL_WEIGHTS = {"intraday": 0.20, "swing": 0.30, "core": 0.50}
RISK_VIEW_LOCK = threading.Lock()
RISK_VIEW = {"value": None, "captured_at": 0.0}
ENTRY_LOCK = threading.Lock()
ACCOUNT_ACTIVITY_TYPES = ("FILL", "TRANS", "MISC", "FEE")


def pool_weights():
    return {"intraday": settings.intraday_allocation_pct,
            "swing": settings.swing_allocation_pct, "core": settings.core_allocation_pct}


def default_stop_loss_pct(pool: str) -> float:
    """Stop distance used only when a managed entry recorded no stop of its own."""
    if pool == "core":
        return settings.core_stop_loss_pct
    return settings.stop_loss_pct


def minutes_since_open(now: datetime | None = None) -> float | None:
    """Minutes since the regular NY open, or None at a weekend."""
    local = now or datetime.now(NY)
    if local.weekday() >= 5:
        return None
    open_at = local.replace(hour=9, minute=30, second=0, microsecond=0)
    return (local - open_at).total_seconds() / 60


def cap_entry_quantity(qty: int, price: float, stop_loss_pct: float, equity: float,
                       open_risk: float, max_open_risk_pct: float) -> int:
    """Keep the sum of stop-risk across open positions inside the portfolio cap."""
    if max_open_risk_pct <= 0 or equity <= 0:
        return qty
    per_share_risk = max(price * stop_loss_pct, 0.01)
    room = max(equity * max_open_risk_pct - max(open_risk, 0.0), 0.0)
    return max(0, min(qty, math.floor(room / per_share_risk)))


def order_tree(order):
    yield order
    for leg in getattr(order, "legs", None) or []:
        yield from order_tree(leg)


def broker_order_type(order):
    raw_type = getattr(order, "type", None) or getattr(order, "order_type", None)
    if raw_type is None:
        return None
    return str(getattr(raw_type, "value", raw_type)).lower()


def sync_broker_account_activities():
    if settings.alpaca_activity_sync_seconds < 1:
        raise ValueError("ALPACA_ACTIVITY_SYNC_SECONDS must be at least 1")
    if settings.alpaca_activity_lookback_days < 1:
        raise ValueError("ALPACA_ACTIVITY_LOOKBACK_DAYS must be at least 1")
    base_url = ("https://paper-api.alpaca.markets" if settings.alpaca_paper
                else "https://api.alpaca.markets")
    headers = {
        "APCA-API-KEY-ID": settings.alpaca_api_key,
        "APCA-API-SECRET-KEY": settings.alpaca_secret_key,
    }
    last_synced_by_type = {
        row["activity_type"]: row["last_synced_at"]
        for row in broker_activity_sync_status()
    }
    now = datetime.now(timezone.utc)
    initial_after = now - timedelta(days=settings.alpaca_activity_lookback_days)
    with httpx.Client(base_url=base_url, headers=headers, timeout=15.0) as http_client:
        for activity_type in ACCOUNT_ACTIVITY_TYPES:
            last_synced = last_synced_by_type.get(activity_type)
            after = (last_synced - timedelta(minutes=5)
                     if last_synced is not None else initial_after)
            attempted_at = datetime.now(timezone.utc)
            try:
                activities = fetch_account_activities(
                    http_client, activity_type, after,
                )
                save_broker_activities(activities)
                update_broker_activity_sync(activity_type, attempted_at, True)
            except Exception as exc:
                update_broker_activity_sync(
                    activity_type, attempted_at, False, str(exc)[:1000],
                )
                log.exception(
                    "Alpaca %s account activity sync failed; reports may be stale",
                    activity_type,
                )


def backfill_fill_order_types(client_api: TradingClient, limit: int = 25) -> int:
    """Record the type of filled orders the bot never tracked, mainly bracket stop and take-profit legs."""
    stored = 0
    for broker_order_id in fill_order_ids_missing_type(limit):
        try:
            order_type = broker_order_type(client_api.get_order_by_id(broker_order_id))
        except Exception:
            log.exception("Unable to fetch order type for %s", broker_order_id)
            continue
        if order_type:
            save_broker_order_type(broker_order_id, order_type)
            stored += 1
    return stored


def open_closing_orders(client_api: TradingClient, symbol: str, closing_side: OrderSide):
    orders = client_api.get_orders(GetOrdersRequest(
        status=QueryOrderStatus.OPEN, limit=500, nested=True,
    ))
    return [
        item for order in orders for item in order_tree(order)
        if getattr(item, "symbol", None) == symbol
        and str(getattr(getattr(item, "side", ""), "value",
                        getattr(item, "side", ""))).lower() == closing_side.value
    ]


def orphaned_protective_stops(open_orders, positions) -> list[dict]:
    held_symbols = {position.symbol for position in positions}
    orphaned = []
    for order in open_orders:
        for item in order_tree(order):
            client_order_id = str(getattr(item, "client_order_id", "") or "")
            order_type = broker_order_type(item) or ""
            symbol = getattr(item, "symbol", None)
            if (client_order_id.startswith("kt-protect-") and "stop" in order_type
                    and symbol and symbol not in held_symbols):
                orphaned.append({
                    "symbol": symbol,
                    "broker_order_id": str(getattr(item, "id", "")),
                    "side": str(getattr(getattr(item, "side", ""), "value",
                                        getattr(item, "side", ""))).lower(),
                    "qty": str(getattr(item, "qty", "")),
                    "stop_price": str(getattr(item, "stop_price", "")),
                })
    return orphaned


def cancel_orphaned_protective_stops(client_api: TradingClient, orphaned: list[dict]) -> list[dict]:
    """Cancel bot stops whose position is gone; a triggered stop would open an unintended short."""
    if not orphaned:
        return []
    # Re-read positions so a position opened since the reconcile snapshot keeps its stop.
    held_symbols = {position.symbol for position in client_api.get_all_positions()}
    remaining = []
    for item in orphaned:
        if item["symbol"] in held_symbols:
            remaining.append(item)
            continue
        try:
            client_api.cancel_order_by_id(item["broker_order_id"])
            log.warning("Cancelled orphaned protective stop %s for %s",
                        item["broker_order_id"], item["symbol"])
        except APIError as exc:
            if "order pending cancel" in str(exc).lower():
                log.info("Orphaned protective stop %s for %s is already pending cancellation",
                         item["broker_order_id"], item["symbol"])
                continue
            log.exception("Unable to cancel orphaned protective stop %s for %s",
                          item["broker_order_id"], item["symbol"])
            remaining.append(item)
    return remaining


def refresh_tracked_order_statuses(client_api: TradingClient, open_orders=None):
    roots = managed_order_roots()
    roots_by_client_id = {row["client_order_id"]: row for row in roots}
    roots_by_broker_id = {
        row["broker_order_id"]: row for row in roots if row["broker_order_id"]
    }
    existing_by_broker_id = {
        row["broker_order_id"]: row for row in managed_orders()
        if row["broker_order_id"]
    }
    broker_orders = client_api.get_orders(GetOrdersRequest(
        status=QueryOrderStatus.ALL, limit=500, nested=True,
    ))
    orders_by_id = {str(order.id): order for order in broker_orders}
    for order in open_orders or []:
        orders_by_id.setdefault(str(order.id), order)
    # Orders older than the 500-order window above would otherwise never get their fills stored.
    for broker_order_id in orders_missing_fill_data():
        if broker_order_id in orders_by_id:
            continue
        try:
            orders_by_id[broker_order_id] = client_api.get_order_by_id(
                broker_order_id, filter=GetOrderByIdRequest(nested=True))
        except Exception:
            log.exception("Unable to backfill fill data for order %s", broker_order_id)

    def sync_tree(order, metadata, parent_broker_order_id=None, is_root=False):
        broker_order_id = str(order.id)
        client_order_id = str(getattr(order, "client_order_id", "") or
                              f"alpaca-{broker_order_id}")
        side = str(getattr(getattr(order, "side", ""), "value",
                           getattr(order, "side", ""))).lower()
        status = str(getattr(getattr(order, "status", ""), "value",
                             getattr(order, "status", ""))).lower()
        signal_timestamp = metadata.get("signal_timestamp") if is_root else None
        reference_price = metadata.get("reference_price") if is_root else None
        reference_timestamp = metadata.get("reference_timestamp") if is_root else None
        order_strategy_config = metadata.get("strategy_config")
        filled_qty = getattr(order, "filled_qty", None)
        filled_avg_price = getattr(order, "filled_avg_price", None)
        fees = getattr(order, "fees", None)
        order_records.append({
            "client_order_id": client_order_id,
            "broker_order_id": broker_order_id,
            "parent_broker_order_id": parent_broker_order_id,
            "signal_id": metadata["signal_id"],
            "symbol": str(getattr(order, "symbol", None) or metadata["symbol"]),
            "side": side,
            "qty": float(getattr(order, "qty", None) or 0),
            "pool": metadata["pool"],
            "status": status,
            "signal_timestamp": signal_timestamp,
            "reference_price": reference_price,
            "reference_timestamp": reference_timestamp,
            "strategy_config": order_strategy_config,
            "submitted_at": getattr(order, "submitted_at", None),
            "filled_at": getattr(order, "filled_at", None),
            "filled_qty": float(filled_qty) if filled_qty is not None else None,
            "filled_avg_price": (float(filled_avg_price)
                                 if filled_avg_price is not None else None),
            "order_type": broker_order_type(order),
            "fees": float(fees) if fees is not None else None,
        })
        previous = existing_by_broker_id.get(broker_order_id)
        previous_filled_qty = previous.get("filled_qty") if previous else None
        previous_filled_price = previous.get("filled_avg_price") if previous else None
        fill_changed = previous is not None and (
            float(previous_filled_qty or 0) != float(filled_qty or 0)
            or float(previous_filled_price or 0) != float(filled_avg_price or 0)
            or (fees is not None and previous.get("fees") != float(fees))
            or (broker_order_type(order) is not None
                and previous.get("order_type") != broker_order_type(order))
        )
        if previous is None or previous["status"] != status or fill_changed:
            fill_price = (float(filled_avg_price)
                          if filled_avg_price is not None else None)
            slippage_bps = None
            if reference_price is not None and reference_price > 0 and fill_price is not None:
                slippage_bps = (
                    (fill_price - reference_price) / reference_price * 10_000
                    if side == OrderSide.BUY.value
                    else (reference_price - fill_price) / reference_price * 10_000
                )
            log.info(
                "Order status observed_at=%s signal_id=%s signal_timestamp=%s "
                "client_order_id=%s broker_order_id=%s parent_broker_order_id=%s "
                "symbol=%s pool=%s side=%s status=%s submitted_at=%s filled_at=%s "
                "filled_qty=%s filled_avg_price=%s reference_price=%s "
                "reference_timestamp=%s order_type=%s fees=%s adverse_slippage_bps=%s",
                datetime.now(timezone.utc).isoformat(), metadata["signal_id"],
                signal_timestamp, client_order_id, broker_order_id,
                parent_broker_order_id, getattr(order, "symbol", None) or metadata["symbol"],
                metadata["pool"], side, status, getattr(order, "submitted_at", None),
                getattr(order, "filled_at", None), filled_qty, filled_avg_price,
                reference_price, reference_timestamp, broker_order_type(order),
                fees, slippage_bps,
            )
        for leg in getattr(order, "legs", None) or []:
            sync_tree(leg, metadata, broker_order_id)

    order_records = []
    for order in orders_by_id.values():
        client_order_id = str(getattr(order, "client_order_id", "") or "")
        metadata = (roots_by_client_id.get(client_order_id)
                    or roots_by_broker_id.get(str(order.id)))
        if metadata is not None:
            sync_tree(order, metadata, is_root=True)
    sync_managed_orders(order_records)
    update_latest_account_orders([
        serialize_order(order) for order in orders_by_id.values()
    ])


def close_managed_position(client_api: TradingClient, symbol: str, pool: str,
                           positions, signal_id: str, event: dict,
                           cancel_protective_stop: bool = False) -> bool:
    """Cancel conflicting managed exits before closing so they cannot oversell."""
    position = next((item for item in positions if item.symbol == symbol), None)
    if position is None:
        return True
    closing_side = OrderSide.SELL if float(position.qty) > 0 else OrderSide.BUY
    entry_order_id = managed_order_id(symbol, pool)
    if entry_order_id:
        entry_order = client_api.get_order_by_id(entry_order_id)
        for item in order_tree(entry_order):
            raw_side = getattr(item, "side", "")
            side = str(getattr(raw_side, "value", raw_side)).lower()
            status = str(getattr(getattr(item, "status", ""), "value",
                                 getattr(item, "status", ""))).lower()
            if (getattr(item, "id", None) != entry_order.id
                    and getattr(item, "symbol", None) == symbol
                    and side == closing_side.value
                    and status in {"new", "accepted", "pending_new", "partially_filled",
                                   "held", "accepted_for_bidding"}):
                try:
                    client_api.cancel_order_by_id(item.id)
                except APIError as exc:
                    if "order pending cancel" not in str(exc).lower():
                        raise
                    log.info("Exit order %s for %s is already pending cancellation",
                             item.id, symbol)

    if cancel_protective_stop:
        for order in open_closing_orders(client_api, symbol, closing_side):
            client_order_id = str(getattr(order, "client_order_id", "") or "")
            if not client_order_id.startswith("kt-protect-"):
                continue
            try:
                client_api.cancel_order_by_id(order.id)
                log.info("Cancelling managed protective stop %s before flattening %s",
                         order.id, symbol)
            except APIError as exc:
                if "order pending cancel" not in str(exc).lower():
                    raise
                log.info("Protective stop %s for %s is already pending cancellation",
                         order.id, symbol)

    deadline = time.monotonic() + 5
    while True:
        remaining = open_closing_orders(client_api, symbol, closing_side)
        if not remaining:
            break
        if time.monotonic() >= deadline:
            order_ids = [str(getattr(order, "id", "unknown")) for order in remaining]
            log.warning(
                "Deferring close for %s; closing-side orders still open: %s",
                symbol, ", ".join(order_ids),
            )
            return False
        time.sleep(0.25)

    close_order = client_api.close_position(symbol)
    status = str(getattr(close_order.status, "value", close_order.status)).lower()
    sync_managed_orders([{
        "client_order_id": str(close_order.client_order_id),
        "broker_order_id": str(close_order.id),
        "parent_broker_order_id": None,
        "signal_id": signal_id,
        "symbol": symbol,
        "side": closing_side.value,
        "qty": float(getattr(close_order, "qty", None) or abs(float(position.qty))),
        "pool": pool,
        "status": status,
        "signal_timestamp": datetime.fromisoformat(event["ts"].replace("Z", "+00:00")),
        "reference_price": (float(event["price"])
                            if event.get("price") is not None else None),
        "reference_timestamp": datetime.fromisoformat(event["ts"].replace("Z", "+00:00")),
        "strategy_config": strategy_config(settings, event),
        "submitted_at": getattr(close_order, "submitted_at", None),
        "filled_at": getattr(close_order, "filled_at", None),
        "filled_qty": (float(close_order.filled_qty)
                       if getattr(close_order, "filled_qty", None) is not None else None),
        "filled_avg_price": (float(close_order.filled_avg_price)
                             if getattr(close_order, "filled_avg_price", None) is not None
                             else None),
        "order_type": broker_order_type(close_order),
        "fees": (float(close_order.fees)
                 if getattr(close_order, "fees", None) is not None else None),
    }])
    return True


def flatten_intraday_positions(client_api: TradingClient, positions) -> list[str]:
    """Close intraday-pool positions shortly before the close; day-only exit legs expire at the bell."""
    if settings.intraday_flatten_minutes <= 0:
        return []
    remaining = minutes_to_close(client_api.get_clock())
    if remaining is None or remaining > settings.intraday_flatten_minutes:
        return []
    pools = managed_pool_assignments()
    known = None
    now = datetime.now(timezone.utc)
    event = {"ts": now.isoformat(), "price": None}
    closed = []
    for position in positions:
        symbol = position.symbol
        if pools.get(symbol) != "intraday":
            if symbol not in pools:
                if known is None:
                    known = managed_symbols()
                if symbol in known:
                    log.warning(
                        "Not flattening %s before the close: it is managed but has no "
                        "recorded pool assignment", symbol)
            continue
        try:
            if close_managed_position(client_api, symbol, "intraday", positions,
                                      "eod-flatten", event,
                                      cancel_protective_stop=True):
                closed.append(symbol)
        except Exception:
            log.exception("End-of-day flatten failed for %s", symbol)
    if closed:
        log.warning("End-of-day flatten closed intraday positions: %s", ", ".join(closed))
    return closed


def notify_admin(message: str):
    if not settings.alert_webhook_url:
        log.critical("Alert webhook is not configured: %s", message)
        return
    try:
        response = httpx.post(settings.alert_webhook_url, json={
            "severity": "critical", "service": "kube-trader",
            "alert": "daily_drawdown_circuit_breaker", "message": message,
            "occurred_at": datetime.now(timezone.utc).isoformat(),
        }, timeout=5.0)
        response.raise_for_status()
    except Exception:
        log.exception("Could not deliver circuit-breaker alert")


def reconcile(client_api: TradingClient):
    account = client_api.get_account()
    positions = client_api.get_all_positions()
    open_orders = client_api.get_orders(GetOrdersRequest(
        status=QueryOrderStatus.OPEN, limit=500, nested=True,
    ))
    protected = set()
    for order in open_orders:
        for item in order_tree(order):
            symbol = getattr(item, "symbol", None)
            raw_type = getattr(item, "type", getattr(item, "order_type", ""))
            raw_side = getattr(item, "side", "")
            order_type = str(getattr(raw_type, "value", raw_type)).lower()
            side = str(getattr(raw_side, "value", raw_side)).lower()
            if symbol and "stop" in order_type:
                protected.add((symbol, side))

    broker_positions = []
    stop_gaps = []
    orphaned_stops = orphaned_protective_stops(open_orders, positions)
    if settings.stop_management_enabled:
        orphaned_stops = cancel_orphaned_protective_stops(client_api, orphaned_stops)
    pool_by_symbol = managed_pool_assignments()
    known = managed_symbols()
    exposures = {pool: 0.0 for pool in POOL_WEIGHTS}
    sector_exposures = {}
    open_risk = 0.0
    for position in positions:
        symbol = position.symbol
        qty = float(position.qty)
        avg_entry = float(position.avg_entry_price)
        close_side = OrderSide.SELL if qty > 0 else OrderSide.BUY
        position_report = serialize_position(position)
        position_report.update({
            "symbol": symbol,
            "qty": qty,
            "market_value": float(position.market_value),
            "avg_entry_price": avg_entry,
        })
        broker_positions.append(position_report)
        pool = pool_by_symbol.get(symbol, "core")
        exposures[pool] += abs(float(position.market_value))
        if symbol in known and qty > 0:
            risk_pct = managed_order_stop_loss_pct(symbol, pool)
            if risk_pct is None:
                risk_pct = default_stop_loss_pct(pool)
            open_risk += qty * avg_entry * risk_pct
        sector = sector_for_symbol(symbol, settings)
        sector_exposures[sector] = sector_exposures.get(sector, 0.0) + abs(float(position.market_value))
        if (settings.stop_management_enabled and symbol in pool_by_symbol
                and (symbol, close_side.value) not in protected):
            stop_pct = managed_order_stop_loss_pct(symbol, pool)
            if stop_pct is None:
                stop_pct = default_stop_loss_pct(pool)
            stop_price = avg_entry * (1 - stop_pct if qty > 0 else 1 + stop_pct)
            client_order_id = "kt-protect-" + uuid.uuid4().hex[:24]
            try:
                client_api.submit_order(StopOrderRequest(
                    symbol=symbol, qty=abs(qty), side=close_side, type=OrderType.STOP,
                    time_in_force=TimeInForce.GTC, stop_price=max(round(stop_price, 2), 0.01),
                    client_order_id=client_order_id,
                ))
                protected.add((symbol, close_side.value))
                log.warning("Submitted missing protective stop for %s", symbol)
            except Exception as exc:
                stop_gaps.append({"symbol": symbol, "error": str(exc)})
                log.exception("Unable to restore protective stop for %s", symbol)

    broker_symbols = {item["symbol"] for item in broker_positions}
    equity = float(account.equity)
    buying_power = float(account.buying_power)
    weights = pool_weights()
    if any(weight < 0 for weight in weights.values()) or abs(sum(weights.values()) - 1.0) > 0.0001:
        raise ValueError("INTRADAY, SWING, and CORE allocation percentages must sum to 1.0")
    if not 0 < settings.max_symbol_exposure_pct <= 1 or not 0 < settings.max_sector_exposure_pct <= 1:
        raise ValueError("Symbol and sector exposure limits must be greater than 0 and at most 1")
    if settings.max_open_positions < 1:
        raise ValueError("MAX_OPEN_POSITIONS must be at least 1")
    allocations = {
        pool: {"target_ratio": weight, "target_equity": equity * weight,
               "exposure": exposures[pool],
               "remaining": max(0.0, equity * weight - exposures[pool]),
               "over_target": max(0.0, exposures[pool] - equity * weight)}
        for pool, weight in weights.items()
    }
    ledger_check = reconcile_open_quantities(
        build_realized_trade_report(managed_fills())["open_managed_quantities"],
        broker_positions,
    )
    warnings = []
    for pool, item in allocations.items():
        if item["over_target"] > 0:
            warnings.append(
                f"{pool} exposure exceeds its target by {item['over_target']:.2f}; "
                "new entries for the pool are blocked until it falls below target")
    unclassified = sorted(item["symbol"] for item in broker_positions
                          if sector_for_symbol(item["symbol"], settings) == "unclassified")
    if unclassified:
        warnings.append("Positions without a sector mapping share one capped group; "
                        "add them to SYMBOL_SECTORS: " + ", ".join(unclassified))
    if ledger_check["discrepancies"]:
        warnings.append(
            f"{len(ledger_check['discrepancies'])} symbols differ between the order ledger and "
            "Alpaca; Alpaca positions are treated as authoritative")
    if orphaned_stops:
        symbols = ", ".join(sorted({item["symbol"] for item in orphaned_stops}))
        warnings.append(
            "Bot protective stop orders are open for symbols with no broker position: "
            + symbols)
    unmanaged_bot_origin = sorted(
        (broker_symbols - known) & bot_origin_symbols()
    )
    if unmanaged_bot_origin:
        warnings.append(
            "Bot-created positions have no pool provenance and are not managed; review and "
            "adopt explicitly with POST /admin/positions/{symbol}/adopt: "
            + ", ".join(unmanaged_bot_origin))
    open_risk_pct = open_risk / equity if equity else 0.0
    if (settings.max_portfolio_open_risk_pct > 0
            and open_risk_pct > settings.max_portfolio_open_risk_pct):
        warnings.append(
            f"Open stop-risk is {open_risk_pct:.2%} of equity, above the "
            f"{settings.max_portfolio_open_risk_pct:.2%} cap; new entries are blocked")
    diffs = {"untracked_broker_positions": sorted(broker_symbols - known),
             "unmanaged_bot_origin_positions": unmanaged_bot_origin,
             "managed_positions_missing_at_broker": sorted(known - broker_symbols),
             "positions_missing_protective_stop": stop_gaps,
             "orphaned_protective_stops": orphaned_stops,
             "ledger_discrepancies": ledger_check["discrepancies"],
             "unclassified_symbols": unclassified,
             "open_risk": open_risk,
             "open_risk_pct": open_risk_pct,
             "max_portfolio_open_risk_pct": settings.max_portfolio_open_risk_pct,
             "warnings": warnings,
             "allocations": allocations, "sector_exposures": sector_exposures}
    for warning in warnings:
        log.warning("Reconciliation: %s", warning)
    save_account_snapshot(
        equity, buying_power, broker_positions, diffs,
        alpaca_account=serialize_account(account),
        open_orders=[serialize_order(order) for order in open_orders],
        orders=[serialize_order(order) for order in open_orders],
        trading_mode="paper" if settings.alpaca_paper else "live",
    )

    today = datetime.now(NY).date()
    state = get_risk_state(today)
    history_start = None
    try:
        history = client_api.get_portfolio_history(GetPortfolioHistoryRequest(
            period="1D", timeframe="1Min", intraday_reporting="market_hours",
        ))
        history_values = [float(value) for value in (history.equity or [])
                          if value is not None and float(value) > 0]
        if history_values:
            history_start = history_values[0]
    except Exception:
        log.exception("Portfolio history unavailable; using account previous-close equity")
    fallback_start = float(account.last_equity or equity)
    if not state or state["session_date"] != today:
        save_risk_state(today, history_start or fallback_start, False, None)
        state = get_risk_state(today)
    elif history_start and not state["halted"]:
        save_risk_state(today, history_start, False, None)
        state = get_risk_state(today)
    drawdown = (equity / float(state["starting_equity"])) - 1 if state["starting_equity"] else 0
    limit = settings.daily_drawdown_limit_pct
    if drawdown <= -limit and not state["halted"]:
        reason = f"Daily drawdown {drawdown:.2%} crossed limit {-limit:.2%}"
        save_risk_state(today, float(state["starting_equity"]), True, reason)
        log.critical("CIRCUIT BREAKER: %s; new automated entries halted", reason)
        notify_admin(reason)
        state = get_risk_state(today)
    return (account, positions,
            {"halted": bool(state["halted"]), "drawdown": drawdown,
             "equity": equity, "buying_power": buying_power,
             "open_risk": open_risk,
             "reconciliation": diffs},
            open_orders)


def invalidate_risk_view():
    with RISK_VIEW_LOCK:
        RISK_VIEW["value"] = None
        RISK_VIEW["captured_at"] = 0.0


def cached_risk_view(client_api: TradingClient, max_age_seconds: float | None = None):
    """Reconcile at most once per TTL so queued signals do not each pay for a snapshot."""
    ttl = settings.risk_view_ttl_seconds if max_age_seconds is None else max_age_seconds
    with RISK_VIEW_LOCK:
        value = RISK_VIEW["value"]
        if value is not None and time.monotonic() - RISK_VIEW["captured_at"] <= ttl:
            return value
        value = reconcile(client_api)
        RISK_VIEW["value"] = value
        RISK_VIEW["captured_at"] = time.monotonic()
        return value


def build_entry_request(symbol: str, qty: int, price: float, stop_price: float,
                        take_profit_price: float | None, client_order_id: str,
                        pool: str, force_market: bool = False):
    """Build the entry order and report whether it is a marketable limit."""
    if pool == "intraday":
        if settings.entry_order_type == "marketable_limit" and not force_market:
            limit_price = max(
                round(price * (1 + settings.entry_limit_offset_bps / 10_000.0), 2), 0.01)
            # A distinct client id keeps a cancelled limit order from overwriting the
            # ledger row that is ultimately mapped to the market fallback.
            return LimitOrderRequest(
                symbol=symbol, qty=qty, side=OrderSide.BUY, type=OrderType.LIMIT,
                limit_price=limit_price, time_in_force=TimeInForce.DAY,
                order_class=OrderClass.BRACKET,
                stop_loss=StopLossRequest(stop_price=stop_price),
                take_profit=TakeProfitRequest(limit_price=take_profit_price),
                client_order_id=(client_order_id + "-l")[:48],
            ), True
        return MarketOrderRequest(
            symbol=symbol, qty=qty, side=OrderSide.BUY, type=OrderType.MARKET,
            time_in_force=TimeInForce.DAY, order_class=OrderClass.BRACKET,
            stop_loss=StopLossRequest(stop_price=stop_price),
            take_profit=TakeProfitRequest(limit_price=take_profit_price),
            client_order_id=client_order_id,
        ), False
    # Multi-day positions use a GTC protective stop without an arbitrary take-profit.
    return MarketOrderRequest(
        symbol=symbol, qty=qty, side=OrderSide.BUY, type=OrderType.MARKET,
        time_in_force=TimeInForce.GTC, order_class=OrderClass.OTO,
        stop_loss=StopLossRequest(stop_price=stop_price),
        client_order_id=client_order_id,
    ), False


def order_status(order) -> str:
    return str(getattr(getattr(order, "status", ""), "value",
                       getattr(order, "status", ""))).lower()


def await_fill_or_fallback(client_api: TradingClient, order, symbol: str, qty: int,
                           stop_price: float, take_profit_price: float | None,
                           pool: str, client_order_id: str):
    """Give a marketable limit a short window, then take liquidity for the remainder."""
    deadline = time.monotonic() + max(settings.limit_order_timeout_seconds, 0.0)
    while time.monotonic() < deadline:
        try:
            current = client_api.get_order_by_id(order.id)
        except Exception:
            current = order
        status = order_status(current)
        if status in {"filled", "canceled", "expired", "rejected", "stopped"}:
            return current
        if float(getattr(current, "filled_qty", None) or 0.0) > 0:
            # A partial fill keeps the remaining DAY order working under the broker.
            return current
        time.sleep(0.5)
    try:
        client_api.cancel_order_by_id(order.id)
    except APIError:
        log.exception("Unable to cancel unfilled limit order %s for %s", order.id, symbol)
    try:
        current = client_api.get_order_by_id(order.id)
    except Exception:
        current = order
    if order_status(current) not in {"canceled", "expired", "rejected"}:
        log.warning(
            "Limit order %s for %s is still live after the fallback timeout; leaving it "
            "instead of submitting a second entry", order.id, symbol)
        return current
    remaining = qty - float(getattr(current, "filled_qty", None) or 0.0)
    if remaining < 1:
        return current
    fallback, _ = build_entry_request(
        symbol, int(remaining), 0.0, stop_price, take_profit_price,
        (client_order_id + "-f")[:48], pool, force_market=True)
    log.warning("Limit entry for %s did not fill; submitting a market order for %s shares",
                symbol, int(remaining))
    return client_api.submit_order(fallback)


def place_entry(client_api: TradingClient, event: dict, signal_id: str,
                account, positions, risk: dict) -> bool:
    """Return True only when a new order was submitted for this signal."""
    if event.get("action") != "buy" or risk["halted"]:
        return False
    symbol = event["symbol"]
    clock = client_api.get_clock()
    if not clock.is_open:
        log.info("Ignoring %s entry because the NYSE market is closed", symbol)
        return False
    if any(position.symbol == symbol for position in positions):
        log.info("Ignoring entry for already-held symbol %s", symbol)
        return False
    pool = event.get("pool", "intraday")
    if pool not in POOL_WEIGHTS:
        log.error("Unknown allocation pool %s; rejecting signal", pool)
        return False
    session_now = datetime.now(NY)
    if settings.require_validated_strategy:
        config_key = validation_config_key(strategy_config(settings))["key"]
        try:
            record = latest_strategy_validation(config_key)
        except Exception:
            log.exception("Unable to read strategy validation for %s", config_key)
            record = None
        if not (record and record.get("validated")):
            log.warning(
                "Ignoring entry for %s: strategy configuration %s has no validated "
                "out-of-sample record", symbol, config_key)
            return False
    if pool == "intraday" and settings.intraday_entry_start_minutes > 0:
        elapsed = minutes_since_open(session_now)
        if elapsed is not None and elapsed < settings.intraday_entry_start_minutes:
            log.info("Ignoring intraday entry for %s: %.0f minutes since the open",
                     symbol, elapsed)
            return False
    if pool == "intraday" and settings.min_entry_bar_coverage_pct > 0:
        elapsed = minutes_since_open(session_now)
        if elapsed is not None and elapsed >= 5:
            expected = min(int(elapsed), 390)
            try:
                observed = symbol_session_bar_count(symbol)
                coverage = 100.0 * observed / expected if expected else 0.0
            except Exception:
                log.exception("Unable to read session bar coverage for %s", symbol)
                coverage = 0.0
            if coverage < settings.min_entry_bar_coverage_pct:
                log.warning(
                    "Ignoring intraday entry for %s: %.1f%% of expected session bars "
                    "observed, below %.1f%%", symbol, coverage,
                    settings.min_entry_bar_coverage_pct)
                return False
    if pool == "intraday" and settings.intraday_entry_cutoff_minutes > 0:
        remaining = minutes_to_close(clock)
        if remaining is not None and remaining <= settings.intraday_entry_cutoff_minutes:
            log.info("Ignoring intraday entry for %s: %.0f minutes to close", symbol, remaining)
            return False
    if settings.max_entries_per_5min > 0:
        recent_entries = entry_count_since(300)
        if recent_entries >= settings.max_entries_per_5min:
            log.info("Ignoring entry for %s: %s entries already opened in the last 5 minutes",
                     symbol, recent_entries)
            return False
    if len(positions) >= settings.max_open_positions:
        log.info("Ignoring entry for %s because MAX_OPEN_POSITIONS is reached", symbol)
        return False
    open_orders = client_api.get_orders(GetOrdersRequest(
        status=QueryOrderStatus.OPEN, limit=500, nested=True,
    ))
    if any(getattr(item, "symbol", None) == symbol
           and str(getattr(getattr(item, "side", ""), "value",
                           getattr(item, "side", ""))).lower() == OrderSide.BUY.value
           for order in open_orders for item in order_tree(order)):
        log.info("Ignoring entry for %s because an open buy order already exists", symbol)
        return False
    needs_trade_history = (
        settings.symbol_loss_cooldown_seconds > 0
        or settings.symbol_daily_loss_limit > 0
        or (pool == "intraday" and settings.intraday_daily_loss_limit_pct > 0)
    )
    session_trades = build_realized_trade_report(managed_fills())["trades"] \
        if needs_trade_history else []
    if settings.symbol_loss_cooldown_seconds > 0 or settings.symbol_daily_loss_limit > 0:
        now = datetime.now(NY)
        block = symbol_entry_block(
            session_trades, symbol, now, now.date(),
            settings.symbol_loss_cooldown_seconds, settings.symbol_daily_loss_limit, NY)
        if block:
            log.info("Ignoring entry: %s", block)
            return False
    if pool == "intraday" and settings.intraday_daily_loss_limit_pct > 0 and risk.get("equity"):
        pool_loss = pool_session_realized(
            session_trades, pool, session_now.date(), NY)
        pool_limit = risk["equity"] * settings.intraday_daily_loss_limit_pct
        if pool_loss <= -pool_limit:
            log.warning(
                "Ignoring entry for %s: intraday pool realized %.2f today, beyond the "
                "%.2f session loss limit", symbol, pool_loss, -pool_limit)
            return False
    price = float(event["price"])
    if price <= 0:
        return False
    equity = risk["equity"]
    allocation = risk["reconciliation"]["allocations"][pool]
    sector = sector_for_symbol(symbol, settings)
    symbol_exposure = sum(abs(float(position.market_value)) for position in positions
                          if position.symbol == symbol)
    sector_exposure = sum(abs(float(position.market_value)) for position in positions
                          if sector_for_symbol(position.symbol, settings) == sector)
    available = max(0.0, min(
        allocation["remaining"], risk["buying_power"],
        equity * settings.max_symbol_exposure_pct - symbol_exposure,
        equity * settings.max_sector_exposure_pct - sector_exposure,
    ))
    stop_loss_pct = min(max(float(event.get("stop_loss_pct", settings.stop_loss_pct)), 0.005), 0.25)
    risk_budget = equity * min(settings.max_trade_risk_pct, 0.01)
    qty = min(math.floor(risk_budget / (price * stop_loss_pct)),
              math.floor(available / price), 100)
    qty = cap_entry_quantity(
        qty, price, stop_loss_pct, equity,
        float(risk.get("open_risk") or 0.0), settings.max_portfolio_open_risk_pct,
    )
    if qty < 1:
        log.info("Risk/allocation limits leave no whole shares for %s", symbol)
        return False

    client_order_id = "kt-" + hashlib.sha256(f"{symbol}:{pool}:{signal_id}".encode()).hexdigest()[:28]
    signal_timestamp = datetime.fromisoformat(event["ts"].replace("Z", "+00:00"))
    stop_price = max(round(price * (1 - stop_loss_pct), 2), 0.01)
    take_profit_pct = None
    take_profit_price = None
    if pool == "intraday":
        take_profit_pct = max(
            float(event.get("take_profit_pct", settings.take_profit_pct)),
            stop_loss_pct * 2,
        )
        take_profit_price = max(
            round(price * (1 + take_profit_pct), 2), stop_price + 0.01,
        )
    run_config = strategy_config(settings, event)
    run_config.update({
        "effective_stop_loss_pct": stop_loss_pct,
        "submitted_stop_price": stop_price,
        "order_sizing": {
            "equity_at_entry": equity,
            "pool_available_before_order": available,
            "risk_budget": risk_budget,
            "quantity": qty,
            "notional_at_reference_price": qty * price,
            "risk_at_submitted_stop": qty * max(price - stop_price, 0.0),
        },
    })
    if take_profit_pct is not None:
        run_config["effective_take_profit_pct"] = take_profit_pct
        run_config["submitted_take_profit_price"] = take_profit_price
    if not reserve_order(client_order_id, signal_id, symbol, qty, pool, stop_loss_pct,
                         signal_timestamp, price, run_config):
        try:
            existing = client_api.get_order_by_client_id(client_order_id)
            status = str(getattr(existing.status, "value", existing.status)).lower()
            update_order(
                client_order_id, str(existing.id), status,
                submitted_at=getattr(existing, "submitted_at", None),
                filled_at=getattr(existing, "filled_at", None),
                filled_qty=(float(existing.filled_qty)
                            if getattr(existing, "filled_qty", None) is not None else None),
                filled_avg_price=(float(existing.filled_avg_price)
                                  if getattr(existing, "filled_avg_price", None) is not None
                                  else None),
                order_type=broker_order_type(existing),
                fees=(float(existing.fees)
                      if getattr(existing, "fees", None) is not None else None),
            )
            log.info(
                "Duplicate order suppressed timestamp=%s signal_timestamp=%s signal_id=%s "
                "client_order_id=%s broker_order_id=%s symbol=%s pool=%s status=%s",
                datetime.now(timezone.utc).isoformat(), event.get("ts"), signal_id,
                client_order_id, existing.id, symbol, pool, status,
            )
        except Exception:
            log.exception(
                "Duplicate order reservation found but broker status lookup failed "
                "timestamp=%s signal_timestamp=%s signal_id=%s client_order_id=%s "
                "symbol=%s pool=%s",
                datetime.now(timezone.utc).isoformat(), event.get("ts"), signal_id,
                client_order_id, symbol, pool,
            )
        return False
    try:
        request, is_limit = build_entry_request(
            symbol, qty, price, stop_price, take_profit_price, client_order_id, pool)
        order = client_api.submit_order(request)
        if is_limit:
            order = await_fill_or_fallback(
                client_api, order, symbol, qty, stop_price, take_profit_price,
                pool, client_order_id)
    except Exception:
        update_order(client_order_id, None, "submission_unknown")
        log.exception(
            "Order submission outcome unknown timestamp=%s signal_timestamp=%s signal_id=%s "
            "client_order_id=%s symbol=%s pool=%s qty=%s",
            datetime.now(timezone.utc).isoformat(), event.get("ts"), signal_id,
            client_order_id, symbol, pool, qty,
        )
        return False
    status = str(getattr(order.status, "value", order.status)).lower()
    broker_order_id = str(order.id)
    update_order(
        client_order_id, broker_order_id, status,
        submitted_at=getattr(order, "submitted_at", None),
        filled_at=getattr(order, "filled_at", None),
        filled_qty=(float(order.filled_qty)
                    if getattr(order, "filled_qty", None) is not None else None),
        filled_avg_price=(float(order.filled_avg_price)
                          if getattr(order, "filled_avg_price", None) is not None else None),
        order_type=broker_order_type(order),
        fees=(float(order.fees)
              if getattr(order, "fees", None) is not None else None),
    )
    mode = "paper" if settings.alpaca_paper else "LIVE"
    log.warning(
        "Order submitted timestamp=%s signal_timestamp=%s signal_id=%s "
        "client_order_id=%s broker_order_id=%s mode=%s pool=%s symbol=%s "
        "qty=%s stop=%s status=%s",
        datetime.now(timezone.utc).isoformat(), event.get("ts"), signal_id,
        client_order_id, broker_order_id, mode, pool, symbol, qty, stop_price, status,
    )
    return True


def handle_signal(client_api: TradingClient, event: dict, signal_id: str,
                  risk: dict | None = None) -> bool:
    if not settings.execution_enabled:
        log.info("Execution disabled; signal observed: %s %s", event.get("symbol"), event.get("action"))
        return True
    action = event.get("action")
    if action not in {"buy", "sell"}:
        log.warning("Ignoring unsupported signal action %r for %s",
                    action, event.get("symbol"))
        return True
    if action == "buy" and not settings.new_entries_enabled:
        log.info("New entries disabled; buy signal observed: %s", event.get("symbol"))
        return True
    timestamp = datetime.fromisoformat(event["ts"].replace("Z", "+00:00"))
    age = (datetime.now(timezone.utc) - timestamp.astimezone(timezone.utc)).total_seconds()
    if age < -30 or age > settings.max_signal_age_seconds:
        log.warning("Ignoring stale/future signal for %s (age %.1fs)", event.get("symbol"), age)
        return True
    account, positions, risk, _ = cached_risk_view(client_api)
    if action == "sell":
        assigned_pool = managed_pool_assignments().get(event["symbol"])
        event_pool = event.get("pool", "intraday")
        if assigned_pool != event_pool:
            log.warning(
                "Ignoring unmanaged exit signal timestamp=%s signal_timestamp=%s "
                "signal_id=%s symbol=%s requested_pool=%s assigned_pool=%s",
                datetime.now(timezone.utc).isoformat(), event.get("ts"), signal_id,
                event["symbol"], event_pool, assigned_pool,
            )
            return True
        if event["symbol"] in managed_symbols() and any(
            position.symbol == event["symbol"] for position in positions
        ):
            closed = close_managed_position(
                client_api, event["symbol"], event_pool, positions, signal_id, event,
                cancel_protective_stop=settings.stop_management_enabled,
            )
            if closed:
                invalidate_risk_view()
            return closed
        return True
    with ENTRY_LOCK:
        placed = place_entry(client_api, event, signal_id, account, positions, risk)
    if placed:
        invalidate_risk_view()
    return True


def dedupe_signals(messages) -> tuple[list[tuple[str, dict]], list[str]]:
    """Keep the newest event per symbol and report the message ids to acknowledge."""
    parsed = []
    dropped = []
    for message_id, fields in messages:
        try:
            parsed.append((message_id, json.loads(fields["data"])))
        except Exception:
            log.exception("Dropping unreadable signal %s", message_id)
            dropped.append(message_id)
    newest: dict[str, tuple[tuple[str, str], str, dict]] = {}
    for message_id, event in parsed:
        symbol = str(event.get("symbol") or "")
        stamp = (str(event.get("ts") or ""), message_id)
        current = newest.get(symbol)
        if current is None:
            newest[symbol] = (stamp, message_id, event)
            continue
        if stamp > current[0]:
            dropped.append(current[1])
            newest[symbol] = (stamp, message_id, event)
        else:
            dropped.append(message_id)
    kept = [(message_id, event) for _, message_id, event in newest.values()]
    # Sell first so a freed position or pool budget can fund later buys.
    kept.sort(key=lambda item: (0 if item[1].get("action") == "sell" else 1,
                                str(item[1].get("ts") or ""), item[0]))
    return kept, dropped


def _handle_and_ack(client_api: TradingClient, redis_client, message_id: str, event: dict):
    try:
        started = time.monotonic()
        handled = handle_signal(client_api, event, message_id)
        elapsed = time.monotonic() - started
        if elapsed > settings.execution_latency_warn_seconds:
            log.warning("Signal %s took %.1fs to handle; queued signals are delayed",
                        message_id, elapsed)
        if handled:
            redis_client.xack(SIGNAL_STREAM, "execution-workers", message_id)
    except Exception:
        log.exception("Unable to process execution signal %s", message_id)


def process_batch(client_api: TradingClient, redis_client, messages) -> None:
    """Deduplicate a batch, then place orders concurrently behind the shared risk view."""
    kept, dropped = dedupe_signals(messages)
    for message_id in dropped:
        redis_client.xack(SIGNAL_STREAM, "execution-workers", message_id)
    if not kept:
        return
    workers = min(max(1, settings.execution_max_workers), len(kept))
    if workers == 1:
        for message_id, event in kept:
            _handle_and_ack(client_api, redis_client, message_id, event)
        return
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_handle_and_ack, client_api, redis_client, message_id, event)
                   for message_id, event in kept]
        for future in futures:
            future.result()


def main():
    init_db()
    if not settings.alpaca_api_key or not settings.alpaca_secret_key:
        log.warning("Alpaca credentials missing; execution/reconciliation worker is idle")
        while True:
            time.sleep(60)
    mode = "paper" if settings.alpaca_paper else "LIVE"
    log.warning("Starting Alpaca %s trading client; EXECUTION_ENABLED=%s",
                mode, settings.execution_enabled)
    client_api = TradingClient(settings.alpaca_api_key, settings.alpaca_secret_key,
                               paper=settings.alpaca_paper)
    redis_client = client()
    ensure_group(redis_client, SIGNAL_STREAM, "execution-workers")
    consumer = f"execution-{uuid.uuid4()}"
    last_reconcile = 0.0
    last_activity_sync = 0.0
    while True:
        if time.monotonic() - last_reconcile >= settings.reconcile_interval_seconds:
            reconcile_started = time.monotonic()
            try:
                _, positions, risk, open_orders = cached_risk_view(
                    client_api, max_age_seconds=0.0)
                flatten_intraday_positions(client_api, positions)
                refresh_tracked_order_statuses(client_api, open_orders)
            except Exception:
                invalidate_risk_view()
                log.exception("Broker reconciliation failed; no entries will be processed")
            reconcile_seconds = time.monotonic() - reconcile_started
            if reconcile_seconds > settings.execution_latency_warn_seconds:
                log.warning(
                    "Reconciliation took %.1fs; signal handling waits behind it",
                    reconcile_seconds)
            last_reconcile = time.monotonic()
        if time.monotonic() - last_activity_sync >= settings.alpaca_activity_sync_seconds:
            try:
                sync_broker_account_activities()
                backfill_fill_order_types(client_api)
            except Exception:
                log.exception("Alpaca account activity synchronization failed")
            last_activity_sync = time.monotonic()
        batches = read_group(redis_client, "execution-workers", consumer, SIGNAL_STREAM,
                             count=20, block=5000)
        for _, messages in batches:
            process_batch(client_api, redis_client, messages)


if __name__ == "__main__":
    main()
