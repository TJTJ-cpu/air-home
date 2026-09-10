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
import statistics
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
  --good:        #0ca30c;
  --warning:     #fab219;
  --serious:     #ec835a;
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

.rooms {
  display: flex; flex-wrap: wrap; align-items: center; gap: 6px;
  margin: 0 0 20px; padding-bottom: 14px; border-bottom: 1px solid var(--border);
}
.rooms .lead {
  font-size: 11px; letter-spacing: 0.1em; text-transform: uppercase;
  color: var(--muted); margin-right: 4px;
}
.rooms a {
  font-size: 13px; text-decoration: none; padding: 6px 14px; border-radius: 8px;
  border: 1px solid var(--border); background: var(--surface); color: var(--ink-2);
}
.rooms a:hover { color: var(--ink); }
/* The current room inverts the ink token, so it reads in both themes. */
.rooms a[aria-current="page"] {
  background: var(--ink); border-color: var(--ink); color: var(--page);
}
.rooms a:focus-visible { outline: 2px solid var(--series-1); outline-offset: 2px; }
.rooms.advice { border-bottom: none; padding-bottom: 0; margin-bottom: 20px; }
.rooms .hint { font-size: 12px; color: var(--muted); }
.rooms button {
  font: inherit; font-size: 13px; padding: 6px 14px; border-radius: 8px;
  border: 1px solid var(--border); background: var(--surface);
  color: var(--ink-2); cursor: pointer;
}
.rooms button:hover:not(:disabled) { color: var(--ink); }
.rooms button:disabled { opacity: .5; cursor: progress; }
.rooms .status { font-size: 12px; color: var(--muted); margin-left: 4px; }
.rooms code { font-size: 12px; background: var(--grid); padding: 1px 5px; border-radius: 4px; }

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
.cursor-dot { fill: var(--ink); stroke: var(--surface); stroke-width: 2; opacity: 0; }
.hit { fill: transparent; }

.tip {
  position: absolute; pointer-events: none; opacity: 0; transition: opacity .08s;
  background: var(--surface); border: 1px solid var(--border); border-radius: 6px;
  padding: 5px 8px; font-size: 12px; white-space: nowrap;
  box-shadow: 0 2px 8px rgba(0,0,0,0.10); color: var(--ink);
}
.tip b { font-variant-numeric: tabular-nums; }

.bands { display: flex; flex-wrap: wrap; gap: 3px 10px; margin: 6px 2px 2px;
  font-size: 10.5px; color: var(--muted); font-variant-numeric: tabular-nums; }
.bands span { display: flex; align-items: center; gap: 4px; }
.bands i { width: 9px; height: 3px; border-radius: 2px; flex: none; }

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
/* A change column is context for the number beside it, not a
   measurement of its own, so it stays quieter. */
td.delta { color: var(--ink-2); font-size: 12px; }
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

var advice = document.querySelector('.rooms.advice');
if (advice) {
  var status = advice.querySelector('.status');
  var servable = location.protocol === 'http:' || location.protocol === 'https:';
  advice.addEventListener('click', function (evt) {
    var button = evt.target.closest('button[data-range]');
    if (!button) return;

    // Served, always rebuild: "last 3 days" must mean the last 3 days as of
    // now, not as of whenever the file happened to be written. Opened from
    // disk there is nothing to rebuild with, so an existing copy is the best
    // answer available.
    if (!servable) {
      if (button.dataset.ready === '1') {
        location.href = button.dataset.file;
        return;
      }
      status.textContent = 'not made yet \u2014 run: python advise.py --room '
        + advice.dataset.room + ' --all-ranges  (or python serve.py to do it here)';
      return;
    }

    var buttons = advice.querySelectorAll('button[data-range]');
    buttons.forEach(function (other) { other.disabled = true; });
    status.textContent = 'asking the AI\u2026 this takes a moment';
    fetch('/advise', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({room: advice.dataset.room, range: button.dataset.range})
    }).then(function (response) {
      return response.json().then(function (data) {
        if (!response.ok) throw new Error(data.error || response.statusText);
        return data;
      });
    }).then(function (data) {
      status.textContent = '';
      location.href = data.url;
    }).catch(function (err) {
      status.textContent = 'could not write it: ' + err.message;
      buttons.forEach(function (other) { other.disabled = false; });
    });
  });
}

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

# A gap up to this many sampling intervals is bridged with a straight line --
# one or two missed readings should not shatter the line into fragments. A
# longer gap (the camera was off for hours) still breaks it, because drawing a
# straight line across that would invent data that was never measured.
GAP_BRIDGE_INTERVALS = 6

_GRADIENT_N = 0  # gradient ids must be unique across a page of many charts


def band_key(bands, unit: str, places: int) -> str:
    """A small legend, so the colour is never the only thing carrying meaning."""
    if not bands:
        return ""
    chips, lower = [], None
    for upper, role in bands:
        if upper is None:
            text = f"&gt;{_fmt(lower, places)}"
        elif lower is None:
            text = f"&le;{_fmt(upper, places)}"
        else:
            text = f"{_fmt(lower, places)}&ndash;{_fmt(upper, places)}"
        chips.append(f'<span><i style="background:var(--{role})"></i>{text}</span>')
        lower = upper
    body = "".join(chips)
    tail = f'<span>{html.escape(unit.strip())}</span>' if unit.strip() else ''
    return f'<div class="bands">{body}{tail}</div>'


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


def _time_formats(span_hours: float) -> tuple[str, str]:
    """Axis and tooltip time formats, chosen by how much time is on screen.

    Over a day, the clock alone is ambiguous -- you need the date to know which
    night a point belongs to.
    """
    if span_hours <= 36:
        return "%H:%M", "%H:%M"
    if span_hours <= 24 * 60:
        return "%d %b", "%d %b %H:%M"
    return "%b %Y", "%d %b %Y"


def _fmt(value: float, places: int) -> str:
    return f"{value:.{places}f}" if places else f"{round(value):g}"


def line_chart(name: str, unit: str, samples: list[tuple[datetime, float | None]],
               places: int = 0, threshold: float | None = None,
               threshold_label: str = "", width: int = WIDTH,
               plot_h: int = PLOT_H, bands: list | None = None) -> str:
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
    axis_format, tip_format = _time_formats(span / 3600)

    # Bridge a short break in the data; leave a real outage broken.
    steps = [b - a for a, b in zip(times, times[1:])]
    cadence = statistics.median(steps).total_seconds() if steps else 0
    max_gap = cadence * GAP_BRIDGE_INTERVALS if cadence else float("inf")

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
                     f'text-anchor="{anchor}">{moment.strftime(axis_format)}</text>')

    if threshold is not None and y0 <= threshold <= y1:
        y = py(threshold)
        parts.append(f'<line class="threshold" x1="{PAD_L}" y1="{y:.1f}" '
                     f'x2="{width - PAD_R}" y2="{y:.1f}"/>')
        if threshold_label:
            parts.append(f'<text class="threshold-label" x="{width - PAD_R}" '
                         f'y="{y - 5:.1f}" text-anchor="end">{html.escape(threshold_label)}</text>')

    # One or two missed readings are drawn straight through; a long outage
    # starts a new subpath so the chart never invents a measurement.
    path, previous, points = [], None, []
    for moment, value in samples:
        if value is None:
            continue
        x, y = px(moment), py(value)
        bridged = previous is not None and (moment - previous).total_seconds() <= max_gap
        path.append(f'{"L" if bridged else "M"}{x:.1f} {y:.1f}')
        previous = moment
        points.append({"x": round(x, 1), "y": round(y, 1),
                       "t": moment.strftime(tip_format), "v": _fmt(value, places)})
    # A vertical gradient in user space maps colour to y, and y maps to value,
    # so the line is coloured by concentration with no extra geometry. Stops
    # are doubled at each boundary to give a hard edge rather than a blend.
    stroke_attr = ""
    if bands:
        global _GRADIENT_N
        _GRADIENT_N += 1
        gid = f"band{_GRADIENT_N}"
        # Colour depends on value, and y depends on value, so a vertical
        # gradient in user space colours the line by concentration with no
        # extra geometry. Read top-down: the highest band sits at y=0. Each
        # boundary gets two stops at the same offset, giving a hard edge
        # rather than a wash between bands.
        roles = [role for _, role in bands][::-1]        # worst first
        bounds = [upper for upper, _ in bands][:-1][::-1]  # boundary values
        marks = [f'<stop offset="0" style="stop-color:var(--{roles[0]})"/>']
        for index, value in enumerate(bounds):
            offset = min(1.0, max(0.0, py(value) / height))
            marks.append(f'<stop offset="{offset:.4f}" '
                         f'style="stop-color:var(--{roles[index]})"/>')
            marks.append(f'<stop offset="{offset:.4f}" '
                         f'style="stop-color:var(--{roles[index + 1]})"/>')
        marks.append(f'<stop offset="1" style="stop-color:var(--{roles[-1]})"/>')
        parts.insert(0, f'<defs><linearGradient id="{gid}" gradientUnits="userSpaceOnUse" '
                        f'x1="0" y1="0" x2="0" y2="{height}">{"".join(marks)}'
                        f'</linearGradient></defs>')
        stroke_attr = f' style="stroke:url(#{gid})"'
    parts.append(f'<path class="line" d="{" ".join(path)}"{stroke_attr}/>')

    peak = max((s for s in samples if s[1] is not None), key=lambda s: s[1])
    peak_x, peak_y = px(peak[0]), py(peak[1])
    peak_fill = ''
    if bands:
        role = next((r for upper, r in bands if upper is None or peak[1] <= upper),
                    bands[-1][1])
        peak_fill = f' style="fill:var(--{role})"'
    parts.append(f'<circle class="peak-dot" cx="{peak_x:.1f}" cy="{peak_y:.1f}" '
                 f'r="3.5"{peak_fill}/>')
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
        f'{band_key(bands, unit, places)}<div class="tip"></div></figure>'
    )


def room_tabs(rooms: list[str], active: str, href_for) -> str:
    """Links between the per-room reports.

    Each room is a separate file rather than another pane in this one: panes
    would multiply the page size by the number of rooms, and the page is
    already large. Plain links keep every file the size of one room, and they
    work over file:// with no server.
    """
    if len(rooms) < 2:
        return ""
    links = "".join(
        f'<a href="{html.escape(href_for(name))}"'
        f'{" aria-current=\"page\"" if name == active else ""}>{html.escape(name)}</a>'
        for name in rooms
    )
    return f'<nav class="rooms"><span class="lead">Room</span>{links}</nav>'


def advice_row(room: str, items: list, hint: str = "") -> str:
    """Buttons that open a written report, generating it first if need be.

    Each button knows whether its report already exists. Served over http the
    page can ask the server to write a missing one; opened from disk it cannot,
    so it says what to run instead. The same file works both ways.
    """
    if not items:
        return ""
    buttons = "".join(
        f'<button type="button" data-range="{html.escape(key)}" '
        f'data-file="{html.escape(name)}" data-ready="{"1" if ready else "0"}">'
        f'{html.escape(label)}</button>'
        for key, label, name, ready in items
    )
    return (f'<nav class="rooms advice" data-room="{html.escape(room)}" '
            f'data-hint="{html.escape(hint)}"><span class="lead">Report</span>'
            f'{buttons}<span class="status" role="status"></span></nav>')


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
            f'<td{" class=\"delta\"" if key.endswith("_delta") else ""}>'
            f"{html.escape('-' if row.get(key) is None else str(row[key]))}</td>"
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


def page(title: str, subtitle: str, body: str, footer: str,
         tabs: str = "", extra_nav: str = "") -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<!-- This file is rewritten every few minutes. Without these, browsers happily
     serve a cached copy of a file:// page and you see yesterday's report. -->
<meta http-equiv="cache-control" content="no-cache, no-store, must-revalidate">
<meta http-equiv="pragma" content="no-cache">
<meta http-equiv="expires" content="0">
<title>{html.escape(title)}</title>
<style>{STYLE}</style>
</head>
<body>
<div class="wrap">
<header>
  <h1>{html.escape(title)}</h1>
  <p class="sub">{html.escape(subtitle)}</p>
</header>
{tabs}
{extra_nav}
{body}
<footer>{html.escape(footer)}</footer>
</div>
<script>{SCRIPT}</script>
</body>
</html>
"""
