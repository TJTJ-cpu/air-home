"""Take a timestamped photo of the air monitor on a fixed interval.

The filename is the capture time in UTC and is the authoritative timestamp for
the reading -- the clock shown on the device itself is never trusted.

    python capture.py grandpa            # every 3 minutes
    python capture.py tj --once         # one shot, for checking framing

The room decides which queue the photos land in: captures/<room>/pending/.
"""
from __future__ import annotations

import argparse
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import cv2

import config

_INTERVAL = re.compile(r"^(\d+(?:\.\d+)?)([smh]?)$", re.IGNORECASE)
_UNITS = {"s": 1, "m": 60, "h": 3600, "": 60}


def parse_interval(text: str) -> float:
    """'30s' -> 30.0, '5m' -> 300.0, '1h' -> 3600.0, bare number -> minutes."""
    match = _INTERVAL.match(text.strip())
    if not match:
        raise argparse.ArgumentTypeError(
            f"bad interval {text!r}; use forms like 30s, 1m, 5m, 1h"
        )
    seconds = float(match.group(1)) * _UNITS[match.group(2).lower()]
    if seconds < 1:
        raise argparse.ArgumentTypeError("interval must be at least 1 second")
    return seconds


def _open_camera(index: int) -> cv2.VideoCapture:
    # CAP_DSHOW avoids the multi-second MSMF startup stall on Windows.
    backend = cv2.CAP_DSHOW if sys.platform == "win32" else cv2.CAP_ANY
    cam = cv2.VideoCapture(index, backend)
    if not cam.isOpened():
        cam.release()
        raise RuntimeError(
            f"could not open camera {index}; try a different --camera index, "
            "and close any app already using the webcam"
        )
    if config.CAMERA_WIDTH:
        cam.set(cv2.CAP_PROP_FRAME_WIDTH, config.CAMERA_WIDTH)
    if config.CAMERA_HEIGHT:
        cam.set(cv2.CAP_PROP_FRAME_HEIGHT, config.CAMERA_HEIGHT)
    return cam


def grab_frame(index: int, warmup: int):
    """Open the camera, discard warm-up frames, return the next good frame.

    The first frames off a webcam are exposed and focused for whatever the
    sensor last saw, so they come back dark or blurry. Throwing them away is
    the difference between a readable LCD and a smear.
    """
    cam = _open_camera(index)
    try:
        for _ in range(max(0, warmup)):
            cam.read()
        ok, frame = cam.read()
        if not ok or frame is None:
            raise RuntimeError("camera opened but returned no frame")
        return frame
    finally:
        cam.release()


def capture_one(outdir: Path, index: int, warmup: int) -> Path:
    frame = grab_frame(index, warmup)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
    path = outdir / f"{stamp}.jpg"
    if not cv2.imwrite(str(path), frame, [cv2.IMWRITE_JPEG_QUALITY, 92]):
        raise RuntimeError(f"failed to write {path}")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("room", help="which room this camera is pointed at, e.g. grandpa, tj")
    parser.add_argument(
        "--interval", type=parse_interval, default="3m",
        help="time between shots: 30s, 1m, 3m, 1h (bare number means minutes). Default 3m",
    )
    parser.add_argument("--once", action="store_true", help="take a single photo and exit")
    parser.add_argument("--count", type=int, default=0, help="stop after N photos (0 = forever)")
    parser.add_argument("--camera", type=int, default=config.CAMERA_INDEX)
    parser.add_argument("--warmup", type=int, default=config.CAMERA_WARMUP_FRAMES)
    parser.add_argument("--outdir", type=Path, default=None,
                        help="override where photos land (default: the room queue)")
    args = parser.parse_args()

    try:
        room = config.room_name(args.room)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 1
    config.ensure_dirs(room)
    if args.outdir is None:
        args.outdir = config.paths_for(room)["pending"]
    args.outdir.mkdir(parents=True, exist_ok=True)

    if args.once:
        path = capture_one(args.outdir, args.camera, args.warmup)
        print(path)
        return 0

    limit = args.count if args.count > 0 else None
    print(
        f"[{room}] capturing every {args.interval:g}s into {args.outdir}"
        + (f", {limit} shots" if limit else "")
        + " -- Ctrl+C to stop"
    )

    taken = 0
    # Anchor each wake-up to the start time so the schedule does not drift by
    # however long the capture itself took.
    started = time.monotonic()
    try:
        while limit is None or taken < limit:
            try:
                path = capture_one(args.outdir, args.camera, args.warmup)
                taken += 1
                print(f"[{taken}] {path.name}")
            except Exception as exc:  # a transient camera glitch must not kill the run
                print(f"capture failed: {exc}", file=sys.stderr)

            if limit is not None and taken >= limit:
                break
            elapsed = time.monotonic() - started
            time.sleep(max(0.0, args.interval - (elapsed % args.interval)))
    except KeyboardInterrupt:
        print(f"\nstopped after {taken} photo(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
