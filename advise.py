"""Write a plain-English report on how the air has been, ready to save as PDF.

The numbers are all worked out here, in Python. The model is only asked to
interpret a summary it is handed -- it never sees raw readings and never does
arithmetic, so it cannot get a figure wrong. If it is unavailable, the report
still gets written with the numbers and no commentary.

    python advise.py                     # last week, most recent room
    python advise.py --last 3d --open
    python advise.py --room tj --last 5d
    python advise.py --all-ranges        # 3d, 5d and 1w in one go
"""
from __future__ import annotations

import argparse
import html
import sys
import webbrowser
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

import analyze
import config
import store
from extract import ExtractionError, ask

LOCAL_TZ = timezone(timedelta(hours=config.LOCAL_UTC_OFFSET))

RANGES = {
    "3d": ("Last 3 days", pd.Timedelta(days=3)),
    "5d": ("Last 5 days", pd.Timedelta(days=5)),
    "1w": ("Last week", pd.Timedelta(days=7)),
}

SYSTEM = """\
You advise a household on the air in their bedrooms. You are given a summary of \
measurements that have already been calculated for you.

Write for someone with no technical background. Be specific and practical: name \
the thing to change and when to do it. Never invent a number -- use only the \
figures given, and if something is not in the summary, do not mention it.

Useful context: outdoor air is about 400 ppm CO2. In a closed bedroom CO2 rises \
overnight because sleeping people exhale it, and it falls once a door or window \
opens. Above roughly 1000 ppm suggests the room is not getting enough fresh air; \
above 1400 it is stuffy enough that people often report waking unrested. High \
CO2 signals poor ventilation rather than direct harm. Opening a door or window, \
or running a fan that pushes air out of the room, all lower it.\
"""

SCHEMA = {
    "name": "air_advice",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "verdict": {"type": "string",
                        "description": "One sentence: how the air has been overall."},
            "sleep": {"type": "string",
                      "description": "Two or three sentences on the nights specifically."},
            "good": {"type": "array", "items": {"type": "string"},
                     "description": "Two or three things that are going well."},
            "improve": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "issue": {"type": "string"},
                        "why": {"type": "string"},
                        "action": {"type": "string"},
                    },
                    "required": ["issue", "why", "action"],
                    "additionalProperties": False,
                },
                "description": "Two to four things to change, most useful first.",
            },
        },
        "required": ["verdict", "sleep", "good", "improve"],
        "additionalProperties": False,
    },
}


def facts(frame: pd.DataFrame, room: str, label: str) -> dict:
    """Everything the report states, computed here rather than by the model."""
    nights = analyze.nights(frame, 30)
    per_night = []
    for evening, subset in reversed(nights):
        stats = analyze.night_stats(subset)
        per_night.append({
            "night": evening.strftime("%a %d %b"),
            "start_ppm": stats["start"], "peak_ppm": stats["peak"],
            "peak_at": stats["peak_at"].strftime("%H:%M") if stats["peak_at"] is not None else None,
            "mean_ppm": stats["mean"], "rise_ppm_per_hour": stats["rise"],
            "hours_above_1000": stats["above"],
        })

    span_hours = (frame.index.max() - frame.index.min()).total_seconds() / 3600
    co2 = frame["co2_ppm"].dropna()
    out = {
        "room": room, "period": label, "readings": len(frame),
        "hours_covered": round(span_hours, 1), "nights": per_night,
        "co2": {
            "mean": round(float(co2.mean()), 0) if len(co2) else None,
            "peak": round(float(co2.max()), 0) if len(co2) else None,
            "hours_above_1000": round(float((co2 > 1000).mean()) * span_hours, 1) if len(co2) else 0,
            "hours_above_1400": round(float((co2 > 1400).mean()) * span_hours, 1) if len(co2) else 0,
        },
    }
    for field, key in (("temperature_c", "temperature_c"), ("humidity_pct", "humidity_pct"),
                       ("pm25", "pm25"), ("pm10", "pm10")):
        series = frame[field].dropna()
        if len(series):
            out[key] = {"mean": round(float(series.mean()), 1),
                        "min": round(float(series.min()), 1),
                        "max": round(float(series.max()), 1)}
    labels = analyze.night_labels()
    noted = {night["night"]: labels[key] for key, night in
             zip([e.isoformat() for e, _ in reversed(nights)], per_night) if labels.get(key)}
    if noted:
        out["what_you_changed"] = noted
    return out


def advice_for(summary: dict) -> tuple[dict | None, str | None]:
    """Ask the model to interpret the summary. Returns (advice, error)."""
    import json

    question = ("Here is the summary. Write the report.\n\n"
                + json.dumps(summary, indent=1, default=str))
    try:
        result = ask(SYSTEM, question, SCHEMA)
    except ExtractionError as exc:
        return None, str(exc)
    except Exception as exc:
        return None, f"the model returned something unusable ({exc})"
    if not isinstance(result, dict) or "verdict" not in result:
        return None, "the model did not answer in the expected shape"
    return result, None


STYLE = """
:root {
  color-scheme: light;
  --paper:#ffffff; --ink:#16191d; --ink-2:#414952; --muted:#6b7480;
  --rule:#dfe3e7; --wash:#f5f7f8;
  --good:#0b7a3b; --warn:#b06a00; --bad:#b3271e; --accent:#1d4ed8;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    color-scheme: dark;
    --paper:#14171a; --ink:#eef1f4; --ink-2:#c2c9d1; --muted:#8b949e;
    --rule:#2a2f35; --wash:#1b1f23;
    --good:#3fbc6d; --warn:#e0a33c; --bad:#e8695e; --accent:#6aa1ff;
  }
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --paper:#14171a; --ink:#eef1f4; --ink-2:#c2c9d1; --muted:#8b949e;
  --rule:#2a2f35; --wash:#1b1f23;
  --good:#3fbc6d; --warn:#e0a33c; --bad:#e8695e; --accent:#6aa1ff;
}
* { box-sizing:border-box; }
body { margin:0; background:var(--paper); color:var(--ink);
  font:16px/1.6 system-ui,-apple-system,"Segoe UI",sans-serif; }
.sheet { max-width:44rem; margin:0 auto; padding:2.5rem 1.5rem 4rem; }
.top { display:flex; justify-content:space-between; align-items:flex-start;
  gap:1rem; border-bottom:2px solid var(--ink); padding-bottom:1rem; }
h1 { font-size:1.6rem; margin:.2rem 0 .3rem; letter-spacing:-.02em; }
.meta { color:var(--muted); font-size:.85rem; }
.eyebrow { font-size:.7rem; letter-spacing:.12em; text-transform:uppercase;
  color:var(--muted); font-weight:600; }
button { font:inherit; font-size:.85rem; padding:.5rem 1rem; border-radius:8px;
  border:1px solid var(--rule); background:var(--wash); color:var(--ink);
  cursor:pointer; white-space:nowrap; }
button:hover { border-color:var(--ink); }
.verdict { font-size:1.25rem; line-height:1.45; margin:1.6rem 0 0;
  padding-left:1rem; border-left:3px solid var(--accent); }
h2 { font-size:1.02rem; margin:2.2rem 0 .6rem;
  padding-bottom:.35rem; border-bottom:1px solid var(--rule); }
p { margin:0 0 .9rem; }
ul { margin:0 0 .9rem; padding-left:1.2rem; }
li { margin-bottom:.45rem; }
li::marker { color:var(--muted); }
.fix { border:1px solid var(--rule); border-radius:10px; padding:.9rem 1.1rem;
  margin-bottom:.7rem; background:var(--wash); }
.fix h3 { margin:0 0 .35rem; font-size:1rem; color:var(--bad); }
.fix .why { color:var(--ink-2); margin:0 0 .5rem; font-size:.95rem; }
.fix .do { margin:0; font-weight:600; }
.fix .do span { font-weight:400; color:var(--muted); }
table { border-collapse:collapse; width:100%; font-size:.88rem;
  font-variant-numeric:tabular-nums; }
th,td { text-align:right; padding:.42rem .6rem; border-bottom:1px solid var(--rule); }
th:first-child, td:first-child { text-align:left; }
th { color:var(--muted); font-weight:600; font-size:.78rem; }
.scroll { overflow-x:auto; }
.hi { color:var(--bad); font-weight:600; }
.ok { color:var(--good); font-weight:600; }
footer { margin-top:2.5rem; padding-top:1rem; border-top:1px solid var(--rule);
  color:var(--muted); font-size:.8rem; }
.note { background:var(--wash); border:1px solid var(--rule); border-radius:10px;
  padding:.9rem 1.1rem; color:var(--ink-2); font-size:.92rem; }
@media print {
  @page { margin:16mm; }
  body { background:#fff; color:#000; font-size:11.5pt; }
  .sheet { max-width:none; padding:0; }
  .noprint { display:none !important; }
  h2 { break-after:avoid; }
  .fix, tr, li { break-inside:avoid; }
  a { text-decoration:none; color:#000; }
}
"""


def row_class(value, warn, bad) -> str:
    if value is None:
        return ""
    if value >= bad:
        return ' class="hi"'
    if value >= warn:
        return ""
    return ' class="ok"'


def render(summary: dict, advice: dict | None, error: str | None) -> str:
    e = html.escape
    nights = summary["nights"]
    rows = []
    for n in nights:
        def cell(value, fmt="{:.0f}", klass=""):
            return f"<td{klass}>{fmt.format(value)}</td>" if value is not None else "<td>-</td>"
        peak = n["peak_ppm"]
        rows.append(
            "<tr>"
            + f"<td>{e(n['night'])}</td>"
            + cell(n["start_ppm"])
            + cell(peak, klass=row_class(peak, 1000, 1400))
            + f"<td>{e(n['peak_at'] or '-')}</td>"
            + cell(n["mean_ppm"])
            + cell(n["rise_ppm_per_hour"], "{:+.0f}")
            + cell(n["hours_above_1000"], "{:.1f}")
            + "</tr>"
        )
    night_table = (
        '<div class="scroll"><table><thead><tr>'
        "<th>Night</th><th>Start</th><th>Peak</th><th>Peak at</th>"
        "<th>Average</th><th>Rise/h</th><th>Hours &gt;1000</th>"
        "</tr></thead><tbody>" + "".join(rows) + "</tbody></table></div>"
        if rows else '<p class="note">No complete nights in this period yet.</p>'
    )

    co2 = summary["co2"]
    def band(key, fmt="{:.1f}"):
        block = summary.get(key)
        if not block:
            return "<td>-</td><td>-</td><td>-</td>"
        return (f"<td>{fmt.format(block['mean'])}</td><td>{fmt.format(block['min'])}</td>"
                f"<td>{fmt.format(block['max'])}</td>")
    numbers = (
        '<div class="scroll"><table><thead><tr><th>Measurement</th>'
        "<th>Average</th><th>Lowest</th><th>Highest</th></tr></thead><tbody>"
        f"<tr><td>CO&#8322; (ppm)</td><td>{co2['mean']:.0f}</td><td>-</td>"
        f"<td{row_class(co2['peak'], 1000, 1400)}>{co2['peak']:.0f}</td></tr>"
        f"<tr><td>Temperature (&deg;C)</td>{band('temperature_c')}</tr>"
        f"<tr><td>Humidity (%)</td>{band('humidity_pct')}</tr>"
        f"<tr><td>PM2.5 (&micro;g/m&sup3;)</td>{band('pm25')}</tr>"
        f"<tr><td>PM10 (&micro;g/m&sup3;)</td>{band('pm10')}</tr>"
        "</tbody></table></div>"
    )

    if advice:
        good = "".join(f"<li>{e(item)}</li>" for item in advice.get("good", []))
        fixes = "".join(
            f'<div class="fix"><h3>{e(fix["issue"])}</h3>'
            f'<p class="why">{e(fix["why"])}</p>'
            f'<p class="do">Do this: <span>{e(fix["action"])}</span></p></div>'
            for fix in advice.get("improve", [])
        )
        body = (
            f'<p class="verdict">{e(advice["verdict"])}</p>'
            f"<h2>How you slept</h2><p>{e(advice['sleep'])}</p>"
            + (f"<h2>What is going well</h2><ul>{good}</ul>" if good else "")
            + (f"<h2>What to improve</h2>{fixes}" if fixes else "")
        )
    else:
        body = ('<div class="note"><strong>No written advice this time.</strong><br>'
                f"{e(error or 'the model was unavailable')}<br>"
                "The measurements below are unaffected.</div>")

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="cache-control" content="no-cache, no-store, must-revalidate">
<title>Air report &mdash; {e(summary['room'])} &mdash; {e(summary['period'])}</title>
<style>{STYLE}</style>
</head>
<body>
<div class="sheet">
  <div class="top">
    <div>
      <div class="eyebrow">Air report &middot; {e(summary['room'])}</div>
      <h1>{e(summary['period'])}</h1>
      <div class="meta">{summary['readings']} readings over
        {summary['hours_covered']:.0f} hours &middot; {len(nights)} full night(s)</div>
    </div>
    <button class="noprint" onclick="window.print()">Save as PDF</button>
  </div>
  {body}
  <h2>The nights</h2>
  {night_table}
  <h2>The numbers</h2>
  {numbers}
  <footer>
    Generated {datetime.now(LOCAL_TZ):%d %b %Y %H:%M} local from
    {summary['readings']} readings taken by air-home.
    Every figure above was calculated from the measurements; the written
    sections are an interpretation of those figures by a local language model
    and should be read as suggestions, not medical advice.
  </footer>
</div>
</body>
</html>
"""


def build(room: str, key: str, output: Path, quiet: bool = False) -> Path | None:
    label, span = RANGES[key]
    frame = analyze.load(config.DB_PATH, room)
    if frame.empty:
        print(f"[{room}] no readings")
        return None
    window = analyze.rolling_window(frame, span)
    if window.empty:
        print(f"[{room}] nothing in the {label.lower()}")
        return None

    summary = facts(window, room, label)
    advice, error = advice_for(summary)
    if error and not quiet:
        print(f"  no written advice: {error}")
    path = output.with_name(f"advice-{room}-{key}.html")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(summary, advice, error), encoding="utf-8")
    if not quiet:
        print(f"wrote {path}  [{room}] {label.lower()}, {len(window)} readings")
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--room", help="which room (default: the most recent)")
    parser.add_argument("--last", default="1w", choices=list(RANGES),
                        help="how far back: 3d, 5d, 1w. Default 1w")
    parser.add_argument("--all-ranges", action="store_true",
                        help="build all three ranges")
    parser.add_argument("--output", type=Path, default=config.REPORTS / "report.html",
                        help="where the reports live (advice files sit beside it)")
    parser.add_argument("--open", action="store_true", help="open it when done")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    if not config.DB_PATH.exists():
        print(f"no database at {config.DB_PATH} -- run process.py first", file=sys.stderr)
        return 1

    with store.connect() as conn:
        names = [row["room"] for row in store.rooms(conn)]
        if args.room:
            room = config.room_name(args.room)
            if room not in names:
                print(f"no readings for room {room!r}. Rooms: {', '.join(names) or 'none'}")
                return 1
        else:
            room = store.latest_room(conn)
    if not room:
        print("no readings yet")
        return 1

    keys = list(RANGES) if args.all_ranges else [args.last]
    written = [build(room, key, args.output, args.quiet) for key in keys]
    written = [p for p in written if p]
    if not written:
        return 1
    if args.open:
        webbrowser.open(written[0].resolve().as_uri())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
