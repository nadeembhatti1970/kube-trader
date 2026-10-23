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
              created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            );
            ALTER TABLE orders ADD COLUMN IF NOT EXISTS stop_loss_pct DOUBLE PRECISION;
            CREATE TABLE IF NOT EXISTS account_snapshots (
              captured_at TIMESTAMPTZ PRIMARY KEY, equity DOUBLE PRECISION NOT NULL,
              buying_power DOUBLE PRECISION NOT NULL, positions JSONB NOT NULL,
              reconciliation JSONB NOT NULL
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
                  pool: str, stop_loss_pct: float) -> bool:
    with connect() as db:
        result = db.execute("""
          INSERT INTO orders(client_order_id, signal_id, symbol, side, qty, pool, status,
                             stop_loss_pct)
          VALUES (%s, %s, %s, 'buy', %s, %s, 'pending', %s)
          ON CONFLICT(client_order_id) DO NOTHING
        """, (client_order_id, signal_id, symbol, qty, pool, stop_loss_pct))
        return result.rowcount == 1


def update_order(client_order_id: str, broker_id: str | None, status: str):
    with connect() as db:
        db.execute("""
          UPDATE orders SET broker_order_id=COALESCE(%s, broker_order_id), status=%s
          WHERE client_order_id=%s
        """, (broker_id, status, client_order_id))


def tracked_open_orders(limit: int = 100) -> list[dict]:
    with connect() as db:
        return db.execute("""
          SELECT client_order_id, broker_order_id, signal_id, symbol, pool, status, created_at
          FROM orders
          WHERE status NOT IN ('filled', 'canceled', 'expired', 'rejected', 'replaced',
                               'done_for_day', 'suspended')
          ORDER BY created_at DESC
          LIMIT %s
        """, (limit,)).fetchall()


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


def save_account_snapshot(equity: float, buying_power: float, positions: list[dict], diffs: dict):
    with connect() as db:
        db.execute("INSERT INTO account_snapshots VALUES (%s, %s, %s, %s, %s)",
                   (datetime.now(timezone.utc), equity, buying_power,
                    json.dumps(positions), json.dumps(diffs)))


def managed_symbols() -> set[str]:
    with connect() as db:
        rows = db.execute("SELECT DISTINCT symbol FROM orders WHERE status IN "
                          "('accepted','filled','new','partially_filled','pending_new')").fetchall()
    return {row["symbol"] for row in rows}


def managed_pool_assignments() -> dict[str, str]:
    with connect() as db:
        rows = db.execute("""
          SELECT DISTINCT ON (symbol) symbol, pool FROM orders
          WHERE status IN ('accepted','filled','new','partially_filled','pending_new')
          ORDER BY symbol, created_at DESC
        """).fetchall()
    return {row["symbol"]: row["pool"] for row in rows}


def managed_order_id(symbol: str, pool: str):
    with connect() as db:
        row = db.execute("""
          SELECT broker_order_id FROM orders
          WHERE symbol=%s AND pool=%s AND broker_order_id IS NOT NULL
            AND status IN ('accepted','filled','new','partially_filled','pending_new')
          ORDER BY created_at DESC LIMIT 1
        """, (symbol, pool)).fetchone()
    return row["broker_order_id"] if row else None


def managed_order_stop_loss_pct(symbol: str, pool: str):
    with connect() as db:
        row = db.execute("""
          SELECT stop_loss_pct FROM orders
          WHERE symbol=%s AND pool=%s AND stop_loss_pct IS NOT NULL
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
