"""Delete readings you know are wrong.

Shows you what it would remove and changes nothing, until you add --yes.

    python prune.py --room tj --at "2026-08-25 16:31"
    python prune.py --room tj --everything     # wipe a whole room
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
    if args.room:
        rows = conn.execute(
            "SELECT * FROM readings WHERE room = ? ORDER BY captured_at",
            (config.room_name(args.room),)).fetchall()
    else:
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
    header = ["room", "local time"] + FIELDS
    table = [header]
    for row in rows:
        table.append([row["room"], f"{_local(row['captured_at']):%Y-%m-%d %H:%M}"]
                     + [("-" if row[f] is None else f"{row[f]:g}") for f in FIELDS])
    widths = [max(len(line[i]) for line in table) for i in range(len(header))]
    for index, line in enumerate(table):
        print("  " + "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(line)))
        if index == 0:
            print("  " + "  ".join("-" * w for w in widths))


def reject_photo(room: str, image: str) -> str | None:
    """Move a deleted reading's photo and sidecar out of that room's processed/."""
    if not image:
        return None
    queues = config.paths_for(room)
    queues["rejected"].mkdir(parents=True, exist_ok=True)
    moved = []
    for suffix in (Path(image).suffix, ".json"):
        source = queues["processed"] / (Path(image).stem + suffix)
        if source.exists():
            shutil.move(str(source), str(queues["rejected"] / source.name))
            moved.append(source.name)
    return ", ".join(moved) or None


def wipe_room(room: str, confirmed: bool) -> int:
    """Remove one room entirely: its readings, its photos and its report.

    Unlike the range deletions, this does not keep the photos -- "all of it"
    means all of it -- so the preview spells out exactly what disappears and
    nothing happens without --yes.
    """
    queues = config.paths_for(room)
    photos = {name: sorted(path.glob("*")) for name, path in queues.items()
              if name != "base" and path.is_dir()}
    total_files = sum(len(items) for items in photos.values())
    total_bytes = sum(f.stat().st_size for items in photos.values()
                      for f in items if f.is_file())
    report = config.DATA / f"report-{room}.html"

    with store.connect() as conn:
        readings = store.count(conn, room)
        others = [r["room"] for r in store.rooms(conn) if r["room"] != room]

    if not readings and not total_files and not report.exists():
        print(f"nothing stored for room {room!r}")
        return 0

    print(f"wiping room {room!r} would remove:")
    print()
    print(f"  {readings:>6} reading(s) from the database")
    for name, items in photos.items():
        if items:
            print(f"  {len(items):>6} file(s) in captures/{room}/{name}/")
    print(f"  {total_bytes / 1048576:>6.1f} MB of photos in total")
    if report.exists():
        print(f"         {report.name}")
    print()
    print(f"  untouched: {', '.join(others) if others else 'no other rooms'}")

    if not confirmed:
        print()
        print("This was a preview -- nothing was deleted.")
        print("Add --yes to wipe this room. It cannot be undone.")
        return 0

    with store.connect() as conn:
        conn.execute("DELETE FROM readings WHERE room = ?", (room,))
        conn.commit()
        store.checkpoint(conn)
        left = store.count(conn)
    shutil.rmtree(queues["base"], ignore_errors=True)
    report.unlink(missing_ok=True)

    print()
    print(f"wiped room {room!r}: {readings} reading(s) and {total_files} file(s) removed")
    print(f"{left} reading(s) remain across {len(others)} other room(s)")
    print("run analyze.py --all-rooms to rebuild the reports")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    picker = parser.add_argument_group("what to delete (pick one)")
    parser.add_argument("--room", help="restrict to one room (default: all rooms)")
    picker.add_argument("--at", metavar="TIME", help="a single reading, e.g. '2026-08-25 16:31'")
    picker.add_argument("--from", dest="start", metavar="TIME", help="range start, local time")
    picker.add_argument("--to", dest="end", metavar="TIME", help="range end, local time")
    picker.add_argument("--night", metavar="DATE",
                        help="a whole night, by the evening it began (YYYY-MM-DD)")
    picker.add_argument("--incomplete", action="store_true",
                        help="every reading with a missing value")
    picker.add_argument("--everything", action="store_true",
                        help="wipe one room completely: readings, photos and report")
    parser.add_argument("--yes", action="store_true", help="actually delete (default: preview)")
    parser.add_argument("--keep-photos", action="store_true",
                        help="leave the photos in captures/processed/")
    args = parser.parse_args()

    if not (args.at or args.start or args.end or args.night
            or args.incomplete or args.everything):
        parser.print_help()
        return 1

    if args.everything and not args.room:
        print("--everything needs --room: say which room to wipe", file=sys.stderr)
        return 1

    if not config.DB_PATH.exists():
        print(f"no database at {config.DB_PATH}", file=sys.stderr)
        return 1

    if args.everything:
        return wipe_room(config.room_name(args.room), args.yes)

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

        keys = [(row["room"], row["captured_at"]) for row in rows]
        conn.executemany(
            "DELETE FROM readings WHERE room = ? AND captured_at = ?", keys)
        conn.commit()

        moved = 0
        if not args.keep_photos:
            for row in rows:
                if reject_photo(row["room"], row["image"]):
                    moved += 1

        store.checkpoint(conn)
        print(f"\ndeleted {len(rows)} reading(s); {store.count(conn)} remain")
        if moved:
            print(f"moved {moved} photo(s) into their room's rejected/ folder")
        print("run analyze.py to rebuild the report")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
