"""Serve the reports so the buttons in them actually work.

Opened straight from disk, a report is just a file: the browser cannot run
Python, so the "generate report" buttons have nothing to call. Served from
here they do, because this process is listening and can run the AI for them.

    python serve.py                 # then open http://localhost:8000

Nothing is exposed to the network -- it binds to this machine only.
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import webbrowser
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

import advise
import config
import store

BUSY = threading.Lock()


class Handler(SimpleHTTPRequestHandler):
    """Static files out of data/, plus one endpoint that writes a report."""

    def do_GET(self) -> None:  # noqa: N802  (http.server's naming)
        # Browsers ask for this unprompted; answering plainly beats a 404 storm.
        if self.path == "/favicon.ico":
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        super().do_GET()

    def do_POST(self) -> None:  # noqa: N802  (http.server's naming)
        if self.path.rstrip("/") != "/advise":
            self.send_error(404, "no such endpoint")
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or "{}")
            room = config.room_name(body.get("room", ""))
            span = str(body.get("range", ""))
        except (ValueError, TypeError) as exc:
            self._json(400, {"error": f"bad request: {exc}"})
            return
        if span not in advise.RANGES:
            self._json(400, {"error": f"unknown range {span!r}"})
            return

        # One at a time: the model is a single local process, and two reports
        # racing would only make both slower.
        if not BUSY.acquire(blocking=False):
            self._json(429, {"error": "already writing a report -- try again in a moment"})
            return
        try:
            path = advise.build(room, span, config.REPORTS / "report.html", quiet=True)
        except Exception as exc:                      # never take the server down
            self._json(500, {"error": str(exc)})
            return
        finally:
            BUSY.release()

        if not path:
            self._json(404, {"error": f"no readings for {room} in that period"})
            return
        self._json(200, {"url": path.name})

    def _json(self, code: int, payload: dict) -> None:
        raw = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def end_headers(self) -> None:
        # The reports are rewritten constantly; a cached copy is always wrong.
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, fmt: str, *args) -> None:
        """Log writes and real failures; stay quiet about routine file fetches.

        The arguments are not always strings -- log_error passes an HTTPStatus
        -- so format first and match on the finished line.
        """
        try:
            line = fmt % args if args else fmt
        except (TypeError, ValueError):
            line = str(fmt)
        if "POST" in line or "code 5" in line:
            super().log_message("%s", line)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--room", help="which room's report to open")
    parser.add_argument("--no-open", action="store_true", help="do not open a browser")
    args = parser.parse_args()

    if not config.DB_PATH.exists():
        print(f"no database at {config.DB_PATH} -- run process.py first", file=sys.stderr)
        return 1
    config.ensure_dirs()

    room = args.room
    if not room:
        with store.connect() as conn:
            room = store.latest_room(conn)
    page = f"report-{config.room_name(room)}.html" if room else "report.html"

    # Only the reports folder is served -- the database has no business
    # being reachable over HTTP, even on localhost.
    handler = partial(Handler, directory=str(config.REPORTS))
    server = ThreadingHTTPServer(("127.0.0.1", args.port), handler)
    url = f"http://localhost:{args.port}/{page}"
    print(f"serving {config.REPORTS} at {url}")
    print("the report buttons will work here -- Ctrl+C to stop")
    if not args.no_open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
