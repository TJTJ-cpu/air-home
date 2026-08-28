"""Rebuild the database from the .json sidecars in captures/processed/.

Every processed photo is written alongside a sidecar holding exactly what the
model returned, so the sidecars are a complete record independent of the
database. If the database is lost, corrupted, or a run died before committing,
this replays them -- no model calls, no cost, nothing re-read.

    python rebuild.py --dry-run
    python rebuild.py
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import config
import store


def load_sidecars(folder: Path, room: str) -> tuple[list[dict], list[str]]:
    """Return (records, problems) parsed from every *.json in the folder."""
    records: list[dict] = []
    problems: list[str] = []
    for path in sorted(folder.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            records.append(
                {
                    "room": data.get("room") or room,
                    "captured_at": data["captured_at"],
                    "image": data.get("image", f"{path.stem}.jpg"),
                    "reading": data["reading"],
                    "notes": data.get("notes") or [],
                }
            )
        except (json.JSONDecodeError, KeyError, OSError) as exc:
            problems.append(f"{path.name}: {exc}")
    return records, problems


def existing_keys() -> set[tuple]:
    if not config.DB_PATH.exists():
        return set()
    with store.connect() as conn:
        return {(row["room"], row["captured_at"])
                for row in conn.execute("SELECT room, captured_at FROM readings")}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--room", help="restrict to one room (default: all of them)")
    parser.add_argument("--source", type=Path, default=None,
                        help="a specific folder of sidecars")
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would be restored, write nothing")
    args = parser.parse_args()

    try:
        rooms = [config.room_name(args.room)] if args.room else config.rooms_on_disk()
    except ValueError as exc:
        print(exc)
        return 1
    records, problems = [], []
    for room in rooms:
        folder = args.source or config.paths_for(room)["processed"]
        if folder.is_dir():
            got, bad = load_sidecars(folder, room)
            records.extend(got)
            problems.extend(bad)
    if not records and not problems:
        print(f"no sidecars found for: {', '.join(rooms) or 'no rooms'}")
        return 0

    print(f"read {len(records)} sidecar(s) across {len(rooms)} room(s)")
    for problem in problems:
        print(f"  unusable: {problem}")

    if args.dry_run:
        known = existing_keys()
        missing = [r for r in records
                   if (r["room"], r["captured_at"]) not in known]
        print(f"\nwould restore {len(missing)}, already present {len(records) - len(missing)}"
              f", unusable {len(problems)}")
        if missing:
            span = f'{missing[0]["captured_at"]} .. {missing[-1]["captured_at"]}'
            print(f"missing span: {span}")
        return 0

    restored = present = 0
    with store.connect() as conn:
        for record in records:
            if store.save(conn, record["room"], record["captured_at"],
                          record["image"], record["reading"], record["notes"]):
                restored += 1
            else:
                present += 1
        conn.commit()
        print(f"\nrestored {restored}, already present {present}, unusable {len(problems)}")
        print(f"{store.count(conn)} reading(s) in {config.DB_PATH}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
