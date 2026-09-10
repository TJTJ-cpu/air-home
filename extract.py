"""Read the air-monitor display out of a photo using a local vision model.

Talks to LM Studio's OpenAI-compatible endpoint, so any vision model loaded
there works; qwen/qwen3-vl-8b is the default.
"""
from __future__ import annotations

import base64
import io
import json
from pathlib import Path

from openai import OpenAI
from PIL import Image

import config

SYSTEM_PROMPT = """\
You read values off a photograph of an INKBIRD PLUS air quality monitor and \
report them as JSON. You are a transcriber, not an estimator.

The display is laid out like this:
- Top left: a clock. IGNORE IT COMPLETELY. Never report it.
- Large number in the centre of the coloured ring: AQI.
- Left of centre, beside a thermometer icon: temperature in degrees Celsius.
- Right of centre, beside a droplet icon: relative humidity as a percentage.
- Bottom row, three values left to right: PM2.5 in ug/m3, PM10 in ug/m3, \
and CO2 in ppm.

Rules:
- The digits are a seven-segment LCD font and are zero-padded. "002" is 2, \
"0454" is 454, "005" is 5. Strip the leading zeros.
- Count the digits before reading them. A seven-segment "1" is a narrow bar
with space either side and is easy to miss, which would turn 31 into 3 or 12
into 2. Temperature and humidity are normally two digits.
- Report only what is legibly printed. If glare, blur or an obstruction makes \
a value uncertain, return null for that field. A null is correct; a guess is \
a corrupted measurement.
- Do not infer one field from another, and do not carry over values you \
expect to see.
"""

USER_PROMPT = "Transcribe the six values from this air monitor display."

RESPONSE_SCHEMA = {
    "name": "air_reading",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "aqi": {"type": ["integer", "null"]},
            "temperature_c": {"type": ["number", "null"]},
            "humidity_pct": {"type": ["number", "null"]},
            "pm25": {"type": ["integer", "null"]},
            "pm10": {"type": ["integer", "null"]},
            "co2_ppm": {"type": ["integer", "null"]},
        },
        "required": list(config.FIELDS),
        "additionalProperties": False,
    },
}


class ExtractionError(RuntimeError):
    """The model could not be reached, or returned nothing usable.

    `transient` marks a fault with the server rather than with the photo. A
    transient failure must never be blamed on the image: the photo is fine and
    will read correctly once LM Studio is back.
    """

    def __init__(self, message: str, transient: bool = False):
        super().__init__(message)
        self.transient = transient


def _client() -> OpenAI:
    # LM Studio ignores the key but the SDK insists on one being present.
    return OpenAI(base_url=config.LMSTUDIO_BASE_URL, api_key="lm-studio", timeout=180)


def encode_image(path: Path, max_width: int | None = None) -> str:
    """Downscale and re-encode as a base64 JPEG data URI.

    A 4K frame carries no more readable digits than a 1280px one but costs the
    model far more time, so shrink before sending.
    """
    max_width = max_width or config.VISION_MAX_WIDTH
    with Image.open(path) as img:
        img = img.convert("RGB")
        if max_width and img.width > max_width:
            height = round(img.height * max_width / img.width)
            img = img.resize((max_width, height), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=90)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def _messages(data_uri: str) -> list[dict]:
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": data_uri}},
                {"type": "text", "text": USER_PROMPT},
            ],
        },
    ]


def _parse_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):  # some models fence their output regardless
        text = text.split("```")[1].lstrip("json").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise ExtractionError(f"model returned no JSON object: {text[:200]!r}")
        return json.loads(text[start : end + 1])


def _call(client: OpenAI, messages: list[dict], use_schema: bool) -> str:
    kwargs = {}
    if use_schema:
        kwargs["response_format"] = {"type": "json_schema", "json_schema": RESPONSE_SCHEMA}
    response = client.chat.completions.create(
        model=config.LMSTUDIO_MODEL,
        messages=messages,
        temperature=0,  # transcription, not creativity
        max_tokens=300,
        **kwargs,
    )
    return response.choices[0].message.content or ""


def ask(system: str, user: str, schema: dict | None = None,
        max_tokens: int = 1200) -> dict | str:
    """Ask the local model a text question. Returns parsed JSON if a schema is given.

    Used for the written advice. Note what is NOT sent: raw readings. Every
    number is computed in Python and handed over already summarised, so the
    model interprets arithmetic it cannot get wrong.
    """
    client = _client()
    kwargs = {}
    if schema:
        kwargs["response_format"] = {"type": "json_schema", "json_schema": schema}
    try:
        response = client.chat.completions.create(
            model=config.LMSTUDIO_MODEL,
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": user}],
            temperature=0.3, max_tokens=max_tokens, **kwargs)
    except Exception as exc:
        raise ExtractionError(
            f"LM Studio call failed ({exc}). Is the server running at "
            f"{config.LMSTUDIO_BASE_URL} with {config.LMSTUDIO_MODEL} loaded?",
            transient=True) from exc
    text = response.choices[0].message.content or ""
    return _parse_json(text) if schema else text.strip()


def validate(raw: dict) -> tuple[dict, list[str]]:
    """Coerce the model's dict into typed fields, nulling implausible values.

    Returns the cleaned reading and a list of human-readable rejection notes.
    """
    cleaned: dict[str, float | int | None] = {}
    notes: list[str] = []
    for field, (low, high) in config.FIELD_RANGES.items():
        value = raw.get(field)
        if value is None or value == "":
            cleaned[field] = None
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            cleaned[field] = None
            notes.append(f"{field}: not a number ({value!r})")
            continue
        if not low <= number <= high:
            cleaned[field] = None
            notes.append(f"{field}: {number:g} outside {low:g}..{high:g}")
            continue
        cleaned[field] = number if field == "temperature_c" else int(round(number))
    return cleaned, notes


def extract(path: Path, retry_implausible: bool = True) -> tuple[dict, list[str]]:
    """Run the model over one image. Raises ExtractionError if it cannot.

    If a value comes back outside its plausible range, the model is asked once
    more. That kind of miss is usually a slip on a single glyph -- a dropped
    "1" turning 31 into 3 -- rather than a photo that genuinely cannot be read,
    and a second look often lands it. The cleaner answer wins; if the retry is
    no better, the implausible value is discarded exactly as before.
    """
    reading, notes = _extract_once(path)
    if notes and retry_implausible:
        second, second_notes = _extract_once(path)
        if len(second_notes) < len(notes):
            return second, second_notes
    return reading, notes


def _extract_once(path: Path) -> tuple[dict, list[str]]:
    client = _client()
    try:
        messages = _messages(encode_image(path))
    except Exception as exc:
        raise ExtractionError(f"could not read image file: {exc}") from exc
    try:
        text = _call(client, messages, use_schema=True)
    except Exception as exc:
        # Not every backend accepts json_schema; retry on prompt alone before
        # giving up, so a schema quirk does not look like a model failure.
        try:
            text = _call(client, messages, use_schema=False)
        except Exception:
            raise ExtractionError(
                f"LM Studio call failed ({exc}). Is the server running at "
                f"{config.LMSTUDIO_BASE_URL} with {config.LMSTUDIO_MODEL} loaded?",
                transient=True,
            ) from exc
    return validate(_parse_json(text))
