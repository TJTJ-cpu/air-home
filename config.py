"""Shared paths and environment-backed settings."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

CAPTURES = ROOT / "captures"
PENDING = CAPTURES / "pending"
PROCESSED = CAPTURES / "processed"
FAILED = CAPTURES / "failed"
DATA = ROOT / "data"
DB_PATH = DATA / "readings.db"


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
FIELD_RANGES: dict[str, tuple[float, float]] = {
    "aqi": (0, 500),
    "temperature_c": (-20, 60),
    "humidity_pct": (0, 100),
    "pm25": (0, 1000),
    "pm10": (0, 1000),
    "co2_ppm": (300, 10000),
}

FIELDS = tuple(FIELD_RANGES)


def ensure_dirs() -> None:
    for path in (PENDING, PROCESSED, FAILED, DATA):
        path.mkdir(parents=True, exist_ok=True)
