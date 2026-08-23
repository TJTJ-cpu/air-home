"""Extract readings from every not-yet-processed photo into the local database.

Photos live in captures/pending/. A photo that has been read successfully moves
to captures/processed/, and one the model could not read moves to
captures/failed/ -- so "what is left in pending" is always the work queue, and
nothing is ever read twice.

    python process.py
    python process.py --limit 20
    python process.py --retry-failed
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
            f"another process.py is already running (pid {holder}).\n"
            "Wait for it to finish, or stop it, then re-run.",
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
    parser.add_argument("--source", type=Path, default=None, help="folder of photos to read")
    parser.add_argument("--limit", type=int, default=0, help="process at most N photos")
    parser.add_argument("--retry-failed", action="store_true",
                        help="re-read captures/failed/ instead of captures/pending/")
    parser.add_argument("--dry-run", action="store_true",
                        help="print readings without saving or moving anything")
    args = parser.parse_args()

    config.ensure_dirs()
    source = args.source or (config.FAILED if args.retry_failed else config.PENDING)
    if not source.is_dir():
        print(f"no such folder: {source}", file=sys.stderr)
        return 1

    images = pending_images(source)
    if args.limit > 0:
        images = images[: args.limit]
    if not images:
        print(f"nothing to process in {source}")
        return 0

    print(f"processing {len(images)} photo(s) from {source}")
    saved = skipped = failed = 0
    consecutive_failures = 0

    # A dry run never opens the database -- connecting would create the file.
    with (nullcontext(None) if args.dry_run else store.connect()) as conn:
        for image in images:
            stamp = captured_at(image)
            try:
                reading, notes = extract(image)
            except ExtractionError as exc:
                if exc.transient:
                    # The server is the problem, not this photo. Leave it and
                    # every photo after it exactly where they are.
                    print(f"  {image.name}  {exc}", file=sys.stderr)
                    print(
                        "\naborting -- no further photos were touched. "
                        "Start LM Studio, then re-run.",
                        file=sys.stderr,
                    )
                    return 1
                consecutive_failures += 1
                failed += 1
                print(f"  {image.name}  FAILED: {exc}")
                if not args.dry_run:
                    _move(image, config.FAILED, {"captured_at": stamp, "error": str(exc)})
                if _too_many_failures(consecutive_failures):
                    return 1
                continue

            sidecar = {
                "captured_at": stamp,
                "image": image.name,
                "model": config.LMSTUDIO_MODEL,
                "reading": reading,
                "notes": notes,
            }

            if all(value is None for value in reading.values()):
                consecutive_failures += 1
                failed += 1
                print(f"  {image.name}  UNREADABLE: no values recognised")
                if not args.dry_run:
                    _move(image, config.FAILED, sidecar)
                if _too_many_failures(consecutive_failures):
                    return 1
                continue

            consecutive_failures = 0
            note_text = f"   ({'; '.join(notes)})" if notes else ""
            print(f"  {image.name}  {_summarise(reading)}{note_text}")

            if args.dry_run:
                continue

            if store.save(conn, stamp, image.name, reading, notes):
                saved += 1
            else:
                skipped += 1  # this timestamp was already recorded
            # Commit before the photo leaves the queue. If this run dies here,
            # the photo is still in pending/ and gets re-read -- never the
            # other way round, which would lose the reading for good.
            conn.commit()
            _move(image, config.PROCESSED, sidecar)

        if args.dry_run:
            print(f"\nread {len(images)}, failed {failed} -- dry run, nothing written")
        else:
            print(f"\nsaved {saved}, already-known {skipped}, failed {failed}")
            print(f"{store.count(conn)} reading(s) in {config.DB_PATH}")

    return 0


if __name__ == "__main__":
    args_need_lock = "--dry-run" not in sys.argv
    with (single_instance() if args_need_lock else nullcontext()):
        raise SystemExit(main())
