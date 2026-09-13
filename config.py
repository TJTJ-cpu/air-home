"""Shared paths and environment-backed settings."""
from __future__ import annotations

import os
import re
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

CAPTURES = ROOT / "captures"
DATA = ROOT / "data"
DB_PATH = DATA / "readings.db"
# Generated pages and exports live together, so data/ holds only the things
# that are yours: the database, your night notes, and backups.
REPORTS = DATA / "reports"

# Every reading belongs to a room. One database holds them all, keyed by
# (room, captured_at), so rooms can be compared without opening several files.
# Photos are per-room folders, because each room needs its own work queue.
LEGACY_ROOM = os.getenv("LEGACY_ROOM", "grandpa").strip().lower()
DEFAULT_ROOM = os.getenv("DEFAULT_ROOM", "").strip().lower()
_ROOM_OK = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


def room_name(raw: str) -> str:
    """Normalise and check a room name -- it becomes a folder, so be strict."""
    name = (raw or "").strip().lower().replace(" ", "-")
    if not _ROOM_OK.match(name):
        raise ValueError(
            f"bad room name {raw!r}: use letters, digits, dash or underscore "
            "(max 32 chars), e.g. grandpa, tj, mom"
        )
    return name


def rooms_on_disk() -> list[str]:
    """Rooms that have a captures folder, whether or not they have readings."""
    if not CAPTURES.is_dir():
        return []
    return sorted(p.name for p in CAPTURES.iterdir()
                  if p.is_dir() and _ROOM_OK.match(p.name))


def paths_for(room: str) -> dict:
    """The four queue folders for one room."""
    base = CAPTURES / room_name(room)
    return {"base": base, "pending": base / "pending", "processed": base / "processed",
            "failed": base / "failed", "rejected": base / "rejected"}


def _int(name: str, default: int | None) -> int | None:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    return int(raw)


CAMERA_INDEX = _int("CAMERA_INDEX", 0)
CAMERA_WARMUP_FRAMES = _int("CAMERA_WARMUP_FRAMES", 8)
CAMERA_WIDTH = _int("CAMERA_WIDTH", None)
CAMERA_HEIGHT = _int("CAMERA_HEIGHT", None)

# Readings are stored in UTC. Analysis is done in local time, because "the
# trend across the night" only means anything on a local clock.
LOCAL_UTC_OFFSET = float(os.getenv("LOCAL_UTC_OFFSET", "7"))
# A "night" runs from the evening of one day into the next morning, so it
# survives midnight -- which is the whole point of having it. The window is
# deliberately wider than sleep itself: the hour before bed gives a baseline to
# measure the climb against, and the hour after waking shows how fast the room
# clears once the door opens.
NIGHT_START_HOUR = _int("NIGHT_START_HOUR", 20)
NIGHT_END_HOUR = _int("NIGHT_END_HOUR", 9)
# Optional, yours to write: {"2026-08-24": "window open 10cm"}. Keyed by the
# evening the night began. Blank or missing simply shows no label.
NIGHT_LABELS = DATA / "night_labels.json"

LMSTUDIO_BASE_URL = os.getenv("LMSTUDIO_BASE_URL", "http://localhost:1234/v1")
LMSTUDIO_MODEL = os.getenv("LMSTUDIO_MODEL", "qwen/qwen3-vl-8b")
VISION_MAX_WIDTH = _int("VISION_MAX_WIDTH", 1280)

# Physically plausible bounds. A value outside its range is treated as a
# misread digit and stored as NULL rather than as data.
# Indoor temperature bounds are climate-specific: -20 C is plausible in a
# Finnish porch and impossible in a Lao bedroom. Narrow bounds are what let a
# misread digit be caught instead of stored, so set these to your own climate.
TEMP_MIN_C = float(os.getenv("TEMP_MIN_C", "10"))
TEMP_MAX_C = float(os.getenv("TEMP_MAX_C", "45"))

FIELD_RANGES: dict[str, tuple[float, float]] = {
    "aqi": (0, 500),
    "temperature_c": (TEMP_MIN_C, TEMP_MAX_C),
    "humidity_pct": (0, 100),
    "pm25": (0, 1000),
    "pm10": (0, 1000),
    "co2_ppm": (300, 10000),
}

FIELDS = tuple(FIELD_RANGES)


# Adaptive sampling. When a reading jumps by more than this from the one
# before, something actually happened -- a door opened, a fan came on -- and
# the next few minutes are worth sampling closely.
#
# The numbers come from the 99th percentile of changes actually observed
# between consecutive 3-minute readings, so they fire on about 1% of them.
#
# Two channels are deliberately absent:
#
# AQI is computed from PM2.5 and moves in lockstep with it, so including it
# would count the same event twice.
#
# Humidity does not signal the events this is for. Measured across 7,900
# readings it barely differs between night and day (64% vs 65%) while CO2
# nearly doubles (770 vs 479), and its correlation with CO2 overnight is
# +0.03 -- no relationship at all. It follows the weather outside, not the
# door being opened, so it would fire on things that are not events and stay
# silent for things that are.
JUMP_THRESHOLDS: dict[str, float] = {
    "co2_ppm": 50,        # normal step is 6; a door opening shows 100+
    "temperature_c": 2,   # the device reports whole degrees, so 2 is a real move
    "pm25": 10,
    "pm10": 12,
}

# While something is changing, sample every minute instead, and keep doing so
# until it has been quiet for this long. Ten minutes covers the shape of a
# room clearing out; each new jump extends it.
FAST_INTERVAL_SECONDS = 60
FAST_WINDOW_SECONDS = 600

# Sitting above this, the room is worth watching more closely even when the
# number is steady -- a flat 1044 is the part of the night you most want
# detail on. This is a level, not a change: it holds for as long as CO2 stays
# up, where the jump rule above times out after ten quiet minutes.
HIGH_CO2_PPM = 800
HIGH_INTERVAL_SECONDS = 120


def ensure_dirs(room: str | None = None) -> None:
    """Make the data folder, and one room's queue folders if a room is given."""
    DATA.mkdir(parents=True, exist_ok=True)
    REPORTS.mkdir(parents=True, exist_ok=True)
    CAPTURES.mkdir(parents=True, exist_ok=True)
    if room:
        for path in paths_for(room).values():
            path.mkdir(parents=True, exist_ok=True)
