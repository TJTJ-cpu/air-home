"""Local SQLite ledger for extracted readings.

One row per photo, keyed on the capture time. Everything lives in
data/readings.db -- no network, no account, no sync.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS readings (
    captured_at   TEXT PRIMARY KEY,   -- UTC ISO8601, from the photo filename
    image         TEXT NOT NULL,
    aqi           INTEGER,
    temperature_c REAL,
    humidity_pct  REAL,
    pm25          INTEGER,
    pm10          INTEGER,
    co2_ppm       INTEGER,
    notes         TEXT,               -- values rejected as implausible
    recorded_at   TEXT NOT NULL
);
"""


@contextmanager
def connect(path: Path | None = None):
    path = path or config.DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    # timeout: wait for a concurrent writer rather than failing instantly.
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        # WAL lets a reader (readings.py) work while a writer is running.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)
        yield conn
        conn.commit()
    finally:
        conn.close()


def save(conn: sqlite3.Connection, captured_at: str, image: str,
         reading: dict, notes: list[str]) -> bool:
    """Insert one reading. Returns False if that timestamp is already stored."""
    row = {
        "captured_at": captured_at,
        "image": image,
        "notes": "; ".join(notes) or None,
        "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        **{field: reading.get(field) for field in config.FIELDS},
    }
    columns = ", ".join(row)
    placeholders = ", ".join(f":{name}" for name in row)
    cursor = conn.execute(
        f"INSERT OR IGNORE INTO readings ({columns}) VALUES ({placeholders})", row
    )
    return cursor.rowcount > 0


def count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0]


def latest(conn: sqlite3.Connection, limit: int = 10) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM readings ORDER BY captured_at DESC LIMIT ?", (limit,)
    ).fetchall()
