"""Local SQLite ledger for extracted readings.

One row per photo, keyed on (room, capture time). Everything lives in
data/readings.db -- no network, no account, no sync.

A single database holds every room rather than one file per room, so rooms can
be compared without opening several databases.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS readings (
    room          TEXT NOT NULL,      -- which room this reading came from
    captured_at   TEXT NOT NULL,      -- UTC ISO8601, from the photo filename
    image         TEXT NOT NULL,
    aqi           INTEGER,
    temperature_c REAL,
    humidity_pct  REAL,
    pm25          INTEGER,
    pm10          INTEGER,
    co2_ppm       INTEGER,
    notes         TEXT,               -- values rejected as implausible
    recorded_at   TEXT NOT NULL,
    PRIMARY KEY (room, captured_at)
);
CREATE INDEX IF NOT EXISTS readings_room_time ON readings (room, captured_at);
"""


def migrate(conn: sqlite3.Connection) -> str | None:
    """Add the room column to a pre-multi-room database.

    The old schema had captured_at as the sole primary key and no room at all.
    Existing readings are assigned to LEGACY_ROOM, since they were all recorded
    before rooms existed. Returns a message if anything was migrated.
    """
    tables = {row[0] for row in
              conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "readings" not in tables:
        return None
    columns = {row[1] for row in conn.execute("PRAGMA table_info(readings)")}
    if "room" in columns:
        return None

    moved = conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0]
    conn.executescript(f"""
        ALTER TABLE readings RENAME TO readings_old;
        {SCHEMA}
        INSERT INTO readings
            (room, captured_at, image, aqi, temperature_c, humidity_pct,
             pm25, pm10, co2_ppm, notes, recorded_at)
        SELECT '{config.LEGACY_ROOM}', captured_at, image, aqi, temperature_c,
               humidity_pct, pm25, pm10, co2_ppm, notes, recorded_at
        FROM readings_old;
        DROP TABLE readings_old;
    """)
    conn.commit()
    return f"migrated {moved} reading(s) into room '{config.LEGACY_ROOM}'"


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
        note = migrate(conn)
        if note:
            print(note)
        conn.executescript(SCHEMA)
        yield conn
        conn.commit()
    finally:
        checkpoint(conn)
        conn.close()


def checkpoint(conn: sqlite3.Connection) -> None:
    """Fold the write-ahead log back into readings.db.

    Until this runs, recent readings live only in readings.db-wal -- so a copy
    or a commit of readings.db alone would silently be missing them. Cheap
    enough to call often.
    """
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except sqlite3.Error:
        pass  # another connection is mid-write; the next attempt will get it


def save(conn: sqlite3.Connection, room: str, captured_at: str, image: str,
         reading: dict, notes: list[str]) -> bool:
    """Insert one reading. False if that room already has that timestamp."""
    row = {
        "room": config.room_name(room),
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


def count(conn: sqlite3.Connection, room: str | None = None) -> int:
    if room:
        return conn.execute("SELECT COUNT(*) FROM readings WHERE room = ?",
                            (config.room_name(room),)).fetchone()[0]
    return conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0]


def rooms(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Every room that has readings, most recently active first."""
    return conn.execute(
        "SELECT room, COUNT(*) AS n, MIN(captured_at) AS first,"
        "       MAX(captured_at) AS last "
        "FROM readings GROUP BY room ORDER BY last DESC"
    ).fetchall()


def latest_room(conn: sqlite3.Connection) -> str | None:
    """The room with the newest reading, used as the default for reports."""
    row = conn.execute(
        "SELECT room FROM readings ORDER BY captured_at DESC LIMIT 1").fetchone()
    return row["room"] if row else None


def latest(conn: sqlite3.Connection, limit: int = 10,
           room: str | None = None) -> list[sqlite3.Row]:
    if room:
        return conn.execute(
            "SELECT * FROM readings WHERE room = ? ORDER BY captured_at DESC LIMIT ?",
            (config.room_name(room), limit)).fetchall()
    return conn.execute(
        "SELECT * FROM readings ORDER BY captured_at DESC LIMIT ?", (limit,)).fetchall()
