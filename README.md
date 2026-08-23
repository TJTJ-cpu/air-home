# air-home

Logs the readings off a physical INKBIRD PLUS air quality monitor by
photographing it with a webcam and transcribing the display with a local
vision model.

The monitor has no data output — it is a screen. So: a webcam takes a photo on
an interval, a local vision model in LM Studio reads the digits, and the values
go into a SQLite database. Everything runs and stays on this machine.

## How it works

    capture.py  ──> captures/pending/2026-08-23T10-42-29Z.jpg
                         │
    process.py  ─────────┤  qwen3-vl-8b via LM Studio
                         ├──> data/readings.db
                         ├──> captures/processed/  (+ .json sidecar)
                         └──> captures/failed/     (unreadable photos)

    analyze.py  ──> data/report.html   charts + overnight trend
    readings.py ──> table on screen, or data/readings.csv

Capture and processing are separate on purpose. The camera can keep shooting
while LM Studio is closed; processing catches up later.

**Timestamps come from the filename, not the screen.** The device's own clock
drifts and is never read. Filenames are UTC; reports convert to local time.

**Nothing is processed twice.** A photo moves out of `pending/` once it has
been read, so the folder *is* the queue. `captured_at` is also a primary key,
so a re-run cannot double-insert.

**One run at a time.** `process.py` takes an exclusive lock and refuses to
start if another instance is going. Two concurrent runs race for the same
photos and block each other on the database.

## Setup

    python -m venv .venv
    pip install -r requirements.txt
    copy .env.example .env

In LM Studio: load `qwen/qwen3-vl-8b`, then start the server on the
**Developer** tab (default `http://localhost:1234`).

Set `LOCAL_UTC_OFFSET` in `.env` to your timezone — reports are drawn on that
clock, and "night" only means anything locally.

Commands below assume `.venv\Scripts\python.exe`; activate the venv first if
you would rather just type `python`.

## Use

Check your framing first — this matters more than anything else below:

    .venv\Scripts\python.exe capture.py --once

Open the photo. Every digit must be legible, with no glare across the glass.
Then start capturing, in its own terminal:

    .venv\Scripts\python.exe capture.py --interval 5m     # also: 30s, 1m, 1h
    .venv\Scripts\python.exe capture.py --interval 1m --count 10

Read the queue whenever you like — at roughly 11s per photo:

    .venv\Scripts\python.exe process.py                   # read everything new
    .venv\Scripts\python.exe process.py --dry-run         # print, change nothing
    .venv\Scripts\python.exe process.py --limit 5         # try a handful first
    .venv\Scripts\python.exe process.py --retry-failed    # re-read failed/

## Looking at the data

    .venv\Scripts\python.exe analyze.py --open            # everything, in browser
    .venv\Scripts\python.exe analyze.py --last 6h         # also 30m, 2d
    .venv\Scripts\python.exe analyze.py --today
    .venv\Scripts\python.exe analyze.py --every 1h        # average into buckets
    .venv\Scripts\python.exe analyze.py --night           # most recent night
    .venv\Scripts\python.exe analyze.py --from "2026-08-23 11:00" --to "2026-08-23 12:30"

`analyze.py` writes `data/report.html` — a single self-contained file holding:

- **tiles** — peak CO₂ and when, rate of change, temperature and humidity range
- **a metric selector** — click *CO₂*, *Temperature*, *Humidity*, *AQI*,
  *PM2.5* or *PM10* to blow that one up to full width; *All* returns to the grid
- **a chart per metric**, with the peak labelled and a hover crosshair
- **summary and trend** — readings, mean, min, max and slope per hour for each
- **averages by hour**, and by day once the window covers more than a day
- **every reading**, in a collapsible table

To open straight onto one metric rather than clicking:

    .venv\Scripts\python.exe analyze.py --today --metric co2
    .venv\Scripts\python.exe analyze.py --last 12h --metric humidity

Accepted names: `co2`, `temp`, `humidity`, `aqi`, `pm25`, `pm10`.

Windows stack with `--every`, which only averages the *charts* — the tables
always summarise the real readings, so a bucket never hides a spike from the
numbers.

Each metric gets its own plot rather than sharing an axis: CO₂ in the hundreds
and AQI in single digits on one pair of y-axes would invent a correlation that
is not in the data.

For a quick look without the browser:

    .venv\Scripts\python.exe readings.py                  # 20 most recent
    .venv\Scripts\python.exe readings.py --csv            # data/readings.csv

## The data

Everything lives in `data/readings.db`, one row per photo:

| Column | |
|---|---|
| `captured_at` | UTC timestamp, primary key |
| `image` | filename, findable in `captures/processed/` |
| `aqi`, `temperature_c`, `humidity_pct`, `pm25`, `pm10`, `co2_ppm` | the readings |
| `notes` | values rejected as implausible |
| `recorded_at` | when the model read it |

It is a plain SQLite file — open it with any SQLite browser, or load it with
pandas, which is what `analyze.py` does.

Each processed photo also gets a `.json` sidecar beside it holding exactly what
the model returned. That is both the place to look when a number seems wrong
and a full backup of the database:

    .venv\Scripts\python.exe rebuild.py --dry-run
    .venv\Scripts\python.exe rebuild.py     # replay sidecars into the db

## Getting good reads

The model is only as good as the photo. In rough order of impact:

- **Square-on.** Shoot the display straight, not from below. An angled
  seven-segment digit is where `8` becomes `0`.
- **Kill the reflection.** No lamp or window behind the camera. The glass is
  glossy and a highlight across the bottom row will take out PM2.5 and PM10.
- **Fill the frame.** The display should be most of the picture.
- **Fix the camera in place.** Once framing is right, tape it down. Consistent
  framing is most of the accuracy.

A value the model cannot read comes back as `NULL`, and one that is physically
implausible is rejected into `notes` rather than stored — a gap in the series
is recoverable, a wrong number silently logged is not. Gaps break the chart
line rather than being bridged, so missing data never looks measured.

## Files

| File | Role |
|---|---|
| `capture.py` | Webcam, interval loop, timestamped filenames |
| `process.py` | Batch: extract → validate → store |
| `extract.py` | Prompt, LM Studio call, plausibility checks |
| `store.py` | SQLite ledger |
| `analyze.py` | pandas analysis, window selection, stats |
| `report.py` | HTML + inline-SVG rendering |
| `readings.py` | Terminal table, CSV export |
| `rebuild.py` | Restore the database from sidecars |
| `config.py` | Paths and `.env` settings |
