"""Capture and read the monitor in one continuous loop.

Takes a photo, reads it with the model, stores the reading, waits, repeats --
so there is one command to run instead of three.

    python watch.py grandpa             # every 3 minutes
    python watch.py tj --interval 5m
    python watch.py mom --interval 1m --count 20

The room decides where photos queue and which room the readings are filed
under, so one machine can cover several rooms as the camera moves.

The gap between photos is not fixed. Three rules apply and the fastest wins:
the interval you asked for; every two minutes while CO2 simply sits high, which
is a level rather than a change and holds for as long as it stays up; and every
minute for ten minutes after a reading jumps, which is how a fan switching on or
a door opening gets captured as a shape instead of one step. None of them ever
slows a fast interval down. Use --no-adaptive to keep the gap fixed.

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
from datetime import datetime, timezone

from pathlib import Path

import capture
import config
import process
import store
from extract import ExtractionError

# Leave this much of the interval free so a slow extraction never delays the
# next photo. One read takes ~11s; this is that plus camera warm-up and slack.
RESERVE_SECONDS = 25


def _read(conn, room: str, image: Path, announce: bool) -> tuple[str, str]:
    """Read one photo. Returns (outcome, message); outcome "down" means the
    model is unreachable and nothing further should be attempted this cycle."""
    try:
        status, message = process.process_one(conn, room, image)
    except ExtractionError as exc:
        print(f"    model unavailable: {exc}", file=sys.stderr)
        return "down", str(exc)
    if announce:
        if status in ("failed", "unreadable"):
            print(f"    {image.name}  {status.upper()}: {message}", flush=True)
        else:
            print(f"    {message}", flush=True)
    return status, message


def read_live(conn, room: str, image: Path | None) -> tuple[bool, bool]:
    """Read the photo just taken, before anything else.

    It is the only one describing the room right now: it decides whether to
    speed sampling up, and it is what the report shows as current. Behind a
    backlog of fifty photos, reading in filename order would answer both
    questions with a picture from hours ago -- by which time a fan switching
    on is long over.

    Returns (stored_something, model_down).
    """
    if image is None or not image.exists():
        return False, False
    status, _ = _read(conn, room, image, announce=True)
    if status == "down":
        return False, True
    return status not in ("failed", "unreadable"), False


def drain_backlog(conn, room: str, deadline: float, skip: Path | None = None) -> bool:
    """Work through older photos, oldest first, until the cycle runs short.

    History fills in forwards, and never at the cost of a late capture: the
    deadline is whatever time is left in the current interval, which shrinks
    when sampling speeds up.

    Returns True if the model went away.
    """
    pending = config.paths_for(room)["pending"]
    done = 0
    for image in process.pending_images(pending):
        if image == skip:
            continue
        if time.monotonic() > deadline:
            break
        status, _ = _read(conn, room, image, announce=False)
        if status == "down":
            return True
        done += 1

    if done:
        left = len([p for p in process.pending_images(pending) if p != skip])
        tail = f", {left} still queued" if left else ", queue clear"
        print(f"    caught up on {done} older photo(s){tail}", flush=True)
    return False


def latest_co2(conn, room: str, interval: float) -> float | None:
    """The most recent CO2 reading, if it is recent enough to still describe now.

    Skips over readings whose CO2 could not be read, so one unreadable photo
    does not look like the room suddenly clearing. A value older than a few
    intervals is ignored: after an outage it describes a room that has since
    moved on.
    """
    row = conn.execute(
        "SELECT captured_at, co2_ppm FROM readings "
        "WHERE room = ? AND co2_ppm IS NOT NULL ORDER BY captured_at DESC LIMIT 1",
        (room,)).fetchone()
    if row is None:
        return None
    age = (datetime.now(timezone.utc)
           - datetime.fromisoformat(row["captured_at"])).total_seconds()
    if age > max(interval, 60) * 4:
        return None
    return float(row["co2_ppm"])


def sampling_gap(conn, room: str, args, fast_until: float,
                 jump_reason: str) -> tuple[float, str]:
    """How long to wait before the next photo, and why.

    Three states, fastest wins: normal, CO2 sitting high, and something
    actively changing. Both of the quicker ones are capped by whatever
    interval was asked for -- this never slows a fast interval down.
    """
    gap, why = args.interval, "normal"
    if args.no_adaptive:
        return gap, why

    level = latest_co2(conn, room, args.interval)
    if (level is not None and level > config.HIGH_CO2_PPM
            and config.HIGH_INTERVAL_SECONDS < gap):
        gap = config.HIGH_INTERVAL_SECONDS
        why = f"co2 {level:.0f}, above {config.HIGH_CO2_PPM}"

    if time.monotonic() < fast_until and config.FAST_INTERVAL_SECONDS < gap:
        gap = config.FAST_INTERVAL_SECONDS
        why = jump_reason or "still changing"

    return gap, why


def recent_jump(conn, room: str, interval: float) -> str | None:
    """Did the newest reading move sharply from the one before it?

    Compares the last two stored readings rather than tracking state in the
    loop, so a backlog being worked through is judged on the same basis as a
    live capture. Readings far apart in time are ignored: a gap across an
    outage is not a change, it is a gap.
    """
    rows = conn.execute(
        "SELECT * FROM readings WHERE room = ? ORDER BY captured_at DESC LIMIT 2",
        (room,)).fetchall()
    if len(rows) < 2:
        return None
    newest, prior = rows
    apart = (datetime.fromisoformat(newest["captured_at"])
             - datetime.fromisoformat(prior["captured_at"])).total_seconds()
    if apart <= 0 or apart > max(interval, 60) * 2.5:
        return None

    for field, threshold in config.JUMP_THRESHOLDS.items():
        now, before = newest[field], prior[field]
        if now is None or before is None:
            continue
        move = float(now) - float(before)
        if abs(move) >= threshold:
            return f"{field.replace('_ppm', '').replace('_c', '').replace('_pct', '')} " \
                   f"{move:+.0f}"
    return None


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
    parser.add_argument("--no-adaptive", action="store_true",
                        help="keep the interval fixed: never speed up for a "
                             "jump or for high CO2")
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
    fast_until = 0.0          # monotonic deadline; 0 means sampling normally
    jump_reason = ""
    last_gap = None
    started = time.monotonic()
    if not args.no_adaptive:
        print(f"sampling drops to {config.HIGH_INTERVAL_SECONDS}s above "
              f"{config.HIGH_CO2_PPM} ppm, and to "
              f"{config.FAST_INTERVAL_SECONDS}s while readings are moving", flush=True)

    try:
        with store.connect() as conn:
            while limit is None or taken < limit:
                cycle_start = time.monotonic()

                path = None
                try:
                    path = capture.capture_one(pending, args.camera, args.warmup)
                    taken += 1
                    stamp = datetime.now().strftime("%H:%M:%S")
                    print(f"[{taken}] {stamp}  {path.name}", flush=True)
                except Exception as exc:  # a camera glitch must not end the run
                    print(f"capture failed: {exc}", file=sys.stderr)

                stored, down = read_live(conn, room, path)
                if stored:
                    read_total += 1
                    # Keep readings.db itself current so a copy or a commit is
                    # never missing the last few hours.
                    store.checkpoint(conn)
                    if not args.no_adaptive:
                        moved = recent_jump(conn, room, args.interval)
                        if moved:
                            fast_until = time.monotonic() + config.FAST_WINDOW_SECONDS
                            jump_reason = moved
                elif path is not None and not down:
                    failed_total += 1

                # Decided now, after the live reading, so a change just seen
                # shortens this cycle rather than the next one -- and the
                # backlog only gets whatever time is left inside it.
                gap, why = sampling_gap(conn, room, args, fast_until, jump_reason)
                if time.monotonic() >= fast_until:
                    fast_until, jump_reason = 0.0, ""
                if gap != last_gap:
                    if last_gap is not None:
                        print(f"    now every {gap:g}s -- {why}", flush=True)
                    last_gap = gap

                if not down:
                    down = drain_backlog(conn, room,
                                         cycle_start + gap - RESERVE_SECONDS, skip=path)

                if down and not model_down:
                    print("    photos are still being captured and will be read "
                          "once LM Studio is back", file=sys.stderr)
                model_down = down

                if stored and not args.no_report:
                    refresh_report(room)

                if limit is not None and taken >= limit:
                    break
                time.sleep(max(0.0, cycle_start + gap - time.monotonic()))
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
