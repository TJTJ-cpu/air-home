"""Extract readings from every not-yet-processed photo into the local database.

Photos live in captures/pending/. A photo that has been read successfully moves
to captures/processed/, and one the model could not read moves to
captures/failed/ -- so "what is left in pending" is always the work queue, and
nothing is ever read twice.

    python process.py              # every room with photos waiting
    python process.py grandpa      # just one room
    python process.py tj --retry-failed

`watch.py` runs the capture and this step together in one loop; this script
stays useful for draining a backlog or re-reading failures.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from contextlib import contextmanager, nullcontext
from datetime import datetime, timezone
from pathlib import Path

import config
import store
from extract import ExtractionError, extract

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png"}
FILENAME_FORMAT = "%Y-%m-%dT%H-%M-%SZ"
# Consecutive unreadable photos that suggest something systemic -- the camera
# knocked out of frame, the room light left off -- rather than one bad shot.
ABORT_AFTER = 5
LOCK_PATH = config.DATA / "process.lock"


def _lock(handle) -> None:
    """Take an exclusive advisory lock, or raise OSError if someone holds it.

    The OS drops the lock when the process exits, however it exits, so there
    is no stale lock file to clean up by hand.
    """
    handle.seek(0)
    if sys.platform == "win32":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(handle) -> None:
    try:
        handle.seek(0)
        if sys.platform == "win32":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle, fcntl.LOCK_UN)
    except OSError:
        pass


@contextmanager
def single_instance():
    """Refuse to start if another extraction is already running.

    Two concurrent runs race for the same photos: they pay the model twice for
    each one, collide moving files, and block each other on the database.
    """
    config.DATA.mkdir(parents=True, exist_ok=True)
    handle = open(LOCK_PATH, "a+", encoding="utf-8")
    try:
        _lock(handle)
    except OSError:
        # The pid is written from byte 1 on, because byte 0 is the locked
        # region and reading it back would be denied too.
        holder = "unknown"
        try:
            handle.seek(1)
            holder = handle.read().strip() or holder
        except OSError:
            pass
        handle.close()
        print(
            f"another run is already going (pid {holder}).\n"
            "Wait for it to finish, or stop it, then try again.",
            file=sys.stderr,
        )
        raise SystemExit(1)
    try:
        handle.seek(1)
        handle.truncate()
        handle.write(str(os.getpid()))
        handle.flush()
        yield
    finally:
        _unlock(handle)
        handle.close()


def captured_at(path: Path) -> str:
    """UTC timestamp for a photo, from its filename or else its mtime."""
    try:
        stamp = datetime.strptime(path.stem, FILENAME_FORMAT).replace(tzinfo=timezone.utc)
    except ValueError:
        stamp = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    return stamp.isoformat(timespec="seconds")


def pending_images(source: Path) -> list[Path]:
    return sorted(
        path for path in source.iterdir()
        if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES
    )


def _move(path: Path, destination: Path, sidecar: dict) -> Path:
    destination.mkdir(parents=True, exist_ok=True)
    target = destination / path.name
    if target.exists():  # same name already there: keep both, never overwrite
        target = destination / f"{path.stem}-{int(datetime.now().timestamp())}{path.suffix}"
    # Sidecar first: if the move fails, the reading is still on disk.
    target.with_suffix(".json").write_text(json.dumps(sidecar, indent=2), encoding="utf-8")
    shutil.move(str(path), str(target))
    return target


def _summarise(reading: dict) -> str:
    parts = [
        f"{field}={reading[field]:g}" if isinstance(reading[field], (int, float))
        else f"{field}=?"
        for field in config.FIELDS
    ]
    return " ".join(parts)


def process_one(conn, room: str, image: Path, dry_run: bool = False) -> tuple[str, str]:
    """Read one photo, store the reading, file the photo away.

    Returns (status, message) where status is saved / duplicate / unreadable /
    failed / dry. A transient ExtractionError is re-raised rather than handled,
    so the caller decides what to do about a server that is down -- the photo
    stays exactly where it is, because there is nothing wrong with the photo.
    """
    stamp = captured_at(image)
    queues = config.paths_for(room)
    try:
        reading, notes = extract(image)
    except ExtractionError as exc:
        if exc.transient:
            raise
        if not dry_run:
            _move(image, queues["failed"], {"room": room, "captured_at": stamp,
                                            "error": str(exc)})
        return "failed", str(exc)

    sidecar = {
        "room": room,
        "captured_at": stamp,
        "image": image.name,
        "model": config.LMSTUDIO_MODEL,
        "reading": reading,
        "notes": notes,
    }

    if all(value is None for value in reading.values()):
        if not dry_run:
            _move(image, queues["failed"], sidecar)
        return "unreadable", "no values recognised"

    message = _summarise(reading) + (f"   ({'; '.join(notes)})" if notes else "")
    if dry_run:
        return "dry", message

    saved = store.save(conn, room, stamp, image.name, reading, notes)
    # Commit before the photo leaves the queue. If this dies here, the photo is
    # still in pending/ and gets re-read -- never the other way round, which
    # would lose the reading for good.
    conn.commit()
    _move(image, queues["processed"], sidecar)
    return ("saved" if saved else "duplicate"), message


def _too_many_failures(count: int) -> bool:
    if count < ABORT_AFTER:
        return False
    print(
        f"\nstopping: {ABORT_AFTER} unreadable photos in a row. "
        "Check the camera framing before running again.",
        file=sys.stderr,
    )
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("room", nargs="?", help="which room to read (default: all of them)")
    parser.add_argument("--source", type=Path, default=None, help="folder of photos to read")
    parser.add_argument("--limit", type=int, default=0, help="process at most N photos")
    parser.add_argument("--retry-failed", action="store_true",
                        help="re-read captures/failed/ instead of captures/pending/")
    parser.add_argument("--dry-run", action="store_true",
                        help="print readings without saving or moving anything")
    args = parser.parse_args()

    config.ensure_dirs()
    try:
        rooms = [config.room_name(args.room)] if args.room else config.rooms_on_disk()
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 1
    if not rooms:
        print("no rooms yet -- run capture.py <room> or watch.py <room> first")
        return 0

    queue = "failed" if args.retry_failed else "pending"
    work = []
    for room in rooms:
        config.ensure_dirs(room)
        source = args.source or config.paths_for(room)[queue]
        if source.is_dir():
            work.extend((room, image) for image in pending_images(source))
    if args.limit > 0:
        work = work[: args.limit]
    if not work:
        where = ", ".join(rooms)
        print(f"nothing to process in {queue}/ for: {where}")
        return 0

    by_room = {}
    for room, _ in work:
        by_room[room] = by_room.get(room, 0) + 1
    print("processing " + ", ".join(f"{n} photo(s) from {r}" for r, n in by_room.items()))
    saved = skipped = failed = 0
    consecutive_failures = 0

    # A dry run never opens the database -- connecting would create the file.
    with (nullcontext(None) if args.dry_run else store.connect()) as conn:
        for room, image in work:
            try:
                status, message = process_one(conn, room, image, args.dry_run)
            except ExtractionError as exc:
                print(f"  {image.name}  {exc}", file=sys.stderr)
                print("\naborting -- no further photos were touched. "
                      "Start LM Studio, then re-run.", file=sys.stderr)
                return 1

            if status in ("failed", "unreadable"):
                consecutive_failures += 1
                failed += 1
                print(f"  [{room}] {image.name}  {status.upper()}: {message}")
                if _too_many_failures(consecutive_failures):
                    return 1
                continue

            consecutive_failures = 0
            if status == "saved":
                saved += 1
            elif status == "duplicate":
                skipped += 1
            print(f"  [{room}] {image.name}  {message}")

        if args.dry_run:
            print(f"\nread {len(images)}, failed {failed} -- dry run, nothing written")
        else:
            print(f"\nsaved {saved}, already-known {skipped}, failed {failed}")
            print(f"{store.count(conn)} reading(s) in {config.DB_PATH}")

    return 0


if __name__ == "__main__":
    # Asking for --help, or a dry run, does no work and must not be blocked by
    # a run that is already going.
    _no_work = {"-h", "--help"}.intersection(sys.argv) or "--dry-run" in sys.argv
    with (nullcontext() if _no_work else single_instance()):
        raise SystemExit(main())
