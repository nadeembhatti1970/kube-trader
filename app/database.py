import json
from datetime import datetime, timezone

import psycopg
from psycopg.rows import dict_row

from app.settings import get_settings


def connect():
    return psycopg.connect(get_settings().postgres_dsn, row_factory=dict_row)


def init_db():
    with connect() as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS bars (
              symbol TEXT NOT NULL, ts TIMESTAMPTZ NOT NULL, open DOUBLE PRECISION NOT NULL,
              high DOUBLE PRECISION NOT NULL, low DOUBLE PRECISION NOT NULL,
              close DOUBLE PRECISION NOT NULL, volume BIGINT NOT NULL,
              PRIMARY KEY (symbol, ts)
            );
            CREATE INDEX IF NOT EXISTS bars_symbol_ts ON bars(symbol, ts DESC);
            CREATE TABLE IF NOT EXISTS strategy_state (
              symbol TEXT PRIMARY KEY, above_sma BOOLEAN NOT NULL DEFAULT FALSE
            );
            CREATE TABLE IF NOT EXISTS horizon_strategy_state (
              symbol TEXT NOT NULL, pool TEXT NOT NULL, above_trend BOOLEAN NOT NULL,
              last_bar_date DATE NOT NULL, PRIMARY KEY (symbol, pool)
            );
            CREATE TABLE IF NOT EXISTS backtest_metrics (
              symbol TEXT PRIMARY KEY, as_of TIMESTAMPTZ NOT NULL,
              bars INTEGER NOT NULL, total_return DOUBLE PRECISION NOT NULL,
              max_drawdown DOUBLE PRECISION NOT NULL, sharpe DOUBLE PRECISION NOT NULL
            );
            CREATE TABLE IF NOT EXISTS risk_state (
              id BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (id), session_date DATE NOT NULL,
              starting_equity DOUBLE PRECISION NOT NULL, halted BOOLEAN NOT NULL DEFAULT FALSE,
              reason TEXT
            );
            CREATE TABLE IF NOT EXISTS orders (
              client_order_id TEXT PRIMARY KEY, signal_id TEXT NOT NULL, symbol TEXT NOT NULL,
              side TEXT NOT NULL, qty DOUBLE PRECISION NOT NULL, pool TEXT NOT NULL,
              broker_order_id TEXT, status TEXT NOT NULL, stop_loss_pct DOUBLE PRECISION,
              created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
              parent_broker_order_id TEXT, signal_timestamp TIMESTAMPTZ,
              reference_price DOUBLE PRECISION, submitted_at TIMESTAMPTZ, filled_at TIMESTAMPTZ,
              filled_qty DOUBLE PRECISION, filled_avg_price DOUBLE PRECISION,
              reference_timestamp TIMESTAMPTZ, order_type TEXT, fees DOUBLE PRECISION,
              strategy_config JSONB
            );
            ALTER TABLE orders ADD COLUMN IF NOT EXISTS stop_loss_pct DOUBLE PRECISION;
            ALTER TABLE orders ADD COLUMN IF NOT EXISTS parent_broker_order_id TEXT;
            ALTER TABLE orders ADD COLUMN IF NOT EXISTS signal_timestamp TIMESTAMPTZ;
            ALTER TABLE orders ADD COLUMN IF NOT EXISTS reference_price DOUBLE PRECISION;
            ALTER TABLE orders ADD COLUMN IF NOT EXISTS submitted_at TIMESTAMPTZ;
            ALTER TABLE orders ADD COLUMN IF NOT EXISTS filled_at TIMESTAMPTZ;
            ALTER TABLE orders ADD COLUMN IF NOT EXISTS filled_qty DOUBLE PRECISION;
            ALTER TABLE orders ADD COLUMN IF NOT EXISTS filled_avg_price DOUBLE PRECISION;
            ALTER TABLE orders ADD COLUMN IF NOT EXISTS reference_timestamp TIMESTAMPTZ;
            ALTER TABLE orders ADD COLUMN IF NOT EXISTS order_type TEXT;
            ALTER TABLE orders ADD COLUMN IF NOT EXISTS fees DOUBLE PRECISION;
            ALTER TABLE orders ADD COLUMN IF NOT EXISTS strategy_config JSONB;
            CREATE TABLE IF NOT EXISTS broker_order_types (
              broker_order_id TEXT PRIMARY KEY, order_type TEXT NOT NULL,
              fetched_at TIMESTAMPTZ NOT NULL DEFAULT now()
            );
            CREATE TABLE IF NOT EXISTS account_snapshots (
              captured_at TIMESTAMPTZ PRIMARY KEY, equity DOUBLE PRECISION NOT NULL,
              buying_power DOUBLE PRECISION NOT NULL, positions JSONB NOT NULL,
              reconciliation JSONB NOT NULL, alpaca_account JSONB NOT NULL DEFAULT '{}',
              open_orders JSONB NOT NULL DEFAULT '[]', orders JSONB NOT NULL DEFAULT '[]',
              trading_mode TEXT
            );
            ALTER TABLE account_snapshots ADD COLUMN IF NOT EXISTS alpaca_account JSONB
              NOT NULL DEFAULT '{}';
            ALTER TABLE account_snapshots ADD COLUMN IF NOT EXISTS open_orders JSONB
              NOT NULL DEFAULT '[]';
            ALTER TABLE account_snapshots ADD COLUMN IF NOT EXISTS orders JSONB
              NOT NULL DEFAULT '[]';
            ALTER TABLE account_snapshots ADD COLUMN IF NOT EXISTS trading_mode TEXT;
            CREATE TABLE IF NOT EXISTS broker_activities (
              activity_id TEXT PRIMARY KEY, activity_type TEXT NOT NULL,
              activity_at TIMESTAMPTZ, order_id TEXT, symbol TEXT, side TEXT,
              qty DOUBLE PRECISION, price DOUBLE PRECISION, net_amount DOUBLE PRECISION,
              fees DOUBLE PRECISION,
              raw JSONB NOT NULL
            );
            ALTER TABLE broker_activities ADD COLUMN IF NOT EXISTS fees DOUBLE PRECISION;
            CREATE INDEX IF NOT EXISTS broker_activities_type_time
              ON broker_activities(activity_type, activity_at DESC);
            CREATE INDEX IF NOT EXISTS broker_activities_order
              ON broker_activities(order_id);
            CREATE TABLE IF NOT EXISTS broker_activity_sync (
              activity_type TEXT PRIMARY KEY, last_attempt_at TIMESTAMPTZ,
              last_success_at TIMESTAMPTZ, last_synced_at TIMESTAMPTZ,
              last_error TEXT
            );
            CREATE TABLE IF NOT EXISTS extended_hours_samples (
              sampled_at TIMESTAMPTZ NOT NULL, session TEXT NOT NULL, symbol TEXT NOT NULL,
              feed TEXT NOT NULL, overnight_tradable BOOLEAN, overnight_halted BOOLEAN,
              bid DOUBLE PRECISION, ask DOUBLE PRECISION,
              bid_size DOUBLE PRECISION, ask_size DOUBLE PRECISION,
              spread_bps DOUBLE PRECISION, quote_ts TIMESTAMPTZ,
              PRIMARY KEY (sampled_at, symbol)
            );
            CREATE TABLE IF NOT EXISTS universe_screen (
              symbol TEXT PRIMARY KEY, checked_at TIMESTAMPTZ NOT NULL DEFAULT now(),
              price DOUBLE PRECISION, avg_dollar_volume DOUBLE PRECISION,
              selected BOOLEAN NOT NULL, reason TEXT NOT NULL
            );
        """)


def record_bar(bar: dict):
    with connect() as db:
        db.execute("""
            INSERT INTO bars(symbol, ts, open, high, low, close, volume)
            VALUES (%(symbol)s, %(ts)s, %(open)s, %(high)s, %(low)s, %(close)s, %(volume)s)
            ON CONFLICT(symbol, ts) DO UPDATE SET
              open=excluded.open, high=excluded.high, low=excluded.low,
              close=excluded.close, volume=excluded.volume
        """, bar)


def recent_closes(symbol: str, limit: int) -> list[dict]:
    with connect() as db:
        rows = db.execute(
            "SELECT ts, close FROM bars WHERE symbol=%s ORDER BY ts DESC LIMIT %s",
            (symbol, limit),
        ).fetchall()
    return list(reversed(rows))


def strategy_is_above(symbol: str) -> bool:
    with connect() as db:
        row = db.execute("SELECT above_sma FROM strategy_state WHERE symbol=%s", (symbol,)).fetchone()
    return bool(row and row["above_sma"])


def save_strategy_state(symbol: str, above: bool):
    with connect() as db:
        db.execute("""
          INSERT INTO strategy_state(symbol, above_sma) VALUES (%s, %s)
          ON CONFLICT(symbol) DO UPDATE SET above_sma=excluded.above_sma
        """, (symbol, above))


def horizon_strategy_state(symbol: str, pool: str):
    with connect() as db:
        return db.execute(
            "SELECT above_trend, last_bar_date FROM horizon_strategy_state WHERE symbol=%s AND pool=%s",
            (symbol, pool),
        ).fetchone()


def save_horizon_strategy_state(symbol: str, pool: str, above: bool, bar_date):
    with connect() as db:
        db.execute("""
          INSERT INTO horizon_strategy_state(symbol, pool, above_trend, last_bar_date)
          VALUES (%s, %s, %s, %s)
          ON CONFLICT(symbol, pool) DO UPDATE SET above_trend=excluded.above_trend,
            last_bar_date=excluded.last_bar_date
        """, (symbol, pool, above, bar_date))


def save_backtest(symbol: str, metrics: dict):
    with connect() as db:
        db.execute("""
          INSERT INTO backtest_metrics(symbol, as_of, bars, total_return, max_drawdown, sharpe)
          VALUES (%s, %s, %s, %s, %s, %s)
          ON CONFLICT(symbol) DO UPDATE SET as_of=excluded.as_of, bars=excluded.bars,
            total_return=excluded.total_return, max_drawdown=excluded.max_drawdown,
            sharpe=excluded.sharpe
        """, (symbol, datetime.now(timezone.utc), metrics["bars"], metrics["total_return"],
              metrics["max_drawdown"], metrics["sharpe"]))


def reserve_order(client_order_id: str, signal_id: str, symbol: str, qty: float,
                  pool: str, stop_loss_pct: float, signal_timestamp: datetime,
                  reference_price: float, strategy_config: dict | None = None) -> bool:
    with connect() as db:
        result = db.execute("""
          INSERT INTO orders(client_order_id, signal_id, symbol, side, qty, pool, status,
                             stop_loss_pct, signal_timestamp, reference_price,
                             reference_timestamp, order_type, strategy_config)
          VALUES (%s, %s, %s, 'buy', %s, %s, 'pending', %s, %s, %s, %s, 'market', %s::jsonb)
          ON CONFLICT(client_order_id) DO NOTHING
        """, (client_order_id, signal_id, symbol, qty, pool, stop_loss_pct,
              signal_timestamp, reference_price, signal_timestamp,
              json.dumps(strategy_config) if strategy_config is not None else None))
        return result.rowcount == 1


def update_order(client_order_id: str, broker_id: str | None, status: str,
                 submitted_at: datetime | None = None, filled_at: datetime | None = None,
                 filled_qty: float | None = None,
                 filled_avg_price: float | None = None,
                 order_type: str | None = None, fees: float | None = None):
    with connect() as db:
        db.execute("""
          UPDATE orders SET broker_order_id=COALESCE(%s, broker_order_id), status=%s,
            submitted_at=COALESCE(%s, submitted_at), filled_at=COALESCE(%s, filled_at),
            filled_qty=COALESCE(%s, filled_qty),
            filled_avg_price=COALESCE(%s, filled_avg_price),
            order_type=COALESCE(%s, order_type), fees=COALESCE(%s, fees)
          WHERE client_order_id=%s
        """, (broker_id, status, submitted_at, filled_at, filled_qty, filled_avg_price,
              order_type, fees, client_order_id))


def sync_managed_orders(orders: list[dict]):
    if not orders:
        return
    records = [
        {
            **order,
            "strategy_config": (json.dumps(order["strategy_config"])
                                if order.get("strategy_config") is not None else None),
        }
        for order in orders
    ]
    with connect() as db:
        db.cursor().executemany("""
          INSERT INTO orders(client_order_id, signal_id, symbol, side, qty, pool, broker_order_id,
                             status, parent_broker_order_id, signal_timestamp, reference_price,
                             submitted_at, filled_at, filled_qty, filled_avg_price,
                             reference_timestamp, order_type, fees, strategy_config)
          VALUES (%(client_order_id)s, %(signal_id)s, %(symbol)s, %(side)s, %(qty)s, %(pool)s,
                  %(broker_order_id)s, %(status)s, %(parent_broker_order_id)s,
                  %(signal_timestamp)s, %(reference_price)s, %(submitted_at)s,
                  %(filled_at)s, %(filled_qty)s, %(filled_avg_price)s,
                  %(reference_timestamp)s, %(order_type)s, %(fees)s,
                  %(strategy_config)s::jsonb)
          ON CONFLICT(client_order_id) DO UPDATE SET
            broker_order_id=excluded.broker_order_id, status=excluded.status,
            parent_broker_order_id=COALESCE(excluded.parent_broker_order_id,
                                            orders.parent_broker_order_id),
            signal_timestamp=COALESCE(excluded.signal_timestamp, orders.signal_timestamp),
            reference_price=COALESCE(excluded.reference_price, orders.reference_price),
            submitted_at=COALESCE(excluded.submitted_at, orders.submitted_at),
            filled_at=COALESCE(excluded.filled_at, orders.filled_at),
            filled_qty=COALESCE(excluded.filled_qty, orders.filled_qty),
            filled_avg_price=COALESCE(excluded.filled_avg_price, orders.filled_avg_price),
            reference_timestamp=COALESCE(excluded.reference_timestamp,
                                         orders.reference_timestamp),
            order_type=COALESCE(excluded.order_type, orders.order_type),
            fees=COALESCE(excluded.fees, orders.fees),
            strategy_config=COALESCE(excluded.strategy_config, orders.strategy_config)
        """, records)


def managed_order_roots(limit: int = 5000) -> list[dict]:
    with connect() as db:
        return db.execute("""
          SELECT client_order_id, broker_order_id, signal_id, symbol, pool, status,
                 signal_timestamp, reference_price, reference_timestamp, strategy_config
          FROM orders
          WHERE parent_broker_order_id IS NULL
          ORDER BY created_at DESC LIMIT %s
        """, (limit,)).fetchall()


def managed_orders(limit: int = 3000) -> list[dict]:
    with connect() as db:
        return db.execute("""
          SELECT client_order_id, broker_order_id, signal_id, symbol, side, pool, status,
                 filled_qty, filled_avg_price, order_type, fees
          FROM orders ORDER BY created_at DESC LIMIT %s
        """, (limit,)).fetchall()


def orders_missing_fill_data(limit: int = 50) -> list[str]:
    """Broker ids of orders marked filled whose fill quantity or price was never stored."""
    with connect() as db:
        rows = db.execute("""
          SELECT broker_order_id FROM orders
          WHERE status = 'filled' AND broker_order_id IS NOT NULL
            AND (filled_qty IS NULL OR filled_avg_price IS NULL)
          ORDER BY created_at DESC LIMIT %s
        """, (limit,)).fetchall()
    return [row["broker_order_id"] for row in rows]


def fill_order_ids_missing_type(limit: int = 50) -> list[str]:
    """Broker ids of filled orders (e.g. bracket exit legs) whose order type was never stored."""
    with connect() as db:
        rows = db.execute("""
          SELECT a.order_id FROM broker_activities a
          LEFT JOIN orders o ON o.broker_order_id = a.order_id
          LEFT JOIN broker_order_types t ON t.broker_order_id = a.order_id
          WHERE a.activity_type = 'FILL' AND a.order_id IS NOT NULL
            AND o.order_type IS NULL AND t.broker_order_id IS NULL
          GROUP BY a.order_id ORDER BY MAX(a.activity_at) DESC LIMIT %s
        """, (limit,)).fetchall()
    return [row["order_id"] for row in rows]


def save_broker_order_type(broker_order_id: str, order_type: str):
    with connect() as db:
        db.execute("""
          INSERT INTO broker_order_types(broker_order_id, order_type) VALUES (%s, %s)
          ON CONFLICT(broker_order_id) DO UPDATE SET order_type=excluded.order_type,
            fetched_at=now()
        """, (broker_order_id, order_type))


def managed_fills() -> list[dict]:
    """Bot-recorded order fills plus Alpaca FILL activities for orders the bot never recorded.

    Orders with no recorded fill data (bracket exit legs, orders placed outside the bot, or bot
    orders whose fills were never stored) use Alpaca fills aggregated per order. They keep the
    bot's pool, signal and order type when recorded, else the pool of the symbol's latest earlier order or "unattributed".
    """
    with connect() as db:
        return db.execute("""
          WITH activity_fills AS (
            SELECT a.order_id, a.symbol,
                   CASE WHEN a.side LIKE 'buy%%' THEN 'buy' ELSE 'sell' END AS side,
                   SUM(a.qty) AS qty, MAX(a.activity_at) AS filled_at,
                   SUM(a.qty * a.price) / SUM(a.qty) AS avg_price,
                   CASE WHEN COUNT(a.fees) = COUNT(*) THEN SUM(a.fees) END AS fees
            FROM broker_activities a
            WHERE a.activity_type = 'FILL' AND a.order_id IS NOT NULL AND a.qty > 0
              AND a.price IS NOT NULL AND a.symbol IS NOT NULL
              AND a.side IN ('buy', 'sell', 'sell_short', 'buy_to_cover')
            GROUP BY a.order_id, a.symbol, 3
          ), broker_only AS (
            SELECT COALESCE(o.client_order_id, 'broker-' || f.order_id) AS client_order_id,
                   f.order_id AS broker_order_id, o.signal_id, f.symbol, f.side,
                   COALESCE(o.pool, sp.pool, 'unattributed') AS pool, f.qty,
                   o.signal_timestamp, o.reference_price, o.submitted_at, f.filled_at,
                   f.qty AS filled_qty, f.avg_price AS filled_avg_price,
                   o.reference_timestamp, COALESCE(o.order_type, t.order_type) AS order_type,
                   COALESCE(o.fees, f.fees) AS fees, o.strategy_config
            FROM activity_fills f
            LEFT JOIN orders o ON o.broker_order_id = f.order_id
            LEFT JOIN broker_order_types t ON t.broker_order_id = f.order_id
            LEFT JOIN LATERAL (
              SELECT p.pool FROM orders p
              WHERE p.symbol = f.symbol AND p.pool IS NOT NULL
                AND p.created_at <= f.filled_at
              ORDER BY p.created_at DESC LIMIT 1
            ) sp ON TRUE
            WHERE o.broker_order_id IS NULL
               OR NOT (COALESCE(o.filled_qty, 0) > 0 AND o.filled_avg_price IS NOT NULL)
          ), recorded AS (
            SELECT client_order_id, broker_order_id, signal_id, symbol, side, pool, qty,
                   signal_timestamp, reference_price, submitted_at, filled_at,
                   filled_qty, filled_avg_price, reference_timestamp, order_type, fees,
                   strategy_config
            FROM orders
            WHERE filled_qty > 0 AND filled_avg_price IS NOT NULL
              AND side IN ('buy', 'sell')
          )
          SELECT * FROM (
            SELECT * FROM recorded UNION ALL SELECT * FROM broker_only
          ) combined
          ORDER BY COALESCE(filled_at, submitted_at), client_order_id
        """).fetchall()


def get_risk_state(session_date):
    with connect() as db:
        return db.execute("SELECT * FROM risk_state WHERE id=TRUE AND session_date=%s",
                          (session_date,)).fetchone()


def save_risk_state(session_date, equity: float, halted: bool, reason: str | None):
    with connect() as db:
        db.execute("""
          INSERT INTO risk_state(id, session_date, starting_equity, halted, reason)
          VALUES (TRUE, %s, %s, %s, %s)
          ON CONFLICT(id) DO UPDATE SET session_date=excluded.session_date,
            starting_equity=excluded.starting_equity, halted=excluded.halted, reason=excluded.reason
        """, (session_date, equity, halted, reason))


def save_account_snapshot(equity: float, buying_power: float, positions: list[dict], diffs: dict,
                          alpaca_account: dict | None = None,
                          open_orders: list[dict] | None = None,
                          orders: list[dict] | None = None,
                          trading_mode: str | None = None):
    with connect() as db:
        db.execute("""
          INSERT INTO account_snapshots(
            captured_at, equity, buying_power, positions, reconciliation,
            alpaca_account, open_orders, orders, trading_mode
          ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (datetime.now(timezone.utc), equity, buying_power,
              json.dumps(positions, default=str), json.dumps(diffs, default=str),
              json.dumps(alpaca_account or {}, default=str),
              json.dumps(open_orders or [], default=str), json.dumps(orders or [], default=str),
              trading_mode))


def update_latest_account_orders(orders: list[dict]):
    with connect() as db:
        db.execute("""
          UPDATE account_snapshots SET orders=%s
          WHERE captured_at=(SELECT captured_at FROM account_snapshots
                             ORDER BY captured_at DESC LIMIT 1)
        """, (json.dumps(orders, default=str),))


def save_broker_activities(activities: list[dict]):
    if not activities:
        return
    rows = []
    for activity in activities:
        activity_at = activity.get("transaction_time") or activity.get("date")
        fee_value = activity.get("fees")
        if fee_value is None:
            fee_value = activity.get("fee")
        rows.append({
            "activity_id": activity["id"],
            "activity_type": activity["activity_type"],
            "activity_at": activity_at,
            "order_id": activity.get("order_id"),
            "symbol": activity.get("symbol"),
            "side": activity.get("side"),
            "qty": activity.get("qty"),
            "price": activity.get("price"),
            "net_amount": activity.get("net_amount"),
            "fees": float(fee_value) if fee_value is not None else None,
            "raw": json.dumps(activity),
        })
    with connect() as db:
        db.cursor().executemany("""
          INSERT INTO broker_activities(
            activity_id, activity_type, activity_at, order_id, symbol, side,
            qty, price, net_amount, fees, raw
          ) VALUES (
            %(activity_id)s, %(activity_type)s, %(activity_at)s, %(order_id)s,
            %(symbol)s, %(side)s, %(qty)s, %(price)s, %(net_amount)s, %(fees)s, %(raw)s
          )
          ON CONFLICT(activity_id) DO UPDATE SET
            activity_type=excluded.activity_type, activity_at=excluded.activity_at,
            order_id=excluded.order_id, symbol=excluded.symbol, side=excluded.side,
            qty=excluded.qty, price=excluded.price, net_amount=excluded.net_amount,
            fees=excluded.fees,
            raw=excluded.raw
        """, rows)


def update_broker_activity_sync(activity_type: str, attempted_at: datetime,
                                successful: bool, error: str | None = None):
    with connect() as db:
        db.execute("""
          INSERT INTO broker_activity_sync(
            activity_type, last_attempt_at, last_success_at, last_synced_at, last_error
          ) VALUES (%s, %s, %s, %s, %s)
          ON CONFLICT(activity_type) DO UPDATE SET
            last_attempt_at=excluded.last_attempt_at,
            last_success_at=COALESCE(excluded.last_success_at,
                                     broker_activity_sync.last_success_at),
            last_synced_at=COALESCE(excluded.last_synced_at,
                                    broker_activity_sync.last_synced_at),
            last_error=excluded.last_error
        """, (
            activity_type, attempted_at, attempted_at if successful else None,
            attempted_at if successful else None, error,
        ))


def broker_activity_sync_status() -> list[dict]:
    with connect() as db:
        return db.execute("""
          SELECT activity_type, last_attempt_at, last_success_at, last_synced_at, last_error
          FROM broker_activity_sync ORDER BY activity_type
        """).fetchall()


def broker_activity_summary() -> list[dict]:
    with connect() as db:
        return db.execute("""
          SELECT activity_type, COUNT(*) AS count, MAX(activity_at) AS latest_activity_at,
                 SUM(net_amount) AS net_amount_total, SUM(fees) AS explicit_fees_total,
                 COUNT(fees) AS activities_with_fees
          FROM broker_activities
          GROUP BY activity_type ORDER BY activity_type
        """).fetchall()


def latest_broker_activities(limit: int = 500) -> list[dict]:
    with connect() as db:
        return db.execute("""
          SELECT activity_id, activity_type, activity_at, order_id, symbol, side,
                 qty, price, net_amount, fees, raw
          FROM broker_activities
          ORDER BY activity_at DESC NULLS LAST, activity_id DESC
          LIMIT %s
        """, (limit,)).fetchall()


def managed_symbols() -> set[str]:
    """Symbols with a net open filled quantity or a live unfilled entry; closed ones are excluded.

    Net quantity includes Alpaca-reported exit fills the bot did not record. Fills that could not
    be attributed to a bot pool do not make a symbol managed.
    """
    net: dict[str, float] = {}
    for fill in managed_fills():
        if fill["pool"] == "unattributed":
            continue
        sign = 1 if fill["side"] == "buy" else -1
        net[fill["symbol"]] = net.get(fill["symbol"], 0.0) + sign * float(fill["filled_qty"])
    with connect() as db:
        pending = db.execute("""
          SELECT DISTINCT symbol FROM orders
          WHERE side='buy' AND COALESCE(filled_qty, 0) = 0
            AND status IN ('accepted','new','partially_filled','pending_new')
        """).fetchall()
    return {symbol for symbol, qty in net.items() if qty > 0.000001} | {
        row["symbol"] for row in pending}


def managed_pool_assignments() -> dict[str, str]:
    with connect() as db:
        rows = db.execute("""
          SELECT DISTINCT ON (symbol) symbol, pool FROM orders
          WHERE side='buy'
            AND status IN ('accepted','filled','new','partially_filled','pending_new')
          ORDER BY symbol, created_at DESC
        """).fetchall()
    return {row["symbol"]: row["pool"] for row in rows}


def managed_order_id(symbol: str, pool: str):
    with connect() as db:
        row = db.execute("""
          SELECT broker_order_id FROM orders
          WHERE symbol=%s AND pool=%s AND side='buy' AND parent_broker_order_id IS NULL
            AND broker_order_id IS NOT NULL
            AND status IN ('accepted','filled','new','partially_filled','pending_new')
          ORDER BY created_at DESC LIMIT 1
        """, (symbol, pool)).fetchone()
    return row["broker_order_id"] if row else None


def managed_order_stop_loss_pct(symbol: str, pool: str):
    with connect() as db:
        row = db.execute("""
          SELECT stop_loss_pct FROM orders
          WHERE symbol=%s AND pool=%s AND side='buy' AND stop_loss_pct IS NOT NULL
            AND status IN ('accepted','filled','new','partially_filled','pending_new')
          ORDER BY created_at DESC LIMIT 1
        """, (symbol, pool)).fetchone()
    return float(row["stop_loss_pct"]) if row else None


def latest_backtest(symbol: str):
    with connect() as db:
        return db.execute("SELECT * FROM backtest_metrics WHERE symbol=%s", (symbol,)).fetchone()


def latest_account_snapshot():
    with connect() as db:
        return db.execute("SELECT * FROM account_snapshots ORDER BY captured_at DESC LIMIT 1").fetchone()


def save_universe_snapshot(records: list[dict]):
    with connect() as db:
        db.execute("DELETE FROM universe_screen")
        db.cursor().executemany("""
          INSERT INTO universe_screen(symbol, price, avg_dollar_volume, selected, reason)
          VALUES (%(symbol)s, %(price)s, %(avg_dollar_volume)s, %(selected)s, %(reason)s)
        """, records)


def latest_universe_snapshot() -> list[dict]:
    with connect() as db:
        rows = db.execute("""
          SELECT symbol, checked_at, price, avg_dollar_volume, selected, reason
          FROM universe_screen ORDER BY selected DESC, avg_dollar_volume DESC NULLS LAST, symbol
        """).fetchall()
    return list(rows)


def save_extended_hours_samples(rows: list[dict], retention_days: int = 30):
    with connect() as db:
        db.cursor().executemany("""
          INSERT INTO extended_hours_samples(sampled_at, session, symbol, feed, overnight_tradable,
            overnight_halted, bid, ask, bid_size, ask_size, spread_bps, quote_ts)
          VALUES (%(sampled_at)s, %(session)s, %(symbol)s, %(feed)s, %(overnight_tradable)s,
            %(overnight_halted)s, %(bid)s, %(ask)s, %(bid_size)s, %(ask_size)s,
            %(spread_bps)s, %(quote_ts)s)
          ON CONFLICT DO NOTHING
        """, rows)
        db.execute("DELETE FROM extended_hours_samples WHERE sampled_at < now() - make_interval(days => %s)",
                   (retention_days,))


def extended_hours_report(days: int = 7) -> dict:
    """Spreads count only quotes at most 10 minutes old; stale quotes show as lower coverage."""
    params = {"days": days}
    with connect() as db:
        sessions = db.execute("""
          SELECT session, count(*) AS samples, count(DISTINCT symbol) AS symbols,
                 min(sampled_at) AS first_sample, max(sampled_at) AS last_sample,
                 round((100.0 * count(*) FILTER (WHERE spread_bps IS NOT NULL AND quote_ts >= sampled_at - interval '10 minutes')
                        / count(*))::numeric, 1) AS fresh_quote_pct,
                 round((percentile_cont(0.5) WITHIN GROUP (ORDER BY spread_bps)
                        FILTER (WHERE quote_ts >= sampled_at - interval '10 minutes'))::numeric, 2) AS median_spread_bps,
                 round((percentile_cont(0.9) WITHIN GROUP (ORDER BY spread_bps)
                        FILTER (WHERE quote_ts >= sampled_at - interval '10 minutes'))::numeric, 2) AS p90_spread_bps
          FROM extended_hours_samples
          WHERE sampled_at >= now() - make_interval(days => %(days)s)
          GROUP BY session ORDER BY session
        """, params).fetchall()
        symbols = db.execute("""
          SELECT session, symbol, count(*) AS samples,
                 bool_or(overnight_tradable) AS overnight_tradable,
                 bool_or(overnight_halted) AS overnight_halted,
                 round((100.0 * count(*) FILTER (WHERE spread_bps IS NOT NULL AND quote_ts >= sampled_at - interval '10 minutes')
                        / count(*))::numeric, 1) AS fresh_quote_pct,
                 round((percentile_cont(0.5) WITHIN GROUP (ORDER BY spread_bps)
                        FILTER (WHERE quote_ts >= sampled_at - interval '10 minutes'))::numeric, 2) AS median_spread_bps,
                 round((percentile_cont(0.9) WITHIN GROUP (ORDER BY spread_bps)
                        FILTER (WHERE quote_ts >= sampled_at - interval '10 minutes'))::numeric, 2) AS p90_spread_bps,
                 round((avg(bid_size + ask_size) FILTER (WHERE quote_ts >= sampled_at - interval '10 minutes'))::numeric, 0)
                   AS avg_quoted_size
          FROM extended_hours_samples
          WHERE sampled_at >= now() - make_interval(days => %(days)s)
          GROUP BY session, symbol ORDER BY session, median_spread_bps NULLS LAST, symbol
        """, params).fetchall()
    return {"days": days, "by_session": list(sessions), "by_symbol": list(symbols)}
