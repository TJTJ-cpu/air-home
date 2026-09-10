"""Look at what has been collected, or export it.

    python readings.py                 # the 20 most recent
    python readings.py --limit 100
    python readings.py --csv           # write data/readings.csv
"""
from __future__ import annotations

import argparse
import csv
from pathlib import Path

import config
import store

COLUMNS = ("room", "captured_at", *config.FIELDS, "notes")
HEADERS = {
    "room": "room",
    "captured_at": "captured (UTC)",
    "temperature_c": "temp C",
    "humidity_pct": "hum %",
    "co2_ppm": "co2",
}


def _cell(value) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


def print_table(rows) -> None:
    """Print rows as an aligned table, newest last so it reads as a timeline."""
    labels = [HEADERS.get(name, name) for name in COLUMNS]
    table = [labels]
    for row in reversed(rows):  # query is newest-first; read oldest-first
        table.append([_cell(row[name]) for name in COLUMNS])

    widths = [max(len(line[i]) for line in table) for i in range(len(COLUMNS))]
    for index, line in enumerate(table):
        print("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(line)).rstrip())
        if index == 0:
            print("  ".join("-" * width for width in widths))


def export_csv(conn, path: Path, room: str | None = None) -> int:
    if room:
        rows = conn.execute("SELECT * FROM readings WHERE room = ? ORDER BY captured_at",
                            (room,)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM readings ORDER BY captured_at").fetchall()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(COLUMNS)
        writer.writerows([row[name] for name in COLUMNS] for row in rows)
    return len(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--room", help="which room (default: all of them)")
    parser.add_argument("--rooms", action="store_true", help="list rooms and exit")
    parser.add_argument("--limit", type=int, default=20, help="how many to show (default 20)")
    parser.add_argument("--all", action="store_true", help="show everything")
    parser.add_argument("--csv", nargs="?", const=config.REPORTS / "readings.csv",
                        type=Path, help="export all readings to CSV")
    args = parser.parse_args()

    if not config.DB_PATH.exists():
        print(f"no database yet at {config.DB_PATH} -- run process.py first")
        return 1

    with store.connect() as conn:
        if args.rooms:
            for row in store.rooms(conn):
                print(f"  {row['room']:12} {row['n']:6} readings   "
                      f"{row['first'][:16].replace('T', ' ')} -> {row['last'][:16].replace('T', ' ')}")
            return 0
        room = config.room_name(args.room) if args.room else None
        if args.csv:
            written = export_csv(conn, args.csv, room)
            print(f"wrote {written} reading(s) to {args.csv}")
            return 0

        total = store.count(conn, room)
        if not total:
            print("no readings yet -- run process.py first")
            return 0

        limit = total if args.all else args.limit
        rows = store.latest(conn, limit, room)
        print_table(rows)
        where = f"for {room}" if room else "across all rooms"
        print(f"\nshowing {len(rows)} of {total} reading(s) {where}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
