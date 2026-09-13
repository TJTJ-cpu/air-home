# air-home

Keeps a record of the air quality in my house, by taking a photo of my air
monitor and letting an AI read the numbers off the screen.

## The problem

I have an INKBIRD PLUS air monitor. It shows CO₂, temperature, humidity, AQI,
PM2.5 and PM10 on a little screen. The numbers are right there — but there's no
app, no cable, no way to get them out. The thing only knows how to display.

So instead of trying to talk to the device, this just looks at it. A webcam
takes a photo every few minutes, and an AI reads the digits the same way you
would.

## How it works

    1. A webcam takes a photo of the monitor.
    2. An AI running on this PC reads the six numbers off the screen.
    3. The numbers go into a small database.
    4. Wait a few minutes, then do it again.

That's the whole idea. One program, `watch.py`, does all four steps on a loop.

Two details worth knowing:

**The photo is saved before the AI looks at it.** If the AI is closed or
broken, the photo is still safe on disk and gets read later. A photo you never
took is gone forever. A reading you haven't done yet is not.

**The time comes from the computer, not from the screen.** The monitor has its
own clock, but it drifts — mine was already 12 seconds slow. Every photo is
named with the exact moment it was taken, and that's what gets recorded.

## What you need

- A webcam pointed at the monitor
- [LM Studio](https://lmstudio.ai) with the `qwen/qwen3-vl-8b` model loaded
- Python 3.11 or newer

The AI runs entirely on your own machine. Nothing gets uploaded, nothing costs
money, and it works with no internet.

## Setup

    python -m venv .venv
    .venv\Scripts\activate
    pip install -r requirements.txt
    copy .env.example .env

Open `.env` and set `LOCAL_UTC_OFFSET` to your timezone — mine is `7`. That's
the only setting you really need to touch.

Then in LM Studio: load `qwen/qwen3-vl-8b` and start the server from the
**Developer** tab.

## Using it

First, check what the camera can see:

    python capture.py grandpa --once

Open that photo. Can you read every digit clearly? If yes, you're good. If it's
blurry or there's glare on the glass, fix it now — it matters more than
anything else here.

Then start it and leave it running in its own window:

    python watch.py grandpa

That's it. It takes a photo every 3 minutes, reads it, saves it, and keeps the
report up to date. Press `Ctrl+C` to stop.

The word after `watch.py` is **which room the camera is pointed at**. See
[Rooms](#rooms) below — it matters, because readings are filed under it.

Want a different gap between photos?

    python watch.py grandpa --interval 5m
    python watch.py tj --interval 1m

Five minutes is a sensible default. Every minute gives you more detail, but
uses a lot more disk space and a lot more of your GPU.

**The photo just taken is always read first.** If a backlog has built up, the
new one still jumps the queue — it is the only photo describing the room right
now, so it decides whether to speed up and it is what the report shows as
current. Older photos are then worked through oldest-first with whatever time
is left in the cycle, and they yield entirely when sampling speeds up: keeping
up with the present matters more than catching up on the past.

**It speeds up on its own when there's something to see.** Rather than one
fixed gap, it picks between three, and the fastest one that applies wins:

| | how often | when |
|---|---|---|
| Normal | your interval | nothing much happening |
| CO₂ is high | every 2 minutes | the last reading was above 800 ppm |
| Something is changing | every minute | a reading jumped, in the last 10 minutes |

The middle one is about a **level**, not a change. A room sitting flat at 1044
never jumps — it just stays bad — and that is exactly the part of the night you
want detail on, so a high number alone is enough to make it look more often. It
keeps that up for as long as CO₂ stays high.

The last one is about a **change**. If a reading jumps sharply from the one
before — you switch on a fan, open a door, start cooking — it drops to a photo
a minute for the next ten minutes, so the shape of the change is captured
instead of showing up as one step. Each new jump extends that window.

When the window runs out it doesn't go straight back to normal, it goes back to
whichever rule still applies — so a fan that clears the room ends at your normal
interval, while one that doesn't leaves it at 2 minutes. It says so when the gap
changes:

    now every 120s -- co2 1039, above 800

None of this ever makes it slower than you asked. If you run `--interval 1m`,
it stays at a minute throughout.

What counts as a jump, between one reading and the next:

| | triggers at |
|---|---|
| CO₂ | 50 ppm |
| Temperature | 2 °C |
| PM2.5 | 10 µg/m³ |
| PM10 | 12 µg/m³ |

Those are the 99th percentile of changes actually seen in the data, so they fire
on roughly 1% of readings — real events, not sensor jitter.

Humidity and AQI deliberately never trigger. AQI is calculated from PM2.5, so it
would count the same event twice. Humidity follows the weather outside rather
than anything happening in the room — overnight it barely differs from daytime
while CO₂ nearly doubles — so it is worth recording but not worth reacting to.

The thresholds, the 800 ppm line and both faster gaps all live in `config.py`
(`JUMP_THRESHOLDS`, `HIGH_CO2_PPM`, `HIGH_INTERVAL_SECONDS`,
`FAST_INTERVAL_SECONDS`). Change them if your room behaves differently, or turn
the whole thing off:

    python watch.py grandpa --no-adaptive

This only works in `watch.py`, which reads each photo as it goes. `capture.py`
never looks at the numbers, so it cannot know when something changed.

## Rooms

One monitor and one webcam can cover several rooms — you just move them, and
tell the program where they are:

    python watch.py grandpa
    python watch.py tj
    python watch.py mom

Every reading is filed under that room, and each room gets its own photo queue
in `captures/<room>/`. Nothing mixes: two rooms can even have readings at the
exact same time.

Room names are lowercased and must be simple — letters, digits, dashes,
underscores. `Grandpa` and `grandpa` are the same room; `guest room` becomes
`guest-room`.

To see what you have:

    python readings.py --rooms

The report has a **Room** switcher at the top, so you can click between rooms
in the browser. Each room gets its own file (`data/report-tj.html`), and
`data/report.html` stays the one to open.

Reports and listings default to whichever room has the newest reading, so most
of the time you don't need to say. When you do:

    python analyze.py --room tj --open
    python analyze.py --all-rooms          # rebuild every room's report
    python readings.py --room mom
    python prune.py --room tj --incomplete

`process.py` with no room reads the waiting photos for **every** room, which is
usually what you want after moving the camera around:

    python process.py            # all rooms
    python process.py tj         # just one

## Looking at your data

    python analyze.py --open

This builds a web page and opens it in your browser. You get:

- Big numbers at the top — peak CO₂, whether it's rising or falling, the
  temperature and humidity range
- A chart for each measurement, which you can click to make bigger
- Averages by hour
- A table of every single reading

It opens on **the last 24 hours**, not on "today". That's deliberate — you go
to bed before midnight, so a calendar day would cut your night in half.

The dropdown at the top is a zoom control rather than a list of dates:

    Last 24 hours · Last 3 days · Last week · Last month · All time

A range only shows up once you have more data than it covers, so the list
starts short and grows with your history. Zoomed out, the axis and the hover
tooltip switch from clock times to dates, so you always know which day a point
belongs to.

The line is coloured by how healthy the reading is — green when it's fine,
through amber and orange, to red when it isn't. Four metrics have bands:

| | green | amber | orange | red |
|---|---|---|---|---|
| CO₂ (ppm) | ≤800 | 800–1000 | 1000–1400 | >1400 |
| PM2.5 (µg/m³) | ≤15 | 15–25 | 25–35 | >35 |
| PM10 (µg/m³) | ≤45 | 45–75 | 75–100 | >100 |
| AQI | ≤50 | 50–100 | 100–150 | >150 |

PM2.5 and PM10 use the WHO 2021 24-hour guideline and its interim targets; AQI
uses the US EPA categories. The CO₂ bands are the traffic-light convention used
by REHVA and most consumer monitors — worth knowing that this is a practical
convention rather than a health limit. ASHRAE deliberately does *not* set an
indoor CO₂ threshold, and the old 1000 ppm figure was dropped from its standard
because people kept reading it as one. High CO₂ means poor ventilation, not
poison. A key under each chart says what the colours mean.

Temperature and humidity stay a single colour — there's no "unhealthy" band for
them in the same sense.

Short gaps in the line are drawn straight through — one missed photo shouldn't
break the chart into pieces. A long gap, like the camera being off for hours,
still leaves a break, because a straight line across that would be showing you
data that was never measured.

### A written report you can save as PDF

    python advise.py --all-ranges       # last 3 days, 5 days and a week
    python advise.py --last 3d --open

This writes a short plain-English report: how the air has been, how you slept,
what is going well, and what to change. Open it and press **Save as PDF** — the
browser's own print dialog does the rest, so there is nothing extra to install.

**To make them from the page itself**, start the little local server:

    python serve.py

It opens `http://localhost:8000/report-<room>.html`, and the **Report** buttons
at the top now work: click *Last 3 days* and it asks the AI and opens the result
(about 30 seconds). Nothing is exposed to the network — it listens on this
machine only.

Opened straight from disk instead, those buttons can only open reports that
already exist. A `file://` page has no server behind it, so it cannot run Python
or reach the AI; it will tell you the command to run.

**Every number is worked out in Python before the AI sees anything.** The model
is handed a finished summary and asked only to interpret it, so it cannot get a
figure wrong. If LM Studio is closed, the report is still written — with the
tables, and a note where the commentary would be.

### Comparing nights

The dropdown also has a **Nights** section, with the same shape as the ranges:

    Nights, last 3 days · Nights, last week · Nights, all time

Each one strips out the daytime and shows only the night windows, so the chart
becomes one segment per night, side by side. A night runs from **20:00 to
09:00** — wider than actual sleep on purpose, so you get the quiet baseline
before bed and the drop after you open the door in the morning.

    python analyze.py --night          # last 3 nights
    python analyze.py --night n1w      # last week
    python analyze.py --night nall     # every night

Each nights view also has a **Nights compared** table: starting CO₂, peak
and when it happened, how fast it climbed, and how many hours it spent above
1000 ppm. Every night is measured against the first one, so you can see what a
change actually did.

To describe what you changed each night, edit `data/night_labels.json`:

    {
      "2026-08-24": "window open 10cm",
      "2026-08-25": "fan on low"
    }

The key is the evening the night started. Whatever you write shows up in the
dropdown and the comparison table. Leave it blank and nothing is shown.

For working with the numbers yourself:

    python analyze.py --export-nights      # data/reports/nights.csv

Change the hours with `NIGHT_START_HOUR` and `NIGHT_END_HOUR` in `.env`.

If you'd rather just see numbers in the terminal:

    python readings.py            # the 20 most recent
    python readings.py --csv      # data/reports/readings.csv

## Where everything lives

`data/` holds what is yours; `data/reports/` holds what the program made.

| Where | What |
|---|---|
| `data/readings.db` | All your readings, every room. This is the important one. |
| `data/night_labels.json` | Your notes on what you changed each night |
| `data/backup/` | Database backups |
| `data/reports/` | Every generated page and export — safe to delete, all rebuildable |
| `captures/<room>/pending/` | Photos waiting to be read |
| `captures/<room>/processed/` | Photos that have been read |
| `captures/<room>/failed/` | Photos the AI couldn't make sense of |
| `captures/<room>/rejected/` | Photos behind readings you deleted |

Photos are never deleted, only moved. They add up fast — roughly 25 MB a day —
so clear out `captures/processed/` now and then if space gets tight. Your
readings are already in the database, so deleting old photos is safe.

## Getting clear photos

The AI can only read what the camera can see. In order of how much each one
helps:

1. **Point the camera straight at the screen**, not up from below. At an angle,
   an `8` starts to look like a `0`.
2. **No lamp or window behind the camera.** The screen is glossy, and a
   reflection across the bottom row will wipe out PM2.5 and PM10.
3. **Get close.** The screen should fill most of the photo.
4. **Tape the camera down** once it looks right. Keeping the framing identical
   is most of the accuracy.

If the AI can't read a number, it saves a blank instead of guessing. If a
number comes back impossible — like CO₂ at 12 ppm — it gets thrown out and
noted. A gap in your data can be filled in later. A wrong number that looks
real cannot.

## If something goes wrong

**Photos piling up in `pending/`?** LM Studio probably isn't running. Start it,
and they'll get read automatically.

**Want to catch up quickly?** `python process.py` reads everything that's
waiting, across every room, faster than the loop manages.

**Lost the database?** `python rebuild.py` puts it back together from the small
`.json` files saved next to each photo.

## The files

| File | What it does |
|---|---|
| `watch.py` | The main loop — photo, read, save, wait |
| `capture.py` | Takes photos |
| `process.py` | Reads photos that are waiting |
| `extract.py` | Asks the AI to read a photo |
| `store.py` | Saves readings to the database |
| `analyze.py` | Builds the report |
| `report.py` | Draws the charts |
| `advise.py` | Writes the plain-English report you can save as PDF |
| `serve.py` | Serves the reports so their buttons work |
| `recheck.py` | Re-reads photos behind readings that look wrong |
| `readings.py` | Shows readings in the terminal |
| `rebuild.py` | Rebuilds the database from photo files |
| `config.py` | Settings |
| `selftest.py` | Checks the pipeline still works, in a throwaway folder |
