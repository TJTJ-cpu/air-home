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
import sqlite3
import webbrowser
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

import config
import report

# field, label, unit, decimal places, threshold, threshold label
METRICS = [
    ("co2_ppm", "CO₂", " ppm", 0, 1000, "1000 ppm"),
    ("temperature_c", "Temperature", " °C", 1, None, ""),
    ("humidity_pct", "Humidity", " %", 0, None, ""),
    ("aqi", "AQI", "", 0, None, ""),
    ("pm25", "PM2.5", " µg/m³", 0, None, ""),
    ("pm10", "PM10", " µg/m³", 0, None, ""),
]
FIELDS = [field for field, *_ in METRICS]
# Column headers carry the unit, so a table cell never needs one.
METRIC_COLUMNS = [(field, f"{label}{unit}".strip())
                  for field, label, unit, *_ in METRICS]

LOCAL_TZ = timezone(timedelta(hours=config.LOCAL_UTC_OFFSET))
MAX_POINTS = 480  # beyond this an SVG line is denser than the screen can show
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


def load(db_path: Path) -> pd.DataFrame:
    """Every reading, indexed by local time."""
    with sqlite3.connect(db_path) as conn:
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
                          threshold, threshold_label)
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
                                  width=FOCUS_WIDTH, plot_h=FOCUS_PLOT_H)
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--last", help="a span back from the newest reading: 30m, 6h, 2d")
    parser.add_argument("--today", action="store_true", help="since local midnight")
    parser.add_argument("--night", nargs="?", const="", metavar="DATE",
                        help="a night window; bare, or a YYYY-MM-DD start date")
    parser.add_argument("--from", dest="start", help="window start, local time")
    parser.add_argument("--to", dest="end", help="window end, local time")
    parser.add_argument("--every", metavar="SPAN",
                        help="average the charts into buckets: 5min, 1h, 1d")
    parser.add_argument("--all", action="store_true", help="everything (the default)")
    parser.add_argument("--metric", default="all", metavar="NAME",
                        help="open focused on one metric: co2, temp, humidity, "
                             "aqi, pm25, pm10")
    parser.add_argument("--output", type=Path, default=config.DATA / "report.html")
    parser.add_argument("--open", action="store_true", help="open in the browser when done")
    args = parser.parse_args()

    active = METRIC_ALIASES.get(args.metric.strip().lower())
    if active is None:
        print(f"unknown --metric {args.metric!r}; try one of: "
              f"{', '.join(sorted(set(METRIC_ALIASES) - {'all'}))}")
        return 1

    if not config.DB_PATH.exists():
        print(f"no database at {config.DB_PATH} -- run process.py first")
        return 1

    frame = load(config.DB_PATH)
    if frame.empty:
        print("no readings yet -- run process.py first")
        return 1

    window, label = choose_window(frame, args)
    if window.empty:
        print("no readings in that window")
        return 1

    # Tables summarise the real readings; only the charts get averaged down.
    raw = window
    if args.every:
        try:
            rule = pd.Timedelta(args.every)
        except ValueError:
            print(f"bad --every value {args.every!r}; try 5min, 1h, 1d")
            return 1
        charted, note = bucket(window, rule)[0], f"charts averaged every {args.every}"
    else:
        charted, note = auto_bucket(window)

    first, last = raw.index.min(), raw.index.max()
    hours = (last - first).total_seconds() / 3600
    subtitle = (f"{first.strftime('%d %b %H:%M')} → {last.strftime('%d %b %H:%M')} local"
                f" · {hours:.1f}h · {len(raw)} readings")
    if note:
        subtitle += f" · {note}"

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

    html_text = report.page(
        title=f"air-home — {label}",
        subtitle=subtitle,
        tiles=build_tiles(raw),
        charts=build_filters(charted, active) + build_charts(charted, active)
               + build_focus(charted, active),
        sections="".join(sections),
        footer=f"Generated {datetime.now(LOCAL_TZ).strftime('%Y-%m-%d %H:%M')} local "
               f"(UTC{config.LOCAL_UTC_OFFSET:+g}) from {config.DB_PATH.name}",
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(html_text, encoding="utf-8")
    print(f"wrote {args.output}  ({len(raw)} readings, {label.lower()})")

    if args.open:
        webbrowser.open(args.output.resolve().as_uri())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
