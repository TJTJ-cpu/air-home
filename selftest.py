"""Check the pipeline still works, without touching your real data.

Everything runs in a throwaway folder: a fake room, fake photos, and a stubbed
model. Your database and captures are never opened.

    python selftest.py
"""
from __future__ import annotations

import contextlib
import time
import io
import pathlib
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import config

SANDBOX = pathlib.Path(tempfile.mkdtemp(prefix="airhome-selftest-"))
REAL_DB = config.DB_PATH

# Redirect every writable path before anything else imports these values.
config.CAPTURES = SANDBOX / "captures"
config.DATA = SANDBOX / "data"
config.DB_PATH = config.DATA / "readings.db"
config.ensure_dirs()
assert config.DB_PATH != REAL_DB, "sandbox not applied -- refusing to run"

import analyze  # noqa: E402
import process  # noqa: E402
import rebuild  # noqa: E402
import watch  # noqa: E402
import store  # noqa: E402
from capture import parse_interval  # noqa: E402
from extract import ExtractionError, validate  # noqa: E402

PASS, FAIL = [], []


def check(name: str, condition: bool, detail: str = "") -> None:
    (PASS if condition else FAIL).append(name)
    mark = "ok  " if condition else "FAIL"
    print(f"  [{mark}] {name}" + (f"   {detail}" if detail and not condition else ""))


READING = {"aqi": 3, "temperature_c": 26.0, "humidity_pct": 60,
           "pm25": 2, "pm10": 3, "co2_ppm": 700}


def fake_extract(conn, room, image, dry_run=False):
    stamp = process.captured_at(image)
    queues = config.paths_for(room)
    sidecar = {"room": room, "captured_at": stamp, "image": image.name,
               "model": "selftest", "reading": READING, "notes": []}
    saved = store.save(conn, room, stamp, image.name, READING, [])
    conn.commit()
    process._move(image, queues["processed"], sidecar)
    return ("saved" if saved else "duplicate"), "ok"


def seed(room: str, count: int, start: datetime) -> None:
    config.ensure_dirs(room)
    pending = config.paths_for(room)["pending"]
    for i in range(count):
        stamp = (start + timedelta(minutes=3 * i)).astimezone(timezone.utc)
        (pending / (stamp.strftime("%Y-%m-%dT%H-%M-%SZ") + ".jpg")).write_bytes(b"x")


def main() -> int:
    tz = timezone(timedelta(hours=config.LOCAL_UTC_OFFSET))
    print(f"sandbox: {SANDBOX}\n")

    print("units")
    check("interval parsing", parse_interval("3m") == 180 and parse_interval("30s") == 30)
    clean, notes = validate({"aqi": 5, "temperature_c": 27, "humidity_pct": 69,
                             "pm25": 2, "pm10": 2, "co2_ppm": 454})
    check("plausible values kept", clean["co2_ppm"] == 454 and not notes)
    clean, notes = validate({"co2_ppm": 12, "aqi": 2, "temperature_c": 27,
                             "humidity_pct": 60, "pm25": 2, "pm10": 2})
    check("implausible value rejected", clean["co2_ppm"] is None and len(notes) == 1)
    check("room names normalised", config.room_name(" TJ ") == "tj")
    try:
        config.room_name("../evil")
        check("path traversal rejected", False)
    except ValueError:
        check("path traversal rejected", True)

    print("\npipeline")
    process.process_one = fake_extract
    # Two nights of readings for one room, plus a second room at the same times.
    # Start at noon and run a full 24h, so the data straddles day and night --
    # a night-only window that keeps everything proves nothing.
    noon = datetime.now(tz).replace(hour=12, minute=0, second=0, microsecond=0)
    start = noon - timedelta(days=2)
    seed("guest", 480, start)
    seed("study", 480, start)
    sys.argv = ["process.py"]
    with contextlib.redirect_stdout(io.StringIO()):
        process.main()

    with store.connect() as conn:
        counts = {r["room"]: r["n"] for r in store.rooms(conn)}
    check("both rooms stored", counts.get("guest") == 480 and counts.get("study") == 480,
          str(counts))
    check("queues drained",
          all(not list(config.paths_for(r)["pending"].glob("*.jpg"))
              for r in ("guest", "study")))
    check("photos filed per room",
          len(list(config.paths_for("guest")["processed"].glob("*.jpg"))) == 480)

    print("\nreports")
    frame = analyze.load(config.DB_PATH, "guest")
    check("frame loads one room", len(frame) == 480 and "co2_ppm" in frame)
    ranges = [key for key, _, _ in analyze.available_ranges(frame)]
    check("zoom ranges offered", "1d" in ranges, str(ranges))
    night_ranges = [key for key, _, _ in analyze.available_night_ranges(frame)]
    check("night ranges offered", "nall" in night_ranges, str(night_ranges))
    only_night = analyze.night_only(frame, None)
    inside = all(h >= config.NIGHT_START_HOUR or h < config.NIGHT_END_HOUR
                 for h in only_night.index.hour)
    check("night filter keeps only night hours", inside and not only_night.empty)
    check("night filter drops daytime", len(only_night) < len(frame))

    out = config.DATA / "report.html"
    code = analyze.main(["--room", "guest", "--all-rooms", "--quiet", "--output", str(out)])
    check("report renders", code == 0 and out.exists())
    html = out.read_text(encoding="utf-8")
    check("room tabs present", '<nav class="rooms">' in html)
    check("both rooms linked", "report-guest.html" in html and "report-study.html" in html)
    check("per-room files written",
          (config.DATA / "report-study.html").exists())
    check("night group in picker", "Nights," in html)

    print("\nadaptive sampling")
    base = {"aqi": 2, "temperature_c": 27.0, "humidity_pct": 60,
            "pm25": 2, "pm10": 3, "co2_ppm": 700}

    def jump(second, apart=180):
        """Store two readings for a scratch room, ask if it counts as a jump."""
        with store.connect() as conn:
            t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
            store.save(conn, "probe", t0.isoformat(timespec="seconds"), "a.jpg", base, [])
            store.save(conn, "probe",
                       (t0 + timedelta(seconds=apart)).isoformat(timespec="seconds"),
                       "b.jpg", {**base, **second}, [])
            conn.commit()
            found = watch.recent_jump(conn, "probe", 180)
            conn.execute("DELETE FROM readings WHERE room = 'probe'")
            conn.commit()
        return found

    check("a fan dropping CO2 triggers", bool(jump({"co2_ppm": 580})))
    check("a 2C drop triggers", bool(jump({"temperature_c": 25.0})))
    check("ordinary drift does not", not jump({"co2_ppm": 706}))
    check("humidity never triggers -- it follows the weather, not the room",
          not jump({"humidity_pct": 79}))
    check("AQI alone does not (it is derived from PM2.5)", not jump({"aqi": 40}))
    check("a gap across an outage is not a change",
          not jump({"co2_ppm": 400}, apart=7200))

    def gap(co2, interval=180, fast=False, age=0):
        """Store one recent reading, ask how long to wait after it."""
        args = SimpleNamespace(interval=interval, no_adaptive=False)
        with store.connect() as conn:
            stamp = datetime.now(timezone.utc) - timedelta(seconds=age)
            store.save(conn, "probe", stamp.isoformat(timespec="seconds"),
                       "a.jpg", {**base, "co2_ppm": co2}, [])
            conn.commit()
            until = time.monotonic() + 60 if fast else 0.0
            seconds, _why = watch.sampling_gap(conn, "probe", args, until, "test")
            conn.execute("DELETE FROM readings WHERE room = 'probe'")
            conn.commit()
        return seconds

    check("clean air waits the full interval", gap(600) == 180)
    check("CO2 above 800 drops to 120s even when steady",
          gap(1044) == config.HIGH_INTERVAL_SECONDS)
    check("exactly 800 is not above it", gap(800) == 180)
    check("something changing beats a high level",
          gap(1044, fast=True) == config.FAST_INTERVAL_SECONDS)
    check("a stale reading does not hold the fast rate on",
          gap(1044, age=3600) == 180)
    check("the level rule never slows a faster interval down",
          gap(1044, interval=60) == 60)

    print("\nrecovery")
    with store.connect() as conn:
        before = store.count(conn)
    sys.argv = ["rebuild.py"]
    with contextlib.redirect_stdout(io.StringIO()):
        rebuild.main()
    with store.connect() as conn:
        check("rebuild is idempotent", store.count(conn) == before)

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("failed: " + ", ".join(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    try:
        code = main()
    finally:
        shutil.rmtree(SANDBOX, ignore_errors=True)
    raise SystemExit(code)
