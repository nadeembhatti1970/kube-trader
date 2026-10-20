import sqlite3
from pathlib import Path


def store_bars(database_path: str, symbol: str, bars: list[dict]) -> None:
    path = Path(database_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.execute("""
            CREATE TABLE IF NOT EXISTS daily_bars (
              symbol TEXT NOT NULL, day TEXT NOT NULL, open REAL NOT NULL,
              high REAL NOT NULL, low REAL NOT NULL, close REAL NOT NULL,
              volume INTEGER NOT NULL, PRIMARY KEY (symbol, day)
            )
        """)
        connection.executemany(
            "INSERT OR REPLACE INTO daily_bars VALUES (?, ?, ?, ?, ?, ?, ?)",
            [(symbol, b["time"], b["open"], b["high"], b["low"], b["close"], b["volume"])
             for b in bars],
        )

