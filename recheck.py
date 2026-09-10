"""Re-read stored photos for readings that look wrong, and fix them.

Every processed photo is kept, so a reading the model fumbled can be read
again later -- after the plausible ranges were tightened, or the prompt was
improved -- without waiting for the moment to come round again.

A reading is suspect when any of its values is missing, or when a stored value
would fail today's plausibility check. Tightening a range in .env therefore
turns old bad readings into work this script can pick up.

    python recheck.py                    # preview, every room
    python recheck.py --room tj --yes    # re-read and update
    python recheck.py --at "2026-09-10 19:32" --yes
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import config
import store
from extract import ExtractionError, extract

LOCAL_TZ = timezone(timedelta(hours=config.LOCAL_UTC_OFFSET))
FIELDS = list(config.FIELDS)


def _local(captured_at: str) -> datetime:
    return datetime.fromisoformat(captured_at).astimezone(LOCAL_TZ)


def why_suspect(row) -> list[str]:
    """Reasons this stored reading looks wrong, judged by today's ranges."""
    reasons = []
    for field in FIELDS:
        value = row[field]
        if value is None:
            reasons.append(f"{field} missing")
            continue
        low, high = config.FIELD_RANGES[field]
        if not low <= float(value) <= high:
            reasons.append(f"{field}={value:g} outside {low:g}..{high:g}")
    return reasons


def find(conn, room: str | None, at: str | None) -> list:
    if room:
        rows = conn.execute("SELECT * FROM readings WHERE room = ? ORDER BY captured_at",
                            (config.room_name(room),)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM readings ORDER BY captured_at").fetchall()

    if at:
        target = datetime.strptime(at, "%Y-%m-%d %H:%M").replace(tzinfo=LOCAL_TZ)
        near = [(abs((_local(r["captured_at"]) - target).total_seconds()), r) for r in rows]
        near = [(gap, r) for gap, r in near if gap <= 120]
        return [min(near, key=lambda pair: pair[0])[1]] if near else []

    return [row for row in rows if why_suspect(row)]


def photo_for(room: str, image: str) -> Path | None:
    """The stored photo behind a reading, wherever it was filed."""
    if not image:
        return None
    queues = config.paths_for(room)
    for where in ("processed", "failed", "rejected", "pending"):
        candidate = queues[where] / image
        if candidate.exists():
            return candidate
    return None


def apply(conn, row, reading: dict, notes: list[str]) -> list[str]:
    """Write improved values back. Returns a description of what changed."""
    changes = []
    updates = {}
    for field in FIELDS:
        before, after = row[field], reading.get(field)
        if before is not None and after is None:
            # The re-read could not produce a plausible value either. If what
            # is stored is itself implausible, clear it: a number outside its
            # range is a misread, and a gap is more honest than a wrong value.
            low, high = config.FIELD_RANGES[field]
            if not low <= float(before) <= high:
                updates[field] = None
                changes.append(f"{field} {before:g} -> cleared (still unreadable)")
            continue
        if after is None or (before is not None and float(before) == float(after)):
            continue
        updates[field] = after
        was = "missing" if before is None else f"{before:g}"
        changes.append(f"{field} {was} -> {after:g}")
    if not updates:
        return []
    sets = ", ".join(f"{field} = :{field}" for field in updates)
    updates.update(room=row["room"], captured_at=row["captured_at"],
                   notes="; ".join(notes) or None)
    conn.execute(f"UPDATE readings SET {sets}, notes = :notes "
                 "WHERE room = :room AND captured_at = :captured_at", updates)
    return changes


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--room", help="only this room (default: all of them)")
    parser.add_argument("--at", metavar="TIME",
                        help="one reading, e.g. '2026-09-10 19:32'")
    parser.add_argument("--limit", type=int, default=0, help="stop after N readings")
    parser.add_argument("--yes", action="store_true",
                        help="actually re-read and update (default: preview)")
    args = parser.parse_args()

    if not config.DB_PATH.exists():
        print(f"no database at {config.DB_PATH}", file=sys.stderr)
        return 1

    with store.connect() as conn:
        rows = find(conn, args.room, args.at)
        if args.limit > 0:
            rows = rows[: args.limit]
        if not rows:
            print("nothing looks wrong -- every reading passes today's ranges")
            return 0

        print(f"{len(rows)} reading(s) look wrong:\n")
        missing_photo = 0
        for row in rows[:40]:
            photo = photo_for(row["room"], row["image"])
            mark = "" if photo else "   (photo gone, cannot re-read)"
            if not photo:
                missing_photo += 1
            print(f"  [{row['room']}] {_local(row['captured_at']):%Y-%m-%d %H:%M}  "
                  f"{'; '.join(why_suspect(row))}{mark}")
        if len(rows) > 40:
            print(f"  ... and {len(rows) - 40} more")

        if not args.yes:
            print(f"\nThis was a preview -- nothing was changed."
                  f"\nAdd --yes to ask the model to read these {len(rows)} photo(s) again.")
            return 0

        fixed = unchanged = skipped = 0
        for row in rows:
            when = f"[{row['room']}] {_local(row['captured_at']):%Y-%m-%d %H:%M}"
            photo = photo_for(row["room"], row["image"])
            if not photo:
                skipped += 1
                print(f"  {when}  SKIP: photo no longer on disk")
                continue
            try:
                reading, notes = extract(photo)
            except ExtractionError as exc:
                if exc.transient:
                    print(f"  {when}  {exc}", file=sys.stderr)
                    print("\naborting -- nothing further was changed. "
                          "Start LM Studio, then re-run.", file=sys.stderr)
                    return 1
                skipped += 1
                print(f"  {when}  SKIP: {exc}")
                continue

            changes = apply(conn, row, reading, notes)
            if changes:
                conn.commit()
                fixed += 1
                print(f"  {when}  {', '.join(changes)}")
                sidecar = photo.with_suffix(".json")
                if sidecar.exists():
                    try:
                        data = json.loads(sidecar.read_text(encoding="utf-8"))
                        data["reading"] = reading
                        data["notes"] = notes
                        data["rechecked_at"] = datetime.now(timezone.utc).isoformat(
                            timespec="seconds")
                        sidecar.write_text(json.dumps(data, indent=2), encoding="utf-8")
                    except (json.JSONDecodeError, OSError):
                        pass  # the database is what matters; the sidecar is a copy
            else:
                unchanged += 1
                print(f"  {when}  no better on a second look")

        store.checkpoint(conn)
        print(f"\nfixed {fixed}, unchanged {unchanged}, skipped {skipped}")
        if fixed:
            print("run analyze.py --all-rooms to rebuild the reports")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
