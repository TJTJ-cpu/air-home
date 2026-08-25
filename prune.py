"""Delete readings you know are wrong.

Shows you what it would remove and changes nothing, until you add --yes.

    python prune.py --at "2026-08-25 16:31"
    python prune.py --from "2026-08-25 16:00" --to "2026-08-25 17:00"
    python prune.py --night 2026-08-24
    python prune.py --incomplete

The photo behind each deleted reading is moved to captures/rejected/ rather
than erased -- so you can still look at what the camera saw, and so rebuild.py
cannot quietly restore the reading from its sidecar later.
"""
from __future__ import annotations

import argparse
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import config
import store

LOCAL_TZ = timezone(timedelta(hours=config.LOCAL_UTC_OFFSET))
FIELDS = list(config.FIELDS)


def _local(captured_at: str) -> datetime:
    return datetime.fromisoformat(captured_at).astimezone(LOCAL_TZ)


def _parse(text: str) -> datetime:
    """Accept '2026-08-25 16:31' or an ISO timestamp, read as local time."""
    for pattern in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, pattern).replace(tzinfo=LOCAL_TZ)
        except ValueError:
            continue
    try:
        stamp = datetime.fromisoformat(text)
    except ValueError:
        raise SystemExit(f"cannot read the time {text!r}; try '2026-08-25 16:31'")
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=LOCAL_TZ)


def select(conn, args) -> list:
    """The rows the given flags point at, oldest first."""
    rows = conn.execute("SELECT * FROM readings ORDER BY captured_at").fetchall()

    if args.at:
        target = _parse(args.at)
        # Nearest reading within a couple of minutes, since you are reading the
        # timestamp off a report rather than typing it to the second.
        near = [(abs((_local(r["captured_at"]) - target).total_seconds()), r) for r in rows]
        near = [(gap, r) for gap, r in near if gap <= 120]
        return [min(near, key=lambda pair: pair[0])[1]] if near else []

    if args.night:
        evening = _parse(args.night).date()
        midnight = datetime.combine(evening, datetime.min.time()).replace(tzinfo=LOCAL_TZ)
        start = midnight + timedelta(hours=config.NIGHT_START_HOUR)
        end = midnight + timedelta(days=1, hours=config.NIGHT_END_HOUR)
        return [r for r in rows if start <= _local(r["captured_at"]) < end]

    if args.start or args.end:
        start = _parse(args.start) if args.start else datetime.min.replace(tzinfo=LOCAL_TZ)
        end = _parse(args.end) if args.end else datetime.max.replace(tzinfo=LOCAL_TZ)
        return [r for r in rows if start <= _local(r["captured_at"]) <= end]

    if args.incomplete:
        return [r for r in rows if any(r[f] is None for f in FIELDS)]

    return []


def show(rows: list) -> None:
    header = ["local time"] + FIELDS
    table = [header]
    for row in rows:
        table.append([f"{_local(row['captured_at']):%Y-%m-%d %H:%M}"]
                     + [("-" if row[f] is None else f"{row[f]:g}") for f in FIELDS])
    widths = [max(len(line[i]) for line in table) for i in range(len(header))]
    for index, line in enumerate(table):
        print("  " + "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(line)))
        if index == 0:
            print("  " + "  ".join("-" * w for w in widths))


def reject_photo(image: str) -> str | None:
    """Move a deleted reading's photo and sidecar out of processed/."""
    if not image:
        return None
    config.REJECTED.mkdir(parents=True, exist_ok=True)
    moved = []
    for suffix in (Path(image).suffix, ".json"):
        source = config.PROCESSED / (Path(image).stem + suffix)
        if source.exists():
            shutil.move(str(source), str(config.REJECTED / source.name))
            moved.append(source.name)
    return ", ".join(moved) or None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    picker = parser.add_argument_group("what to delete (pick one)")
    picker.add_argument("--at", metavar="TIME", help="a single reading, e.g. '2026-08-25 16:31'")
    picker.add_argument("--from", dest="start", metavar="TIME", help="range start, local time")
    picker.add_argument("--to", dest="end", metavar="TIME", help="range end, local time")
    picker.add_argument("--night", metavar="DATE",
                        help="a whole night, by the evening it began (YYYY-MM-DD)")
    picker.add_argument("--incomplete", action="store_true",
                        help="every reading with a missing value")
    parser.add_argument("--yes", action="store_true", help="actually delete (default: preview)")
    parser.add_argument("--keep-photos", action="store_true",
                        help="leave the photos in captures/processed/")
    args = parser.parse_args()

    if not (args.at or args.start or args.end or args.night or args.incomplete):
        parser.print_help()
        return 1

    if not config.DB_PATH.exists():
        print(f"no database at {config.DB_PATH}", file=sys.stderr)
        return 1

    with store.connect() as conn:
        rows = select(conn, args)
        if not rows:
            print("nothing matches that")
            return 0

        print(f"{len(rows)} reading(s) selected:\n")
        show(rows[:40])
        if len(rows) > 40:
            print(f"  ... and {len(rows) - 40} more")

        if not args.yes:
            print(f"\nThis was a preview -- nothing was deleted."
                  f"\nAdd --yes to remove these {len(rows)} reading(s).")
            return 0

        stamps = [(row["captured_at"],) for row in rows]
        conn.executemany("DELETE FROM readings WHERE captured_at = ?", stamps)
        conn.commit()

        moved = 0
        if not args.keep_photos:
            for row in rows:
                if reject_photo(row["image"]):
                    moved += 1

        store.checkpoint(conn)
        print(f"\ndeleted {len(rows)} reading(s); {store.count(conn)} remain")
        if moved:
            print(f"moved {moved} photo(s) to {config.REJECTED}")
        print("run analyze.py to rebuild the report")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
