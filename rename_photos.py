"""Give photos already on disk the new, readable names.

    2026-09-13T15-04-26Z.jpg   ->   2026-09-13 22.04.26 UTC+7.jpg

New photos get these names on their own. This is for the ones taken before.

    python rename_photos.py            # show what would change, touch nothing
    python rename_photos.py --apply    # do it
    python rename_photos.py --undo data/backup/rename-20260913-221500.csv

Only the name changes. The moment each photo was taken is exactly the same --
every rename is checked to read back to the same instant before it happens --
and the readings themselves (their captured_at times) are never touched. What
does change, so everything keeps pointing at the right photo:

  * the photo and its .json sidecar are renamed together
  * the "image" field inside the sidecar
  * the "image" column in the database

The database is backed up first, and every rename is written to a log as it
happens, so a run can be undone even if it is interrupted halfway. It refuses
to run while watch.py or process.py is going, because they move these files.
"""
from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import config
import process

BACKUP = config.DATA / "backup"
QUEUES = ("pending", "processed", "failed", "rejected")


def new_stem(stem: str) -> str | None:
    """The readable name for an old-style stem, or None if it is not old-style."""
    match = config._OLD_NAME.match(stem)
    if not match:
        return None
    moment = config.photo_time(stem)
    if moment is None:
        return None
    renamed = config.photo_name(moment) + stem[match.end():]
    if config.photo_time(renamed) != moment:     # never let a rename move a photo in time
        raise AssertionError(f"{stem} would not read back as the same time")
    return renamed


def plan() -> tuple[list[tuple[Path, Path]], list[str]]:
    """Every (old, new) file rename, and anything that has to be skipped."""
    moves, skipped = [], []
    for room in config.rooms_on_disk():
        queues = config.paths_for(room)
        for queue in QUEUES:
            folder = queues[queue]
            if not folder.is_dir():
                continue
            for path in sorted(folder.iterdir()):
                if not path.is_file():
                    continue
                stem = new_stem(path.stem)
                if stem is None:
                    continue
                target = path.with_name(stem + path.suffix)
                if target.exists():
                    skipped.append(f"{path.relative_to(config.ROOT)}: "
                                   f"{target.name} already exists")
                    continue
                moves.append((path, target))
    return moves, skipped


def backup_database(stamp: str) -> Path | None:
    if not config.DB_PATH.exists():
        return None
    BACKUP.mkdir(parents=True, exist_ok=True)
    target = BACKUP / f"readings-prerename-{stamp}.db"
    source, copy = sqlite3.connect(config.DB_PATH), sqlite3.connect(target)
    try:
        source.backup(copy)
    finally:
        copy.close()
        source.close()
    return target


def fix_sidecar(json_path: Path, old_image: str, new_image: str) -> None:
    data = json.loads(json_path.read_text(encoding="utf-8"))
    if data.get("image") == old_image:
        data["image"] = new_image
        json_path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def update_database(pairs: dict[str, str]) -> int:
    """Point readings at their photos' new names. Returns rows changed."""
    if not config.DB_PATH.exists() or not pairs:
        return 0
    conn = sqlite3.connect(config.DB_PATH, timeout=30)
    try:
        with conn:                      # one transaction: all of them or none
            changed = sum(
                conn.execute("UPDATE readings SET image = ? WHERE image = ?",
                             (new, old)).rowcount
                for old, new in pairs.items())
    finally:
        conn.close()
    return changed


def database_renames() -> dict[str, str]:
    """old -> new for every old-style image name the database still holds.

    Worked out from the names alone, not from the files, so readings whose
    photo is gone are fixed too, and a second run finds nothing left to do.
    """
    if not config.DB_PATH.exists():
        return {}
    conn = sqlite3.connect(config.DB_PATH)
    try:
        names = [row[0] for row in conn.execute("SELECT DISTINCT image FROM readings")]
    finally:
        conn.close()
    pairs = {}
    for name in names:
        stem = new_stem(Path(name).stem)
        if stem is not None:
            pairs[name] = stem + Path(name).suffix
    return pairs


def apply(moves: list[tuple[Path, Path]]) -> int:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    saved = backup_database(stamp)
    if saved:
        print(f"database backed up to {saved.relative_to(config.ROOT)}")
    BACKUP.mkdir(parents=True, exist_ok=True)
    log_path = BACKUP / f"rename-{stamp}.csv"

    done = 0
    with open(log_path, "w", newline="", encoding="utf-8") as handle:
        log = csv.writer(handle)
        log.writerow(["kind", "old", "new"])
        for old, new in moves:
            old.rename(new)
            # Logged the moment it happens, so an interrupted run can still
            # be undone file by file.
            log.writerow(["file", str(old.relative_to(config.ROOT)),
                          str(new.relative_to(config.ROOT))])
            handle.flush()
            if new.suffix == ".json":
                for suffix in process.IMAGE_SUFFIXES:
                    fix_sidecar(new, old.stem + suffix, new.stem + suffix)
            done += 1

        # The database gets its own entries rather than being inferred from the
        # files: it also holds readings whose photo is long gone, and undo has
        # to put those back too. Logged before the update, so a crash between
        # the two leaves undo with nothing worse than a no-op.
        pairs = database_renames()
        for old_name, new_name in pairs.items():
            log.writerow(["db", old_name, new_name])
        handle.flush()

    rows = update_database(pairs)
    print(f"renamed {done} files, repointed {rows} readings")
    print(f"undo with:  python rename_photos.py --undo {log_path.relative_to(config.ROOT)}")
    return 0


def undo(log_file: Path) -> int:
    with open(log_file, newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    back = missing = 0
    pairs = {row["new"]: row["old"] for row in rows if row["kind"] == "db"}
    for row in reversed(rows):
        if row["kind"] != "file":
            continue
        old, new = config.ROOT / row["old"], config.ROOT / row["new"]
        if not new.exists():
            missing += 1
            continue
        if new.suffix == ".json":
            for suffix in process.IMAGE_SUFFIXES:
                fix_sidecar(new, new.stem + suffix, old.stem + suffix)
        new.rename(old)
        back += 1
    rows_back = update_database(pairs)
    print(f"put back {back} files, {rows_back} readings"
          + (f" ({missing} were already gone)" if missing else ""))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="actually rename")
    parser.add_argument("--undo", type=Path, metavar="LOG",
                        help="reverse a previous run, using its log")
    args = parser.parse_args()

    if args.undo:
        with process.single_instance():
            return undo(args.undo)

    moves, skipped = plan()
    photos = [m for m in moves if m[0].suffix.lower() in process.IMAGE_SUFFIXES]
    sign = "+" if config.LOCAL_UTC_OFFSET >= 0 else ""
    print(f"your clock: UTC{sign}{config.LOCAL_UTC_OFFSET:g} (LOCAL_UTC_OFFSET in .env)\n")
    sample = photos[:3] + photos[-2:] if len(photos) > 5 else photos
    for old, new in sample:
        print(f"  {old.parent.parent.name}/{old.parent.name}/{old.name}  ->  {new.name}")
    if len(photos) > 5:
        print("  ...")

    by_room: dict[str, int] = {}
    for old, _ in photos:
        room = old.parent.parent.name
        by_room[room] = by_room.get(room, 0) + 1
    print(f"\n{len(photos)} photos, {len(moves)} files counting sidecars"
          + (": " + ", ".join(f"{r} {n}" for r, n in sorted(by_room.items())) if by_room else ""))
    print(f"{len(database_renames())} readings in the database to repoint")
    for line in skipped:
        print(f"  skip {line}")

    if not moves:
        print("nothing to rename")
        return 0
    if not args.apply:
        print("\ndry run -- nothing changed. Add --apply to do it.")
        return 0
    with process.single_instance():
        return apply(moves)


if __name__ == "__main__":
    sys.exit(main())
