import hashlib
import json
import logging
import math
import time
import uuid
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import httpx
from alpaca.common.exceptions import APIError
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderClass, OrderSide, OrderType, QueryOrderStatus, TimeInForce
from alpaca.trading.requests import (GetOrdersRequest, MarketOrderRequest, StopLossRequest,
                                     GetPortfolioHistoryRequest, StopOrderRequest,
                                     TakeProfitRequest)

from app.database import (get_risk_state, init_db, managed_order_id, managed_order_stop_loss_pct,
                          managed_order_roots, managed_orders,
                          managed_pool_assignments, managed_symbols, reserve_order,
                          save_account_snapshot, save_risk_state, sync_managed_orders,
                          update_order)
from app.settings import get_settings
from app.streams import SIGNAL_STREAM, client, ensure_group, read_group
from app.universe import sector_for_symbol

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("execution")
settings = get_settings()
NY = ZoneInfo("America/New_York")
POOL_WEIGHTS = {"intraday": 0.20, "swing": 0.30, "core": 0.50}


def pool_weights():
    return {"intraday": settings.intraday_allocation_pct,
            "swing": settings.swing_allocation_pct, "core": settings.core_allocation_pct}


def order_tree(order):
    yield order
    for leg in getattr(order, "legs", None) or []:
        yield from order_tree(leg)


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

    def sync_tree(order, metadata, parent_broker_order_id=None, is_root=False):
        broker_order_id = str(order.id)
        client_order_id = str(getattr(order, "client_order_id", "") or
                              f"alpaca-{broker_order_id}")
        side = str(getattr(getattr(order, "side", ""), "value",
                           getattr(order, "side", ""))).lower()
        status = str(getattr(getattr(order, "status", ""), "value",
                             getattr(order, "status", ""))).lower()
        signal_timestamp = metadata["signal_timestamp"] if is_root else None
        reference_price = metadata["reference_price"] if is_root else None
        filled_qty = getattr(order, "filled_qty", None)
        filled_avg_price = getattr(order, "filled_avg_price", None)
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
            "submitted_at": getattr(order, "submitted_at", None),
            "filled_at": getattr(order, "filled_at", None),
            "filled_qty": float(filled_qty) if filled_qty is not None else None,
            "filled_avg_price": (float(filled_avg_price)
                                 if filled_avg_price is not None else None),
        })
        previous = existing_by_broker_id.get(broker_order_id)
        previous_filled_qty = previous.get("filled_qty") if previous else None
        previous_filled_price = previous.get("filled_avg_price") if previous else None
        fill_changed = previous is not None and (
            float(previous_filled_qty or 0) != float(filled_qty or 0)
            or float(previous_filled_price or 0) != float(filled_avg_price or 0)
        )
        if previous is None or previous["status"] != status or fill_changed:
            fill_price = (float(filled_avg_price)
                          if filled_avg_price is not None else None)
            slippage_bps = None
            if reference_price and fill_price is not None:
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
                "adverse_slippage_bps=%s",
                datetime.now(timezone.utc).isoformat(), metadata["signal_id"],
                signal_timestamp, client_order_id, broker_order_id,
                parent_broker_order_id, getattr(order, "symbol", None) or metadata["symbol"],
                metadata["pool"], side, status, getattr(order, "submitted_at", None),
                getattr(order, "filled_at", None), filled_qty, filled_avg_price,
                reference_price, slippage_bps,
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


def close_managed_position(client_api: TradingClient, symbol: str, pool: str,
                           positions, signal_id: str, event: dict) -> bool:
    """Cancel attached exit legs before closing so a protective order cannot oversell."""
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
        "submitted_at": getattr(close_order, "submitted_at", None),
        "filled_at": getattr(close_order, "filled_at", None),
        "filled_qty": (float(close_order.filled_qty)
                       if getattr(close_order, "filled_qty", None) is not None else None),
        "filled_avg_price": (float(close_order.filled_avg_price)
                             if getattr(close_order, "filled_avg_price", None) is not None
                             else None),
    }])
    return True


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
    pool_by_symbol = managed_pool_assignments()
    exposures = {pool: 0.0 for pool in POOL_WEIGHTS}
    sector_exposures = {}
    for position in positions:
        symbol = position.symbol
        qty = float(position.qty)
        avg_entry = float(position.avg_entry_price)
        close_side = OrderSide.SELL if qty > 0 else OrderSide.BUY
        broker_positions.append({"symbol": symbol, "qty": qty,
                                 "market_value": float(position.market_value),
                                 "avg_entry_price": avg_entry})
        pool = pool_by_symbol.get(symbol, "core")
        exposures[pool] += abs(float(position.market_value))
        sector = sector_for_symbol(symbol, settings)
        sector_exposures[sector] = sector_exposures.get(sector, 0.0) + abs(float(position.market_value))
        if (settings.stop_management_enabled and symbol in pool_by_symbol
                and (symbol, close_side.value) not in protected):
            stop_pct = managed_order_stop_loss_pct(symbol, pool)
            if stop_pct is None:
                stop_pct = settings.stop_loss_pct
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

    known = managed_symbols()
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
               "remaining": max(0.0, equity * weight - exposures[pool])}
        for pool, weight in weights.items()
    }
    diffs = {"untracked_broker_positions": sorted(broker_symbols - known),
             "managed_positions_missing_at_broker": sorted(known - broker_symbols),
             "positions_missing_protective_stop": stop_gaps,
             "allocations": allocations, "sector_exposures": sector_exposures}
    save_account_snapshot(equity, buying_power, broker_positions, diffs)

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
             "reconciliation": diffs},
            open_orders)


def place_entry(client_api: TradingClient, event: dict, signal_id: str,
                account, positions, risk: dict):
    if event.get("action") != "buy" or risk["halted"]:
        return
    symbol = event["symbol"]
    if not client_api.get_clock().is_open:
        log.info("Ignoring %s entry because the US market is closed", symbol)
        return
    if any(position.symbol == symbol for position in positions):
        log.info("Ignoring entry for already-held symbol %s", symbol)
        return
    if len(positions) >= settings.max_open_positions:
        log.info("Ignoring entry for %s because MAX_OPEN_POSITIONS is reached", symbol)
        return
    open_orders = client_api.get_orders(GetOrdersRequest(
        status=QueryOrderStatus.OPEN, limit=500, nested=True,
    ))
    if any(getattr(item, "symbol", None) == symbol
           and str(getattr(getattr(item, "side", ""), "value",
                           getattr(item, "side", ""))).lower() == OrderSide.BUY.value
           for order in open_orders for item in order_tree(order)):
        log.info("Ignoring entry for %s because an open buy order already exists", symbol)
        return
    price = float(event["price"])
    if price <= 0:
        return
    pool = event.get("pool", "intraday")
    if pool not in POOL_WEIGHTS:
        log.error("Unknown allocation pool %s; rejecting signal", pool)
        return
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
    if qty < 1:
        log.info("Risk/allocation limits leave no whole shares for %s", symbol)
        return

    client_order_id = "kt-" + hashlib.sha256(f"{symbol}:{pool}:{signal_id}".encode()).hexdigest()[:28]
    signal_timestamp = datetime.fromisoformat(event["ts"].replace("Z", "+00:00"))
    if not reserve_order(client_order_id, signal_id, symbol, qty, pool, stop_loss_pct,
                         signal_timestamp, price):
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
        return
    stop_price = max(round(price * (1 - stop_loss_pct), 2), 0.01)
    try:
        if pool == "intraday":
            take_profit_pct = max(float(event.get("take_profit_pct", settings.take_profit_pct)),
                                  stop_loss_pct * 2)
            take_profit_price = max(round(price * (1 + take_profit_pct), 2), stop_price + 0.01)
            request = MarketOrderRequest(
                symbol=symbol, qty=qty, side=OrderSide.BUY, type=OrderType.MARKET,
                time_in_force=TimeInForce.DAY, order_class=OrderClass.BRACKET,
                stop_loss=StopLossRequest(stop_price=stop_price),
                take_profit=TakeProfitRequest(limit_price=take_profit_price),
                client_order_id=client_order_id,
            )
        else:
            # Multi-day positions use a GTC protective stop without an arbitrary take-profit.
            request = MarketOrderRequest(
                symbol=symbol, qty=qty, side=OrderSide.BUY, type=OrderType.MARKET,
                time_in_force=TimeInForce.GTC, order_class=OrderClass.OTO,
                stop_loss=StopLossRequest(stop_price=stop_price),
                client_order_id=client_order_id,
            )
        order = client_api.submit_order(request)
    except Exception:
        update_order(client_order_id, None, "submission_unknown")
        log.exception(
            "Order submission outcome unknown timestamp=%s signal_timestamp=%s signal_id=%s "
            "client_order_id=%s symbol=%s pool=%s qty=%s",
            datetime.now(timezone.utc).isoformat(), event.get("ts"), signal_id,
            client_order_id, symbol, pool, qty,
        )
        return
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
    )
    mode = "paper" if settings.alpaca_paper else "LIVE"
    log.warning(
        "Order submitted timestamp=%s signal_timestamp=%s signal_id=%s "
        "client_order_id=%s broker_order_id=%s mode=%s pool=%s symbol=%s "
        "qty=%s stop=%s status=%s",
        datetime.now(timezone.utc).isoformat(), event.get("ts"), signal_id,
        client_order_id, broker_order_id, mode, pool, symbol, qty, stop_price, status,
    )


def handle_signal(client_api: TradingClient, event: dict, signal_id: str, risk: dict) -> bool:
    if not settings.execution_enabled:
        log.info("Execution disabled; signal observed: %s %s", event.get("symbol"), event.get("action"))
        return True
    timestamp = datetime.fromisoformat(event["ts"].replace("Z", "+00:00"))
    age = (datetime.now(timezone.utc) - timestamp.astimezone(timezone.utc)).total_seconds()
    if age < -30 or age > settings.max_signal_age_seconds:
        log.warning("Ignoring stale/future signal for %s (age %.1fs)", event.get("symbol"), age)
        return True
    account, positions, risk, _ = reconcile(client_api)
    if event.get("action") == "sell":
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
            return close_managed_position(
                client_api, event["symbol"], event_pool, positions, signal_id, event,
            )
        return True
    place_entry(client_api, event, signal_id, account, positions, risk)
    return True


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
    risk = {"halted": False}
    while True:
        if time.monotonic() - last_reconcile >= settings.reconcile_interval_seconds:
            try:
                _, _, risk, open_orders = reconcile(client_api)
                refresh_tracked_order_statuses(client_api, open_orders)
            except Exception:
                log.exception("Broker reconciliation failed; no entries will be processed")
                risk = {"halted": True, "equity": 0.0, "buying_power": 0.0}
            last_reconcile = time.monotonic()
        batches = read_group(redis_client, "execution-workers", consumer, SIGNAL_STREAM,
                             count=20, block=5000)
        for _, messages in batches:
            for message_id, fields in messages:
                try:
                    handled = handle_signal(
                        client_api, json.loads(fields["data"]), message_id, risk,
                    )
                    if handled:
                        redis_client.xack(SIGNAL_STREAM, "execution-workers", message_id)
                except Exception:
                    log.exception("Unable to process execution signal %s", message_id)


if __name__ == "__main__":
    main()
