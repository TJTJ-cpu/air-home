"""Capture and read the monitor in one continuous loop.

Takes a photo, reads it with the model, stores the reading, waits, repeats --
so there is one command to run instead of three.

    python watch.py grandpa             # every 3 minutes
    python watch.py tj --interval 5m
    python watch.py mom --interval 1m --count 20

The room decides where photos queue and which room the readings are filed
under, so one machine can cover several rooms as the camera moves.

The photo is always written to disk BEFORE the model is asked to read it. If
LM Studio is down or slow, the capture still happened: the photo waits in
captures/pending/ and is picked up automatically on a later pass. A model
outage costs you readings you can catch up on later, never photos, which are
gone the moment they are not taken.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from contextlib import nullcontext
from datetime import datetime

import capture
import config
import process
import store
from extract import ExtractionError

# Leave this much of the interval free so a slow extraction never delays the
# next photo. One read takes ~11s; this is that plus camera warm-up and slack.
RESERVE_SECONDS = 25


def drain(conn, room: str, deadline: float, verbose: bool = True) -> tuple[int, int, bool]:
    """Read pending photos until the queue empties or time runs short.

    Normally there is exactly one waiting photo -- the one just taken. After an
    outage there is a backlog, and this works through it a few per cycle
    without ever pushing the next capture late.

    Returns (read, failed, model_down).
    """
    read = failed = 0
    pending = config.paths_for(room)["pending"]
    for image in process.pending_images(pending):
        if time.monotonic() > deadline - RESERVE_SECONDS:
            remaining = len(process.pending_images(pending))
            if verbose and remaining:
                print(f"    {remaining} photo(s) still queued, continuing next cycle", flush=True)
            break
        try:
            status, message = process.process_one(conn, room, image)
        except ExtractionError as exc:
            # The model is unreachable. Stop trying this cycle and leave every
            # photo where it is; the next cycle picks them all up.
            if verbose:
                print(f"    model unavailable: {exc}", file=sys.stderr)
            return read, failed, True

        if status in ("failed", "unreadable"):
            failed += 1
            if verbose:
                print(f"    {image.name}  {status.upper()}: {message}", flush=True)
        else:
            read += 1
            if verbose:
                print(f"    {message}", flush=True)
    return read, failed, False


def refresh_report(room: str) -> None:
    """Regenerate data/report.html so it is current whenever you open it.

    Run as a separate process rather than by importing analyze. A watch loop
    stays up for days; importing would freeze today's code in memory, so any
    later edit to the report silently would not appear until the loop was
    restarted. A subprocess always runs what is on disk. It costs about a
    second per reading, which is nothing against a multi-minute interval.
    """
    try:
        result = subprocess.run(
            [sys.executable, str(config.ROOT / "analyze.py"), "--room", room, "--quiet"],
            capture_output=True, text=True, timeout=180, cwd=config.ROOT,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip().splitlines()
            print(f"    report not updated: {detail[-1] if detail else 'unknown error'}",
                  file=sys.stderr)
    except Exception as exc:  # a report problem must never stop the loop
        print(f"    report not updated: {exc}", file=sys.stderr)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("room", help="which room the camera is watching, e.g. grandpa, tj")
    parser.add_argument("--interval", type=capture.parse_interval, default="3m",
                        help="time between photos: 30s, 1m, 3m, 5m. Default 3m")
    parser.add_argument("--count", type=int, default=0, help="stop after N photos (0 = forever)")
    parser.add_argument("--camera", type=int, default=config.CAMERA_INDEX)
    parser.add_argument("--warmup", type=int, default=config.CAMERA_WARMUP_FRAMES)
    parser.add_argument("--no-report", action="store_true",
                        help="skip regenerating data/report.html each cycle")
    args = parser.parse_args()

    try:
        room = config.room_name(args.room)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 1
    config.ensure_dirs(room)
    pending = config.paths_for(room)["pending"]
    limit = args.count if args.count > 0 else None

    known = config.rooms_on_disk()
    backlog = len(process.pending_images(pending))
    print(f"[{room}] watching every {args.interval:g}s -- Ctrl+C to stop", flush=True)
    if known and room not in known:
        print(f"new room '{room}' -- existing rooms: {', '.join(known)}", flush=True)
    if backlog:
        print(f"{backlog} photo(s) already queued; working through them as we go", flush=True)

    taken = read_total = failed_total = 0
    model_down = False
    started = time.monotonic()

    try:
        with store.connect() as conn:
            while limit is None or taken < limit:
                cycle_start = time.monotonic()
                deadline = cycle_start + args.interval

                try:
                    path = capture.capture_one(pending, args.camera, args.warmup)
                    taken += 1
                    stamp = datetime.now().strftime("%H:%M:%S")
                    print(f"[{taken}] {stamp}  {path.name}", flush=True)
                except Exception as exc:  # a camera glitch must not end the run
                    print(f"capture failed: {exc}", file=sys.stderr)

                read, failed, down = drain(conn, room, deadline)
                read_total += read
                failed_total += failed

                if down and not model_down:
                    print("    photos are still being captured and will be read "
                          "once LM Studio is back", file=sys.stderr)
                model_down = down

                if read:
                    # Keep readings.db itself current so a copy or a commit is
                    # never missing the last few hours.
                    store.checkpoint(conn)
                    if not args.no_report:
                        refresh_report(room)

                if limit is not None and taken >= limit:
                    break
                # Anchor to the start so the schedule cannot drift by however
                # long the capture and reading took.
                elapsed = time.monotonic() - started
                time.sleep(max(0.0, args.interval - (elapsed % args.interval)))
    except KeyboardInterrupt:
        print()

    print(f"\nstopped: {taken} photo(s) captured, {read_total} reading(s) stored, "
          f"{failed_total} unreadable")
    if leftover := len(process.pending_images(pending)):
        print(f"{leftover} photo(s) left in pending/ -- run process.py {room} to catch up")
    return 0


if __name__ == "__main__":
    # Asking for --help, or a dry run, does no work and must not be blocked by
    # a run that is already going.
    _no_work = {"-h", "--help"}.intersection(sys.argv)
    with (nullcontext() if _no_work else process.single_instance()):
        raise SystemExit(main())
