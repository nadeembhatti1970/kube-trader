from datetime import datetime
from decimal import Decimal

import httpx


ACCOUNT_FIELDS = (
    "status", "currency", "cash", "portfolio_value", "equity", "last_equity",
    "buying_power", "regt_buying_power", "daytrading_buying_power",
    "effective_buying_power", "options_buying_power", "cash_withdrawable",
    "cash_transferable", "non_marginable_buying_power", "initial_margin",
    "maintenance_margin", "long_market_value", "short_market_value", "multiplier",
    "pattern_day_trader", "trading_blocked", "transfers_blocked",
    "account_blocked", "created_at",
)
POSITION_FIELDS = (
    "asset_id", "symbol", "exchange", "asset_class", "qty", "side",
    "avg_entry_price", "market_value", "cost_basis", "current_price",
    "lastday_price", "change_today", "unrealized_pl", "unrealized_plpc",
)
ORDER_FIELDS = (
    "id", "client_order_id", "parent_order_id", "symbol", "asset_class", "qty", "filled_qty",
    "filled_avg_price", "type", "order_type", "side", "status", "order_class",
    "submitted_at", "filled_at", "canceled_at", "expired_at", "limit_price",
    "stop_price", "time_in_force",
)


def _json_value(value):
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if hasattr(value, "value"):
        return value.value
    return value


def _selected_fields(model, field_names: tuple[str, ...]) -> dict:
    result = {}
    for field in field_names:
        value = getattr(model, field, None)
        if value is not None:
            result[field] = _json_value(value)
    return result


def serialize_account(account) -> dict:
    return _selected_fields(account, ACCOUNT_FIELDS)


def serialize_position(position) -> dict:
    return _selected_fields(position, POSITION_FIELDS)


def serialize_order(order) -> dict:
    result = _selected_fields(order, ORDER_FIELDS)
    result["id"] = str(getattr(order, "id"))
    result["legs"] = [
        serialize_order(leg) for leg in (getattr(order, "legs", None) or [])
    ]
    return result


def fetch_account_activities(client: httpx.Client, activity_type: str,
                             after: datetime, page_size: int = 100) -> list[dict]:
    """Fetch all account activity pages for one Alpaca activity category."""
    activities = []
    page_token = None
    seen_tokens = set()
    while True:
        params = {
            "after": after.isoformat(),
            "direction": "asc",
            "page_size": page_size,
        }
        if page_token:
            params["page_token"] = page_token
        response = client.get(
            f"/v2/account/activities/{activity_type}",
            params=params,
        )
        response.raise_for_status()
        page = response.json()
        if not isinstance(page, list) or any(not isinstance(item, dict) for item in page):
            raise ValueError(f"Alpaca returned an invalid {activity_type} activity page")
        activities.extend(page)
        if len(page) < page_size:
            break
        next_token = page[-1].get("id")
        if not next_token or next_token in seen_tokens:
            raise ValueError(f"Alpaca {activity_type} activity pagination did not advance")
        seen_tokens.add(next_token)
        page_token = next_token
    return activities
