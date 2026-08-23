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


def load_sidecars(folder: Path) -> tuple[list[dict], list[str]]:
    """Return (records, problems) parsed from every *.json in the folder."""
    records: list[dict] = []
    problems: list[str] = []
    for path in sorted(folder.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            records.append(
                {
                    "captured_at": data["captured_at"],
                    "image": data.get("image", f"{path.stem}.jpg"),
                    "reading": data["reading"],
                    "notes": data.get("notes") or [],
                }
            )
        except (json.JSONDecodeError, KeyError, OSError) as exc:
            problems.append(f"{path.name}: {exc}")
    return records, problems


def existing_timestamps() -> set[str]:
    if not config.DB_PATH.exists():
        return set()
    with store.connect() as conn:
        return {row["captured_at"] for row in conn.execute("SELECT captured_at FROM readings")}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=config.PROCESSED,
                        help="folder of sidecars (default captures/processed/)")
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would be restored, write nothing")
    args = parser.parse_args()

    records, problems = load_sidecars(args.source)
    if not records and not problems:
        print(f"no sidecars found in {args.source}")
        return 0

    print(f"read {len(records)} sidecar(s) from {args.source}")
    for problem in problems:
        print(f"  unusable: {problem}")

    if args.dry_run:
        known = existing_timestamps()
        missing = [r for r in records if r["captured_at"] not in known]
        print(f"\nwould restore {len(missing)}, already present {len(records) - len(missing)}"
              f", unusable {len(problems)}")
        if missing:
            span = f'{missing[0]["captured_at"]} .. {missing[-1]["captured_at"]}'
            print(f"missing span: {span}")
        return 0

    restored = present = 0
    with store.connect() as conn:
        for record in records:
            if store.save(conn, record["captured_at"], record["image"],
                          record["reading"], record["notes"]):
                restored += 1
            else:
                present += 1
        conn.commit()
        print(f"\nrestored {restored}, already present {present}, unusable {len(problems)}")
        print(f"{store.count(conn)} reading(s) in {config.DB_PATH}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
