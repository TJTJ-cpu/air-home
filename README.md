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

    python capture.py --once

Open that photo. Can you read every digit clearly? If yes, you're good. If it's
blurry or there's glare on the glass, fix it now — it matters more than
anything else here.

Then start it and leave it running in its own window:

    python watch.py

That's it. It takes a photo every 3 minutes, reads it, saves it, and keeps the
report up to date. Press `Ctrl+C` to stop.

Want a different gap between photos?

    python watch.py --interval 5m
    python watch.py --interval 1m

Five minutes is a sensible default. Every minute gives you more detail, but
uses a lot more disk space and a lot more of your GPU.

## Looking at your data

    python analyze.py --open

This builds a web page and opens it in your browser. You get:

- Big numbers at the top — peak CO₂, whether it's rising or falling, the
  temperature and humidity range
- A chart for each measurement, which you can click to make bigger
- Averages by hour
- A table of every single reading

It opens on **the last 24 hours**, not on "today". That's deliberate — you go
to bed before midnight, so a calendar day would cut your night in half. Use the
dropdown at the top to jump to a specific day instead.

### Comparing nights

The dropdown also has a **Nights** section. A night runs from **20:00 to 09:00**
— wider than actual sleep on purpose, so you get the quiet baseline before bed
and the drop after you open the door in the morning.

    python analyze.py --night              # the most recent night
    python analyze.py --night 2026-08-24   # the night that began that evening

Pick any night and you also get a **Nights compared** table: starting CO₂, peak
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

    python analyze.py --export-nights      # data/nights.csv, one row per night

Change the hours with `NIGHT_START_HOUR` and `NIGHT_END_HOUR` in `.env`.

If you'd rather just see numbers in the terminal:

    python readings.py            # the 20 most recent
    python readings.py --csv      # save as a spreadsheet file

## Where everything lives

| Where | What |
|---|---|
| `data/readings.db` | All your readings. This is the important one. |
| `data/report.html` | The web page. Rebuilt automatically, safe to delete. |
| `captures/pending/` | Photos waiting to be read |
| `captures/processed/` | Photos that have been read |
| `captures/failed/` | Photos the AI couldn't make sense of |

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
waiting, faster than the loop manages.

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
| `readings.py` | Shows readings in the terminal |
| `rebuild.py` | Rebuilds the database from photo files |
| `config.py` | Settings |
