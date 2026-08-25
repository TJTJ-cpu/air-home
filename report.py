"""Render readings as a self-contained HTML report.

No CDN, no external assets -- one file you can open, move, or email. Charts are
inline SVG: one small multiple per metric, because the metrics have wildly
different scales and putting two of them on one plot with two y-axes would
invent a correlation that is not in the data.
"""
from __future__ import annotations

import html
import json
import math
from datetime import datetime

# Palette roles. Light values on :root, dark values redefined under both the
# OS media query and an explicit data-theme stamp so a toggle wins either way.
STYLE = """
:root {
  color-scheme: light;
  --page:        #f9f9f7;
  --surface:     #fcfcfb;
  --ink:         #0b0b0b;
  --ink-2:       #52514e;
  --muted:       #898781;
  --grid:        #e1e0d9;
  --axis:        #c3c2b7;
  --border:      rgba(11,11,11,0.10);
  --series-1:    #2a78d6;
  --warning:     #fab219;
  --critical:    #d03b3b;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    color-scheme: dark;
    --page:      #0d0d0d;
    --surface:   #1a1a19;
    --ink:       #ffffff;
    --ink-2:     #c3c2b7;
    --muted:     #898781;
    --grid:      #2c2c2a;
    --axis:      #383835;
    --border:    rgba(255,255,255,0.10);
    --series-1:  #3987e5;
  }
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --page:        #0d0d0d;
  --surface:     #1a1a19;
  --ink:         #ffffff;
  --ink-2:       #c3c2b7;
  --muted:       #898781;
  --grid:        #2c2c2a;
  --axis:        #383835;
  --border:      rgba(255,255,255,0.10);
  --series-1:    #3987e5;
}

* { box-sizing: border-box; }
body {
  margin: 0;
  padding: 32px 24px 64px;
  background: var(--page);
  color: var(--ink);
  font: 15px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif;
}
.wrap { max-width: 1100px; margin: 0 auto; }

header { margin-bottom: 28px; }
h1 { font-size: 22px; font-weight: 600; margin: 0 0 4px; letter-spacing: -0.01em; }
.sub { color: var(--ink-2); font-size: 14px; margin: 0; }

.tiles {
  display: grid; gap: 12px; margin: 24px 0 28px;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
}
.tile {
  background: var(--surface); border: 1px solid var(--border);
  border-radius: 10px; padding: 14px 16px;
}
.tile .label { font-size: 12px; color: var(--muted); text-transform: uppercase;
  letter-spacing: 0.04em; margin-bottom: 6px; }
.tile .value { font-size: 30px; font-weight: 600; line-height: 1.1;
  letter-spacing: -0.02em; }
.tile .value .unit { font-size: 15px; font-weight: 400; color: var(--ink-2);
  margin-left: 3px; letter-spacing: 0; }
.tile .note { font-size: 13px; color: var(--ink-2); margin-top: 4px; }

.daybar { display: flex; align-items: center; gap: 8px; margin: 0 0 18px; flex-wrap: wrap; }
.daybar select {
  font: inherit; font-size: 14px; padding: 6px 10px; border-radius: 8px;
  border: 1px solid var(--border); background: var(--surface); color: var(--ink);
}
.daybar button {
  font: inherit; font-size: 15px; line-height: 1; width: 32px; height: 32px;
  border-radius: 8px; border: 1px solid var(--border);
  background: var(--surface); color: var(--ink-2); cursor: pointer;
}
.daybar button:hover:not(:disabled) { color: var(--ink); }
.daybar button:disabled { opacity: 0.35; cursor: default; }
.daymeta { font-size: 13px; color: var(--muted); }
.day { display: none; }
.day.on { display: block; }

.filters { display: flex; flex-wrap: wrap; gap: 6px; margin: 0 0 14px; }
.filters button {
  font: inherit; font-size: 13px; padding: 5px 13px; border-radius: 999px;
  border: 1px solid var(--border); background: var(--surface);
  color: var(--ink-2); cursor: pointer;
}
.filters button:hover { color: var(--ink); }
/* Active pill inverts the ink token rather than wearing the series color, so
   it stays legible in both themes. */
.filters button[aria-pressed="true"] {
  background: var(--ink); border-color: var(--ink); color: var(--page);
}
.charts { display: grid; gap: 14px; grid-template-columns: repeat(auto-fit, minmax(330px, 1fr)); }
.charts.off { display: none; }
.focus { display: none; }
.focus.on { display: block; }
.chart {
  position: relative; margin: 0; background: var(--surface);
  border: 1px solid var(--border); border-radius: 10px; padding: 14px 14px 8px;
}
.chart figcaption { display: flex; justify-content: space-between; align-items: baseline;
  margin-bottom: 8px; gap: 12px; }
.chart .name { font-size: 14px; font-weight: 600; }
.chart .range { font-size: 12px; color: var(--muted); font-variant-numeric: tabular-nums; }
.chart svg { display: block; width: 100%; height: auto; overflow: visible; }

.grid-line { stroke: var(--grid); stroke-width: 1; }
.axis-line { stroke: var(--axis); stroke-width: 1; }
.tick { fill: var(--muted); font-size: 10px; font-variant-numeric: tabular-nums; }
.line { fill: none; stroke: var(--series-1); stroke-width: 2;
  stroke-linejoin: round; stroke-linecap: round; }
.threshold { stroke: var(--critical); stroke-width: 1; stroke-dasharray: 4 4; opacity: 0.8; }
.threshold-label { fill: var(--critical); font-size: 10px; }
.peak-dot { fill: var(--series-1); stroke: var(--surface); stroke-width: 2; }
.peak-label { fill: var(--ink); font-size: 11px; font-weight: 600; }
.crosshair { stroke: var(--axis); stroke-width: 1; opacity: 0; }
.cursor-dot { fill: var(--series-1); stroke: var(--surface); stroke-width: 2; opacity: 0; }
.hit { fill: transparent; }

.tip {
  position: absolute; pointer-events: none; opacity: 0; transition: opacity .08s;
  background: var(--surface); border: 1px solid var(--border); border-radius: 6px;
  padding: 5px 8px; font-size: 12px; white-space: nowrap;
  box-shadow: 0 2px 8px rgba(0,0,0,0.10); color: var(--ink);
}
.tip b { font-variant-numeric: tabular-nums; }

details, .panel { margin-top: 16px; background: var(--surface);
  border: 1px solid var(--border); border-radius: 10px; padding: 12px 16px; }
.panel h2 { font-size: 14px; font-weight: 600; margin: 2px 0 0; }
.charts + .panel, .charts + details { margin-top: 22px; }
summary { cursor: pointer; font-size: 14px; font-weight: 600; }
.scroll { overflow-x: auto; margin-top: 12px; }
table { border-collapse: collapse; width: 100%; font-size: 13px;
  font-variant-numeric: tabular-nums; }
th, td { text-align: right; padding: 5px 10px; border-bottom: 1px solid var(--grid);
  white-space: nowrap; }
th:first-child, td:first-child { text-align: left; }
th { color: var(--muted); font-weight: 500; font-size: 12px; }
.empty { color: var(--ink-2); background: var(--surface); border: 1px solid var(--border);
  border-radius: 10px; padding: 20px; }
footer { margin-top: 28px; color: var(--muted); font-size: 12px; }
"""

SCRIPT = """
document.querySelectorAll('.chart').forEach(function (fig) {
  var svg = fig.querySelector('svg');
  var data = JSON.parse(fig.dataset.series);
  if (!data.points.length) return;
  var tip = fig.querySelector('.tip');
  var cross = fig.querySelector('.crosshair');
  var dot = fig.querySelector('.cursor-dot');
  var hit = fig.querySelector('.hit');

  function show(evt) {
    var box = svg.getBoundingClientRect();
    var scale = data.width / box.width;
    var x = (evt.clientX - box.left) * scale;
    var best = null, bestDist = Infinity;
    for (var i = 0; i < data.points.length; i++) {
      var d = Math.abs(data.points[i].x - x);
      if (d < bestDist) { bestDist = d; best = data.points[i]; }
    }
    if (!best) return;
    cross.setAttribute('x1', best.x); cross.setAttribute('x2', best.x);
    cross.style.opacity = 1;
    dot.setAttribute('cx', best.x); dot.setAttribute('cy', best.y);
    dot.style.opacity = 1;
    tip.innerHTML = best.t + ' &middot; <b>' + best.v + data.unit + '</b>';
    tip.style.opacity = 1;
    var left = best.x / scale + 12;
    if (left + tip.offsetWidth > box.width) left = best.x / scale - tip.offsetWidth - 12;
    tip.style.left = Math.max(0, left) + 'px';
    tip.style.top = (best.y / scale) + 'px';
  }
  function hide() {
    cross.style.opacity = 0; dot.style.opacity = 0; tip.style.opacity = 0;
  }
  hit.addEventListener('mousemove', show);
  hit.addEventListener('mouseleave', hide);
  hit.addEventListener('touchmove', function (e) { show(e.touches[0]); }, {passive: true});
});

// Each day pane carries its own metric selector, so scope the lookup to the
// pane rather than the document -- otherwise every day would share one state.
document.querySelectorAll('.filters').forEach(function (filters) {
  var scope = filters.closest('.day') || document;
  filters.addEventListener('click', function (evt) {
    var button = evt.target.closest('button');
    if (!button) return;
    var wanted = button.dataset.metric;
    filters.querySelectorAll('button').forEach(function (other) {
      other.setAttribute('aria-pressed', other === button ? 'true' : 'false');
    });
    var grid = scope.querySelector('.charts');
    var focus = scope.querySelector('.focus');
    grid.classList.toggle('off', wanted !== 'all');
    focus.classList.toggle('on', wanted !== 'all');
    focus.querySelectorAll('[data-metric]').forEach(function (panel) {
      panel.style.display = panel.dataset.metric === wanted ? '' : 'none';
    });
  });
});

var daybar = document.querySelector('.daybar');
if (daybar) {
  var picker = daybar.querySelector('select');
  var panes = document.querySelectorAll('.day');
  var stepButtons = daybar.querySelectorAll('button[data-step]');

  function showDay(value) {
    panes.forEach(function (pane) {
      pane.classList.toggle('on', pane.dataset.day === value);
    });
    picker.value = value;
    stepButtons.forEach(function (button) {
      var target = picker.selectedIndex + Number(button.dataset.step);
      button.disabled = target < 0 || target >= picker.options.length;
    });
  }

  picker.addEventListener('change', function () { showDay(picker.value); });
  stepButtons.forEach(function (button) {
    button.addEventListener('click', function () {
      var target = picker.selectedIndex + Number(button.dataset.step);
      if (target >= 0 && target < picker.options.length) {
        showDay(picker.options[target].value);
      }
    });
  });
  showDay(picker.value);
}
"""

WIDTH = 460
PLOT_H = 150
PAD_L, PAD_R, PAD_T, PAD_B = 46, 14, 12, 26
HEIGHT = PLOT_H + PAD_T + PAD_B


def _nice_step(span: float, target: int) -> float:
    # 1/2/5 only: a 2.5 step yields ticks like 27.25 that collapse to a
    # duplicate or uneven label once rounded for display.
    if span <= 0:
        return 1.0
    raw = span / target
    mag = 10 ** math.floor(math.log10(raw))
    for multiple in (1, 2, 5, 10):
        if raw <= mag * multiple:
            return mag * multiple
    return mag * 10


def nice_axis(low: float, high: float, target: int = 4,
              integral: bool = False) -> tuple[float, float, list[float]]:
    """Round an axis outward to human-friendly tick values.

    `integral` forces whole-number ticks, for metrics the device only ever
    reports as integers -- otherwise a range like 2..4 produces 2, 2.5, 3, 3.5,
    4, which renders as the duplicate labels 2, 2, 3, 4, 4.
    """
    if low == high:
        low, high = low - 1, high + 1
    step = _nice_step(high - low, target)
    if integral:
        step = max(1.0, round(step))
    start = math.floor(low / step) * step
    end = math.ceil(high / step) * step
    ticks, value = [], start
    while value <= end + step * 1e-9:
        ticks.append(round(value, 10))
        value += step
    return start, end, ticks


def _fmt(value: float, places: int) -> str:
    return f"{value:.{places}f}" if places else f"{round(value):g}"


def line_chart(name: str, unit: str, samples: list[tuple[datetime, float | None]],
               places: int = 0, threshold: float | None = None,
               threshold_label: str = "", width: int = WIDTH,
               plot_h: int = PLOT_H) -> str:
    """One metric over time. Single series, so no legend -- the title names it.

    `width`/`plot_h` are the SVG's own coordinate system, not CSS. A focused
    chart is re-rendered larger rather than scaled up, so its strokes and tick
    labels keep their intended weight instead of growing with the box.
    """
    height = plot_h + PAD_T + PAD_B
    values = [value for _, value in samples if value is not None]
    if not values:
        return (f'<figure class="chart"><figcaption><span class="name">{html.escape(name)}'
                f'</span></figcaption><p class="sub">no readings</p></figure>')

    low, high = min(values), max(values)
    if threshold is not None and low <= threshold <= high * 1.5:
        high = max(high, threshold)
    y0, y1, yticks = nice_axis(low, high, integral=(places == 0))

    times = [moment for moment, _ in samples]
    t0, t1 = times[0].timestamp(), times[-1].timestamp()
    span = (t1 - t0) or 1.0

    def px(moment: datetime) -> float:
        return PAD_L + (moment.timestamp() - t0) / span * (width - PAD_L - PAD_R)

    def py(value: float) -> float:
        return PAD_T + (y1 - value) / ((y1 - y0) or 1) * plot_h

    parts: list[str] = []
    for tick in yticks:
        y = py(tick)
        parts.append(f'<line class="grid-line" x1="{PAD_L}" y1="{y:.1f}" '
                     f'x2="{width - PAD_R}" y2="{y:.1f}"/>')
        parts.append(f'<text class="tick" x="{PAD_L - 8}" y="{y + 3.5:.1f}" '
                     f'text-anchor="end">{_fmt(tick, places)}</text>')

    parts.append(f'<line class="axis-line" x1="{PAD_L}" y1="{PAD_T + plot_h}" '
                 f'x2="{width - PAD_R}" y2="{PAD_T + plot_h}"/>')

    # Four x labels at most, so they never collide.
    count = len(samples)
    for index in range(min(4, count)):
        moment = times[round(index * (count - 1) / max(1, min(4, count) - 1))] if count > 1 else times[0]
        x = px(moment)
        anchor = "start" if index == 0 else ("end" if index == min(4, count) - 1 else "middle")
        parts.append(f'<text class="tick" x="{x:.1f}" y="{PAD_T + plot_h + 15}" '
                     f'text-anchor="{anchor}">{moment.strftime("%H:%M")}</text>')

    if threshold is not None and y0 <= threshold <= y1:
        y = py(threshold)
        parts.append(f'<line class="threshold" x1="{PAD_L}" y1="{y:.1f}" '
                     f'x2="{width - PAD_R}" y2="{y:.1f}"/>')
        if threshold_label:
            parts.append(f'<text class="threshold-label" x="{width - PAD_R}" '
                         f'y="{y - 5:.1f}" text-anchor="end">{html.escape(threshold_label)}</text>')

    # Gaps (unreadable photos) break the line rather than being bridged, so a
    # missing stretch never looks like a measured flat run.
    path, drawing, points = [], False, []
    for moment, value in samples:
        if value is None:
            drawing = False
            continue
        x, y = px(moment), py(value)
        path.append(f'{"M" if not drawing else "L"}{x:.1f} {y:.1f}')
        drawing = True
        points.append({"x": round(x, 1), "y": round(y, 1),
                       "t": moment.strftime("%H:%M"), "v": _fmt(value, places)})
    parts.append(f'<path class="line" d="{" ".join(path)}"/>')

    peak = max((s for s in samples if s[1] is not None), key=lambda s: s[1])
    peak_x, peak_y = px(peak[0]), py(peak[1])
    parts.append(f'<circle class="peak-dot" cx="{peak_x:.1f}" cy="{peak_y:.1f}" r="3.5"/>')
    anchor = "end" if peak_x > width * 0.6 else "start"
    offset = -7 if anchor == "end" else 7
    parts.append(f'<text class="peak-label" x="{peak_x + offset:.1f}" '
                 f'y="{peak_y - 8:.1f}" text-anchor="{anchor}">{_fmt(peak[1], places)}</text>')

    parts.append(f'<line class="crosshair" y1="{PAD_T}" y2="{PAD_T + plot_h}"/>')
    parts.append('<circle class="cursor-dot" r="4"/>')
    parts.append(f'<rect class="hit" x="{PAD_L}" y="{PAD_T}" '
                 f'width="{width - PAD_L - PAD_R}" height="{plot_h}"/>')

    payload = json.dumps({"width": width, "unit": unit, "points": points})
    return (
        f'<figure class="chart" data-series=\'{html.escape(payload, quote=True)}\'>'
        f'<figcaption><span class="name">{html.escape(name)}</span>'
        f'<span class="range">{_fmt(low, places)}&ndash;{_fmt(high, places)}{html.escape(unit)}</span>'
        f'</figcaption>'
        f'<svg viewBox="0 0 {width} {height}" role="img" '
        f'aria-label="{html.escape(name)} over time">{"".join(parts)}</svg>'
        f'<div class="tip"></div></figure>'
    )


def day_bar(groups: list[tuple[str, list[tuple[str, str]]]], active: str,
            meta: str = "") -> str:
    """Older / view dropdown / newer.

    `groups` is [(group name, [(value, label), ...]), ...] in display order,
    newest first, so stepping *down* the list goes back in time -- hence the
    older arrow steps +1 and the newer arrow -1.
    """
    blocks = []
    for name, options in groups:
        if not options:
            continue
        items = "".join(
            f'<option value="{html.escape(value)}"'
            f'{" selected" if value == active else ""}>{html.escape(label)}</option>'
            for value, label in options
        )
        blocks.append(f'<optgroup label="{html.escape(name)}">{items}</optgroup>')
    return (
        '<div class="daybar">'
        '<button type="button" data-step="1" aria-label="Older">&lsaquo;</button>'
        f'<select aria-label="Choose what to show">{"".join(blocks)}</select>'
        '<button type="button" data-step="-1" aria-label="Newer">&rsaquo;</button>'
        f'<span class="daymeta">{html.escape(meta)}</span>'
        '</div>'
    )


def filter_row(options: list[tuple[str, str]], active: str) -> str:
    """One row of metric pills above the charts. `options` is (key, label)."""
    buttons = "".join(
        f'<button type="button" data-metric="{html.escape(key)}" '
        f'aria-pressed="{"true" if key == active else "false"}">'
        f'{html.escape(label)}</button>'
        for key, label in options
    )
    return f'<div class="filters" role="group" aria-label="Choose a metric">{buttons}</div>'


def stat_tile(label: str, value: str, unit: str = "", note: str = "") -> str:
    unit_html = f'<span class="unit">{html.escape(unit)}</span>' if unit else ""
    note_html = f'<div class="note">{html.escape(note)}</div>' if note else ""
    return (f'<div class="tile"><div class="label">{html.escape(label)}</div>'
            f'<div class="value">{html.escape(value)}{unit_html}</div>{note_html}</div>')


def table(rows: list[dict], columns: list[tuple[str, str]]) -> str:
    head = "".join(f"<th>{html.escape(label)}</th>" for _, label in columns)
    body = []
    for row in rows:
        cells = "".join(
            f"<td>{html.escape('-' if row.get(key) is None else str(row[key]))}</td>"
            for key, _ in columns
        )
        body.append(f"<tr>{cells}</tr>")
    return (f'<div class="scroll"><table><thead><tr>{head}</tr></thead>'
            f'<tbody>{"".join(body)}</tbody></table></div>')


def section(title: str, body: str, collapsed: bool | None = None) -> str:
    """A titled block. `collapsed` renders it as a <details> instead."""
    if collapsed is not None:
        state = "" if collapsed else " open"
        return (f'<details{state}><summary>{html.escape(title)}</summary>'
                f'{body}</details>')
    return (f'<section class="panel"><h2>{html.escape(title)}</h2>{body}</section>')


def page(title: str, subtitle: str, body: str, footer: str) -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<style>{STYLE}</style>
</head>
<body>
<div class="wrap">
<header>
  <h1>{html.escape(title)}</h1>
  <p class="sub">{html.escape(subtitle)}</p>
</header>
{body}
<footer>{html.escape(footer)}</footer>
</div>
<script>{SCRIPT}</script>
</body>
</html>
"""
