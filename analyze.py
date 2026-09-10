"""Analyse readings with pandas and write a self-contained HTML report.

    python analyze.py                      # everything collected
    python analyze.py --last 6h            # the last six hours
    python analyze.py --today
    python analyze.py --every 1h           # average into hourly buckets
    python analyze.py --night              # most recent night, if you want one
    python analyze.py --from "2026-08-23 11:00" --to "2026-08-23 12:30"
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import webbrowser
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

import config
import report
import store

# field, label, unit, decimal places, threshold, threshold label
METRICS = [
    ("co2_ppm", "CO₂", " ppm", 0, 1000, "1000 ppm"),
    ("temperature_c", "Temperature", " °C", 1, None, ""),
    ("humidity_pct", "Humidity", " %", 0, None, ""),
    ("aqi", "AQI", "", 0, None, ""),
    ("pm25", "PM2.5", " µg/m³", 0, None, ""),
    ("pm10", "PM10", " µg/m³", 0, None, ""),
]
# Health bands, drawn as a green-to-red gradient along the line.
#
# CO2 follows the traffic-light convention used by REHVA and consumer monitors
# (green under 800, amber to 1000, orange to 1400, red above). Note this is a
# practical convention, not a health standard: ASHRAE explicitly does NOT set
# an indoor CO2 limit, and the old 1000 ppm figure was removed from 62.1 for
# being misread as one. High CO2 indicates poor ventilation rather than direct
# harm at these levels.
#
# PM2.5 and PM10 use the WHO 2021 24-hour guideline and its interim targets.
# AQI uses the US EPA category boundaries, which are already a colour scale.
BANDS = {
    "co2_ppm": [(800, "good"), (1000, "warning"), (1400, "serious"), (None, "critical")],
    "pm25":    [(15, "good"), (25, "warning"), (35, "serious"), (None, "critical")],
    "pm10":    [(45, "good"), (75, "warning"), (100, "serious"), (None, "critical")],
    "aqi":     [(50, "good"), (100, "warning"), (150, "serious"), (None, "critical")],
}

FIELDS = [field for field, *_ in METRICS]
# Column headers carry the unit, so a table cell never needs one.
METRIC_COLUMNS = [(field, f"{label}{unit}".strip())
                  for field, label, unit, *_ in METRICS]

LOCAL_TZ = timezone(timedelta(hours=config.LOCAL_UTC_OFFSET))
def report_path(room: str, base: Path) -> Path:
    """Each room's report lives in its own file beside the main one."""
    return base.with_name(f"{base.stem}-{room}{base.suffix}")


def tabs_are_stale(path: Path, rooms: list, base: Path) -> bool:
    """Does this report's room switcher still match the rooms that exist?

    Adding or removing a room leaves every other room's file listing the old
    set, which strands you on a page with no way back. Checking is cheap --
    the nav sits near the top -- and lets a single-room render repair the
    others instead of silently leaving them wrong.
    """
    if not path.exists():
        return True
    if len(rooms) < 2:
        return False        # no switcher is rendered at all, nothing to match
    with path.open("r", encoding="utf-8") as handle:
        head = handle.read(64_000)
    listed = set(re.findall(r'<a href="([^"]+\.html)"', head))
    return listed != {report_path(name, base).name for name in rooms}


MAX_POINTS = 480  # beyond this an SVG line is denser than the screen can show
# A night needs at least this much data before it is worth comparing.
MIN_NIGHT_READINGS, MIN_NIGHT_HOURS = 20, 2.0
# The focused single-metric chart is re-rendered at these dimensions.
FOCUS_WIDTH, FOCUS_PLOT_H = 1060, 300

# What you may pass to --metric, mapped to the database column.
METRIC_ALIASES = {
    "all": "all", "co2": "co2_ppm", "co2_ppm": "co2_ppm",
    "temp": "temperature_c", "temperature": "temperature_c",
    "temperature_c": "temperature_c", "hum": "humidity_pct",
    "humidity": "humidity_pct", "humidity_pct": "humidity_pct",
    "aqi": "aqi", "pm25": "pm25", "pm2.5": "pm25", "pm10": "pm10",
}


def fmt(value: float, places: int) -> str:
    if value is None or pd.isna(value):
        return "-"
    return f"{value:.{places}f}" if places else f"{round(value):g}"


def load(db_path: Path, room: str | None = None) -> pd.DataFrame:
    """One room's readings (or all of them), indexed by local time."""
    with sqlite3.connect(db_path) as conn:
        if room:
            frame = pd.read_sql_query(
                "SELECT * FROM readings WHERE room = ? ORDER BY captured_at",
                conn, params=(room,))
        else:
            frame = pd.read_sql_query("SELECT * FROM readings ORDER BY captured_at", conn)
    if frame.empty:
        return frame
    stamps = pd.to_datetime(frame["captured_at"], format="ISO8601", utc=True)
    frame = frame.set_index(stamps.dt.tz_convert(LOCAL_TZ)).drop(columns=["captured_at"])
    frame.index.name = "local_time"
    return frame


def night_bounds(night) -> tuple[pd.Timestamp, pd.Timestamp]:
    """22:00 on the given date through 06:00 the next morning, local time."""
    midnight = pd.Timestamp(datetime.combine(night, datetime.min.time()), tz=LOCAL_TZ)
    return (midnight + timedelta(hours=config.NIGHT_START_HOUR),
            midnight + timedelta(days=1, hours=config.NIGHT_END_HOUR))


def last_night_with_data(frame: pd.DataFrame, lookback: int = 30):
    latest = frame.index.max().date()
    for back in range(lookback):
        night = latest - timedelta(days=back)
        start, end = night_bounds(night)
        if not frame.loc[(frame.index >= start) & (frame.index < end)].empty:
            return night
    return None


def rate_per_hour(series: pd.Series) -> float | None:
    """Least-squares slope in units per hour, or None if too little data."""
    clean = series.dropna()
    if len(clean) < 3:
        return None
    hours = (clean.index - clean.index[0]).total_seconds().to_numpy() / 3600.0
    if hours[-1] <= 0:
        return None
    slope, _ = np.polyfit(hours, clean.to_numpy(dtype=float), 1)
    return float(slope)


def bucket(frame: pd.DataFrame, rule) -> tuple[pd.DataFrame, pd.Series]:
    """Mean of each metric per time bucket, plus how many readings fed it."""
    numeric = frame[[f for f in FIELDS if f in frame]]
    return numeric.resample(rule).mean(), numeric.resample(rule).size()


def auto_bucket(frame: pd.DataFrame) -> tuple[pd.DataFrame, str]:
    """Average down only if there are more points than a chart can resolve."""
    if len(frame) <= MAX_POINTS:
        return frame, ""
    span = (frame.index.max() - frame.index.min()).total_seconds()
    minutes = max(1, int(np.ceil(span / MAX_POINTS / 60)))
    means, _ = bucket(frame, pd.Timedelta(minutes=minutes))
    return means, f"averaged into {minutes}-minute buckets"


def build_tiles(frame: pd.DataFrame) -> str:
    tiles: list[str] = []

    co2 = frame["co2_ppm"].dropna() if "co2_ppm" in frame else pd.Series(dtype=float)
    if not co2.empty:
        tiles.append(report.stat_tile(
            "Peak CO₂", fmt(co2.max(), 0), " ppm",
            f"at {co2.idxmax().strftime('%d %b %H:%M')}",
        ))
        slope = rate_per_hour(frame["co2_ppm"])
        if slope is not None:
            tiles.append(report.stat_tile(
                "CO₂ trend", f"{slope:+.0f}", " ppm/h",
                ("rising" if slope >= 0 else "falling") + " across the window",
            ))

    for field, label, unit, places in (("temperature_c", "Temperature", " °C", 1),
                                       ("humidity_pct", "Humidity", " %", 0)):
        series = frame[field].dropna() if field in frame else pd.Series(dtype=float)
        if series.empty:
            continue
        tiles.append(report.stat_tile(
            label, f"{fmt(series.min(), places)}–{fmt(series.max(), places)}", unit,
            f"mean {fmt(series.mean(), places)}{unit}",
        ))

    gaps = int(frame[[f for f in FIELDS if f in frame]].isna().all(axis=1).sum())
    tiles.append(report.stat_tile(
        "Readings", f"{len(frame):g}", "",
        f"{gaps} with no values" if gaps else "no gaps",
    ))
    return f'<div class="tiles">{"".join(tiles)}</div>'


def samples_for(frame: pd.DataFrame, field: str):
    return [
        (moment.to_pydatetime(), None if pd.isna(value) else float(value))
        for moment, value in frame[field].items()
    ]


def plotted_metrics(frame: pd.DataFrame):
    """Metrics that actually have values in this window."""
    return [m for m in METRICS
            if m[0] in frame and not frame[m[0]].dropna().empty]


def build_filters(frame: pd.DataFrame, active: str) -> str:
    options = [("all", "All")] + [(f, label) for f, label, *_ in plotted_metrics(frame)]
    return report.filter_row(options, active)


def build_charts(frame: pd.DataFrame, active: str) -> str:
    """The small-multiples grid, hidden while a single metric is focused."""
    charts = [
        report.line_chart(label, unit, samples_for(frame, field), places,
                          threshold, threshold_label, bands=BANDS.get(field))
        for field, label, unit, places, threshold, threshold_label in plotted_metrics(frame)
    ]
    off = "" if active == "all" else " off"
    return f'<div class="charts{off}">{"".join(charts)}</div>'


def build_focus(frame: pd.DataFrame, active: str) -> str:
    """One large chart per metric; the selector reveals whichever is chosen."""
    panels = []
    for field, label, unit, places, threshold, threshold_label in plotted_metrics(frame):
        chart = report.line_chart(label, unit, samples_for(frame, field), places,
                                  threshold, threshold_label,
                                  width=FOCUS_WIDTH, plot_h=FOCUS_PLOT_H,
                                  bands=BANDS.get(field))
        hidden = "" if field == active else ' style="display:none"'
        panels.append(f'<div data-metric="{field}"{hidden}>{chart}</div>')
    on = " on" if active != "all" else ""
    return f'<div class="focus{on}">{"".join(panels)}</div>'


def summary_table(frame: pd.DataFrame) -> str:
    """Mean, spread and trend for every metric over the whole window."""
    rows = []
    for field, label, unit, places, *_ in METRICS:
        series = frame[field].dropna() if field in frame else pd.Series(dtype=float)
        if series.empty:
            continue
        slope = rate_per_hour(frame[field])
        rows.append({
            "metric": f"{label}{unit}".strip(),
            "n": len(series),
            "mean": fmt(series.mean(), max(1, places)),
            "min": fmt(series.min(), places),
            "max": fmt(series.max(), places),
            "trend": "-" if slope is None else f"{slope:+.{max(1, places)}f} /h",
        })
    columns = [("metric", "Metric"), ("n", "Readings"), ("mean", "Mean"),
               ("min", "Min"), ("max", "Max"), ("trend", "Trend")]
    return report.table(rows, columns)


def averages_table(frame: pd.DataFrame, rule, label_format: str) -> str:
    means, counts = bucket(frame, rule)
    rows = []
    for stamp in means.index:
        entry = {"bucket": stamp.strftime(label_format), "n": int(counts.loc[stamp])}
        if entry["n"] == 0:
            continue  # an empty bucket is a gap, not a measurement of zero
        for field, _, _, places, *_ in METRICS:
            entry[field] = fmt(means.loc[stamp, field], places) if field in means else "-"
        rows.append(entry)
    columns = [("bucket", "Bucket"), ("n", "Readings")] + METRIC_COLUMNS
    return report.table(rows, columns)


def build_table(frame: pd.DataFrame) -> str:
    rows = []
    for moment, row in frame.iloc[::-1].iterrows():
        entry = {"time": moment.strftime("%Y-%m-%d %H:%M")}
        for field, _, _, places, *_ in METRICS:
            entry[field] = fmt(row.get(field), places) if field in row else "-"
        rows.append(entry)
    columns = [("time", "Local time")] + METRIC_COLUMNS
    return report.table(rows, columns)


# Nights are offered as ranges rather than one entry per date: a list of
# dates grows without limit, while these stay constant however long you run.
NIGHT_RANGES = [
    ("n3d", "Nights, last 3 days", pd.Timedelta(days=3)),
    ("n1w", "Nights, last week", pd.Timedelta(days=7)),
    ("nall", "Nights, all time", None),
]

ALLTIME_KEY = "alltime"
ALLTIME_LABEL = "All time"
# Zoom levels, shortest first. A range only appears once you have more data
# than it covers -- otherwise it would be an identical copy of "All time".
RANGES = [
    ("1d", "Last 24 hours", pd.Timedelta(days=1)),
    ("3d", "Last 3 days", pd.Timedelta(days=3)),
    ("1w", "Last week", pd.Timedelta(days=7)),
    ("1mo", "Last month", pd.Timedelta(days=30)),
]
ROLLING_KEY, ROLLING_LABEL = RANGES[0][0], RANGES[0][1]
# The all-time table is capped; the whole history is in the database.
ALLTIME_TABLE_LIMIT = 500


def pane_span(frame: pd.DataFrame) -> str:
    """Readable span, showing dates only when the window crosses midnight."""
    first, last = frame.index.min(), frame.index.max()
    if first.date() == last.date():
        return f"{first:%H:%M} → {last:%H:%M}"
    return f"{first:%d %b %H:%M} → {last:%d %b %H:%M}"


def day_pane(frame: pd.DataFrame, value: str, active: str, metric: str,
             extra: str = "", averages: str = "hour",
             table_limit: int | None = None) -> str:
    """Everything for one view: tiles, charts, tables -- as one hidden pane.

    `averages` picks hourly or daily buckets: hourly rows over months would be
    thousands long. `table_limit` caps the raw table for the same reason -- the
    full history lives in the database and the CSV export, not in the page.
    """
    charted, _ = auto_bucket(frame)
    if averages == "day":
        summary = report.section("Averages by day",
                                 averages_table(frame, pd.Timedelta(days=1), "%a %d %b"),
                                 collapsed=False)
    else:
        summary = report.section("Averages by hour",
                                 averages_table(frame, pd.Timedelta(hours=1), "%H:00"),
                                 collapsed=False)
    shown = frame if table_limit is None else frame.iloc[-table_limit:]
    title = (f"All {len(frame)} readings" if table_limit is None or len(frame) <= table_limit
             else f"Most recent {len(shown)} of {len(frame)} readings")
    body = (
        build_tiles(frame)
        + build_filters(charted, metric)
        + build_charts(charted, metric)
        + build_focus(charted, metric)
        + report.section("Summary and trend", summary_table(frame))
        + summary
        + extra
        + report.section(title, build_table(shown), collapsed=True)
    )
    on = " on" if value == active else ""
    return f'<div class="day{on}" data-day="{value}">{body}</div>'


def night_labels() -> dict:
    """Optional notes describing what you changed each night."""
    if not config.NIGHT_LABELS.exists():
        return {}
    try:
        raw = json.loads(config.NIGHT_LABELS.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    # Blank entries are placeholders waiting to be filled in, not labels.
    return {key: value for key, value in raw.items() if str(value).strip()}


def nights(frame: pd.DataFrame, keep: int):
    """Per-night frames, newest first. A night is keyed by the evening it began.

    Only nights with a decent amount of data are returned -- a night you were
    only capturing for twenty minutes of is noise in a comparison, not a result.
    """
    found = []
    evenings = sorted({stamp.date() for stamp in frame.index}, reverse=True)
    for evening in evenings:
        start, end = night_bounds(evening)
        subset = frame.loc[(frame.index >= start) & (frame.index < end)]
        span = ((subset.index.max() - subset.index.min()).total_seconds() / 3600
                if len(subset) else 0)
        if len(subset) >= MIN_NIGHT_READINGS and span >= MIN_NIGHT_HOURS:
            found.append((evening, subset))
    return found[:keep]


def night_only(frame: pd.DataFrame, span) -> pd.DataFrame:
    """Readings inside a night window, optionally limited to the last `span`.

    Daytime rows are dropped, so the chart shows one segment per night. The
    daytime gap is far longer than the line-bridging threshold, so the line
    breaks between nights rather than drawing straight through the day.
    """
    if frame.empty:
        return frame
    subset = frame if span is None else rolling_window(frame, span)
    if subset.empty:
        return subset
    hour = subset.index.hour
    start, end = config.NIGHT_START_HOUR, config.NIGHT_END_HOUR
    # A night normally wraps midnight, so the test is an OR, not an AND.
    inside = ((hour >= start) | (hour < end)) if start > end else \
             ((hour >= start) & (hour < end))
    return subset.loc[inside]


def available_night_ranges(frame: pd.DataFrame):
    """Night ranges worth offering, given how much history exists."""
    total = frame.index.max() - frame.index.min()
    out = [(k, lab, span) for k, lab, span in NIGHT_RANGES
           if span is not None and span < total]
    out.append(NIGHT_RANGES[-1])          # all-time is always meaningful
    return out


def night_stats(frame: pd.DataFrame) -> dict:
    """The numbers that make one night comparable with another."""
    co2 = frame["co2_ppm"].dropna()
    span = (frame.index.max() - frame.index.min()).total_seconds() / 3600
    stats = {
        "readings": len(frame),
        "hours": span,
        "start": None, "peak": None, "peak_at": None, "mean": None,
        "rise": None, "above": None,
    }
    if co2.empty:
        return stats
    # Median of the first half hour, so one misread digit cannot define the
    # baseline the whole night is measured against.
    opening = co2.loc[co2.index <= co2.index.min() + pd.Timedelta(minutes=30)]
    stats["start"] = float(opening.median())
    stats["peak"] = float(co2.max())
    stats["peak_at"] = co2.idxmax()
    stats["mean"] = float(co2.mean())
    climb = (stats["peak_at"] - co2.index.min()).total_seconds() / 3600
    if climb > 0:
        stats["rise"] = (stats["peak"] - stats["start"]) / climb
    # Share of the night over 1000 ppm, scaled to hours.
    stats["above"] = float((co2 > 1000).mean()) * span
    return stats


def nights_table(found: list, labels: dict) -> str:
    """One row per night, with each night measured against the first one."""
    rows = []
    baseline = None
    for evening, subset in reversed(found):  # oldest first: it is the control
        stats = night_stats(subset)
        if baseline is None and stats["peak"] is not None:
            baseline = stats
        change = "-"
        if baseline is not None and stats["peak"] is not None and stats is not baseline:
            delta = (stats["peak"] - baseline["peak"]) / baseline["peak"] * 100
            change = f"{delta:+.0f}%"
        rows.append({
            "night": evening.strftime("%a %d %b"),
            "what": labels.get(evening.isoformat()) or "-",
            "n": stats["readings"],
            "hours": f"{stats['hours']:.1f}",
            "start": fmt(stats["start"], 0),
            "peak": fmt(stats["peak"], 0),
            "peak_at": stats["peak_at"].strftime("%H:%M") if stats["peak_at"] is not None else "-",
            "mean": fmt(stats["mean"], 0),
            "rise": fmt(stats["rise"], 0) if stats["rise"] is not None else "-",
            "above": f"{stats['above']:.1f}" if stats["above"] is not None else "-",
            "change": change,
        })
    rows.reverse()  # display newest first, like everything else
    columns = [
        ("night", "Night"), ("what", "What changed"), ("n", "Readings"),
        ("hours", "Hours"), ("start", "CO₂ start"), ("peak", "CO₂ peak"),
        ("peak_at", "Peak at"), ("mean", "CO₂ mean"), ("rise", "Rise ppm/h"),
        ("above", "Hours >1000"), ("change", "Peak vs first"),
    ]
    return report.table(rows, columns)


def by_day(frame: pd.DataFrame, keep: int):
    """Per-day frames, most recent `keep` days, NEWEST first."""
    days = sorted({stamp.date() for stamp in frame.index})[-keep:]
    return [(day, frame.loc[[s.date() == day for s in frame.index]])
            for day in reversed(days)]


def rolling_window(frame: pd.DataFrame, span: pd.Timedelta) -> pd.DataFrame:
    """The last `span` of readings.

    Anchored to the newest reading rather than the wall clock: if capture has
    been stopped for a while, you still want the last day that exists, not a
    mostly-empty window.
    """
    return frame.loc[frame.index >= frame.index.max() - span]


def available_ranges(frame: pd.DataFrame):
    """(key, label, window) for each range worth offering, plus All time."""
    total = frame.index.max() - frame.index.min()
    offered = [(key, label, span) for key, label, span in RANGES if span < total]
    if not offered:  # very little data: the shortest range is still useful
        offered = [RANGES[0]]
    return offered


def range_pane(frame: pd.DataFrame, key: str, active: str, metric: str,
               extra: str = "") -> str:
    """A pane sized for its zoom level: hourly rows close in, daily further out."""
    span_hours = (frame.index.max() - frame.index.min()).total_seconds() / 3600
    return day_pane(frame, key, active, metric, extra=extra,
                    averages="hour" if span_hours <= 48 else "day",
                    table_limit=None if span_hours <= 48 else ALLTIME_TABLE_LIMIT)


def export_nights(frame: pd.DataFrame, path: Path, keep: int) -> int:
    """One row per night, for loading into pandas or a spreadsheet later."""
    found = nights(frame, keep)
    if not found:
        print("no nights with enough data yet")
        return 1
    labels = night_labels()
    rows = []
    for evening, subset in reversed(found):
        stats = night_stats(subset)
        rows.append({
            "night": evening.isoformat(),
            "label": labels.get(evening.isoformat(), ""),
            "readings": stats["readings"],
            "hours": round(stats["hours"], 2),
            "co2_start": stats["start"],
            "co2_peak": stats["peak"],
            "co2_peak_at": stats["peak_at"].strftime("%H:%M") if stats["peak_at"] is not None else "",
            "co2_mean": round(stats["mean"], 1) if stats["mean"] is not None else "",
            "co2_rise_per_hour": round(stats["rise"], 1) if stats["rise"] is not None else "",
            "hours_above_1000": round(stats["above"], 2) if stats["above"] is not None else "",
            "temp_min": subset["temperature_c"].min(),
            "temp_max": subset["temperature_c"].max(),
            "humidity_mean": round(subset["humidity_pct"].mean(), 1),
        })
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(path, index=False)
    print(f"wrote {path}  ({len(rows)} night(s))")
    return 0


def choose_window(frame: pd.DataFrame, args) -> tuple[pd.DataFrame, str]:
    """Slice the readings per the flags. Default is everything collected."""
    if args.start or args.end:
        start = pd.Timestamp(args.start, tz=LOCAL_TZ) if args.start else frame.index.min()
        end = pd.Timestamp(args.end, tz=LOCAL_TZ) if args.end else frame.index.max()
        return frame.loc[(frame.index >= start) & (frame.index <= end)], "Selected window"

    if args.last:
        end = frame.index.max()
        start = end - pd.Timedelta(args.last)
        return frame.loc[frame.index >= start], f"Last {args.last}"

    if args.today:
        today = datetime.now(LOCAL_TZ).date()
        start = pd.Timestamp(datetime.combine(today, datetime.min.time()), tz=LOCAL_TZ)
        return frame.loc[frame.index >= start], f"Today, {today.strftime('%d %b %Y')}"

    if args.night is not None:
        night = (datetime.strptime(args.night, "%Y-%m-%d").date() if args.night
                 else last_night_with_data(frame))
        if night is None:
            hours = f"{config.NIGHT_START_HOUR:02d}:00–{config.NIGHT_END_HOUR:02d}:00"
            print(f"no readings in any {hours} window yet -- showing everything instead.")
            return frame, "All readings"
        start, end = night_bounds(night)
        return (frame.loc[(frame.index >= start) & (frame.index < end)],
                f"Night of {night.strftime('%d %b %Y')}")

    return frame, "All readings"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--last", help="a span back from the newest reading: 30m, 6h, 2d")
    parser.add_argument("--today", action="store_true", help="since local midnight")
    parser.add_argument("--night", nargs="?", const="", metavar="RANGE",
                        help="open on the nights view: n3d, n1w, nall")
    parser.add_argument("--from", dest="start", help="window start, local time")
    parser.add_argument("--to", dest="end", help="window end, local time")
    parser.add_argument("--every", metavar="SPAN",
                        help="average the charts into buckets: 5min, 1h, 1d")
    parser.add_argument("--all", action="store_true",
                        help="open on the all-time view")
    parser.add_argument("--date", metavar="RANGE",
                        help="open on a zoom level: 1d, 3d, 1w, 1mo, alltime. Default 1d")
    parser.add_argument("--days", type=int, default=7,
                        help="how many recent nights to include in the picker. Default 7")
    parser.add_argument("--metric", default="all", metavar="NAME",
                        help="open focused on one metric: co2, temp, humidity, "
                             "aqi, pm25, pm10")
    parser.add_argument("--export-nights", type=Path, metavar="PATH", nargs="?",
                        const=config.DATA / "nights.csv",
                        help="write the night comparison to CSV and exit")
    parser.add_argument("--output", type=Path, default=config.DATA / "report.html")
    parser.add_argument("--open", action="store_true", help="open in the browser when done")
    parser.add_argument("--room", help="which room to report on (default: the most recent)")
    parser.add_argument("--list-rooms", action="store_true", help="show rooms and exit")
    parser.add_argument("--all-rooms", action="store_true",
                        help="rebuild every room's report, not just one")
    parser.add_argument("--no-index", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--quiet", action="store_true", help="suppress the wrote-file line")
    argv = list(sys.argv[1:] if argv is None else argv)
    args = parser.parse_args(argv)

    active = METRIC_ALIASES.get(args.metric.strip().lower())
    if active is None:
        print(f"unknown --metric {args.metric!r}; try one of: "
              f"{', '.join(sorted(set(METRIC_ALIASES) - {'all'}))}")
        return 1

    if not config.DB_PATH.exists():
        print(f"no database at {config.DB_PATH} -- run process.py first")
        return 1

    with store.connect() as conn:
        available = store.rooms(conn)
        if args.list_rooms:
            if not available:
                print("no rooms with readings yet")
                return 0
            for row in available:
                print(f"  {row['room']:12} {row['n']:6} readings   "
                      f"{row['first'][:16].replace('T', ' ')} -> {row['last'][:16].replace('T', ' ')}")
            return 0
        names = [row["room"] for row in available]
        if args.room:
            try:
                room = config.room_name(args.room)
            except ValueError as exc:
                print(exc); return 1
            if room not in names:
                print(f"no readings for room {room!r}. Rooms: {', '.join(names) or 'none'}")
                return 1
        else:
            room = store.latest_room(conn)

    # Repair any other room whose switcher no longer matches the rooms that
    # exist, so adding or wiping a room can never strand you on a dead page.
    # --no-index marks a child render, and a child must not start its own
    # repair pass or the two would call each other forever.
    if not args.no_index:
        rest = [a for a in argv if a not in ("--all-rooms", "--open")]
        for other in names:
            if other == room:
                continue
            if args.all_rooms or tabs_are_stale(report_path(other, args.output),
                                                names, args.output):
                main(rest + ["--room", other, "--quiet", "--no-index"])

    frame = load(config.DB_PATH, room)
    if frame.empty:
        print("no readings yet -- run process.py first")
        return 1

    if args.export_nights:
        return export_nights(frame, args.export_nights, args.days)

    single_window = bool(args.last or args.today or args.start
                         or args.end or args.every)

    if single_window:
        window, label = choose_window(frame, args)
        if window.empty:
            print("no readings in that window")
            return 1
        raw = window
        if args.every:
            try:
                rule = pd.Timedelta(args.every)
            except ValueError:
                print(f"bad --every value {args.every!r}; try 5min, 1h, 1d")
                return 1
            charted, note = bucket(raw, rule)[0], f"charts averaged every {args.every}"
        else:
            charted, note = auto_bucket(raw)

        hours = (raw.index.max() - raw.index.min()).total_seconds() / 3600
        sections = [
            report.section("Summary and trend", summary_table(raw)),
            report.section("Averages by hour",
                           averages_table(raw, pd.Timedelta(hours=1), "%d %b %H:00"),
                           collapsed=False),
        ]
        if hours >= 24:
            sections.append(report.section(
                "Averages by day",
                averages_table(raw, pd.Timedelta(days=1), "%a %d %b"), collapsed=False))
        sections.append(report.section(f"All {len(raw)} readings",
                                       build_table(raw), collapsed=True))
        body = (build_tiles(raw) + build_filters(charted, active)
                + build_charts(charted, active) + build_focus(charted, active)
                + "".join(sections))
        subtitle = (f"{raw.index.min():%d %b %H:%M} → {raw.index.max():%d %b %H:%M} local"
                    f" · {hours:.1f}h · {len(raw)} readings")
        if note:
            subtitle += f" · {note}"
        summary_line = f"{len(raw)} readings, {label.lower()}"

    else:
        # Zoom levels rather than a list of dates: 24 hours, 5 days, a month,
        # six months, all time -- plus individual nights, which are the unit an
        # experiment happens in. Ranges appear only once there is data to fill
        # them, so the list grows with the history instead of starting cluttered.
        offered = available_ranges(frame)
        labels = night_labels()
        night_offered, night_windows, night_counts = [], {}, {}
        for key, lab, span in available_night_ranges(frame):
            scoped = frame if span is None else rolling_window(frame, span)
            window = night_only(frame, span)
            found = nights(scoped, 500)
            if window.empty:
                continue
            night_offered.append((key, lab, span))
            night_windows[key] = window
            night_counts[key] = len(found)

        windows = {key: rolling_window(frame, span) for key, _, span in offered}
        windows[ALLTIME_KEY] = frame

        active_key = offered[0][0]
        if args.all or (args.date or "").lower() in ("alltime", "all"):
            active_key = ALLTIME_KEY
        elif args.date:
            match = [key for key, _, _ in offered if key == args.date.lower()]
            if not match:
                choices = ", ".join([key for key, _, _ in offered] + [ALLTIME_KEY])
                print(f"unknown --date {args.date!r}; try one of: {choices}")
                return 1
            active_key = match[0]

        if args.night is not None:
            if not night_offered:
                print(f"no nights with enough data yet (need {MIN_NIGHT_READINGS} readings "
                      f"over {MIN_NIGHT_HOURS:g}h between {config.NIGHT_START_HOUR:02d}:00 "
                      f"and {config.NIGHT_END_HOUR:02d}:00)")
                return 1
            keys = [k for k, _, _ in night_offered]
            wanted = (args.night or "").strip().lower()
            if not wanted:
                active_key = keys[0]
            elif wanted in keys:
                active_key = wanted
            else:
                print(f"unknown --night {args.night!r}; try one of: {', '.join(keys)}")
                return 1

        panes = "".join(range_pane(windows[key], key, active_key, active)
                        for key, _, _ in offered)
        panes += range_pane(frame, ALLTIME_KEY, active_key, active)
        for key, _, span in night_offered:
            scoped = frame if span is None else rolling_window(frame, span)
            found = nights(scoped, 500)
            table = (report.section("Nights compared", nights_table(found, labels))
                     if len(found) > 1 else "")
            panes += range_pane(night_windows[key], key, active_key, active, extra=table)

        groups = [
            ("Range", [(key, label) for key, label, _ in offered]
                      + [(ALLTIME_KEY, ALLTIME_LABEL)]),
            ("Nights", [(key, f"{lab} ({night_counts[key]})")
                        for key, lab, _ in night_offered]),
        ]
        body = report.day_bar(
            groups, active_key,
            meta=f"{night_counts.get('nall', 0)} night(s) · {len(frame)} readings") + panes

        lookup = dict(windows)
        lookup.update(night_windows)
        chosen = lookup[active_key]
        label = dict(
            [(key, lab) for key, lab, _ in offered]
            + [(ALLTIME_KEY, ALLTIME_LABEL)]
            + [(key, lab) for key, lab, _ in night_offered]
        )[active_key]
        subtitle = (f"{pane_span(chosen)} local · {len(chosen)} readings"
                    f" · {len(frame)} in total")
        summary_line = f"{len(chosen)} readings, {label.lower()}"

    html_text = report.page(
        title=f"air-home — {room}" if not single_window else f"air-home — {room} — {label}",
        tabs=report.room_tabs(names, room, lambda r: report_path(r, args.output).name),
        subtitle=subtitle,
        body=body,
        footer=f"Generated {datetime.now(LOCAL_TZ).strftime('%Y-%m-%d %H:%M')} local "
               f"(UTC{config.LOCAL_UTC_OFFSET:+g}) from {config.DB_PATH.name}",
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    room_file = report_path(room, args.output)
    room_file.write_text(html_text, encoding="utf-8")
    # report.html stays the entry point and holds whichever room was chosen.
    if not args.no_index:
        args.output.write_text(html_text, encoding="utf-8")
    if not args.quiet:
        print(f"wrote {room_file if args.no_index else args.output}"
              f"  [{room}] {summary_line}")

    if args.open:
        webbrowser.open(args.output.resolve().as_uri())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
