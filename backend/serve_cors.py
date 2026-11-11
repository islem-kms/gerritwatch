"""
GerritWatch local server.

Serves flags.json (written by the monitor) and feedback.json to the browser extension,
and records the reviewer's thumbs up/down on POST /feedback. It listens on 127.0.0.1
only, serves no other file, and answers cross-origin requests only from the Gerrit
instances the extension runs on.

feedback.json is separate from flags.json, so feedback survives when the monitor
rewrites flags.json. Shape:
{"<flag_id>": {"detection": "up"|"down"|null, "summary": "up"|"down"|null, "updated_at": "..."}}

Usage (in the folder where the monitor writes flags.json):
    python serve_cors.py
GW_HOST changes the listen address (default 127.0.0.1); the Docker setup uses it.
"""

import http.server
import socketserver
import json
import os
from pathlib import Path
from datetime import datetime, timezone

HOST = os.environ.get("GW_HOST", "127.0.0.1")   # Docker sets 0.0.0.0 and publishes on 127.0.0.1 only
PORT = 8000
FEEDBACK_FILE = Path("feedback.json")
SERVED_FILES = {"flags.json", "feedback.json"}
ALLOWED_ORIGINS = {
    "https://gerrit.onap.org",
    "https://gerrit.libreoffice.org",
    "https://gerrit.wikimedia.org",
    "https://review.opendev.org",
}

VALID_FEEDBACK_TYPES = {"detection", "summary"}
VALID_VALUES = {"up", "down", None}


def load_feedback() -> dict:
    if FEEDBACK_FILE.exists():
        try:
            with open(FEEDBACK_FILE, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def save_feedback(data: dict) -> None:
    tmp = FEEDBACK_FILE.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
    os.replace(tmp, FEEDBACK_FILE)


class CORSRequestHandler(http.server.SimpleHTTPRequestHandler):
    def end_headers(self):
        origin = self.headers.get("Origin")
        if origin in ALLOWED_ORIGINS:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def do_OPTIONS(self):
        # Browsers send this automatically before a POST with a JSON body
        # ("preflight"). We just need to answer 200 with the CORS headers
        # above (already added by end_headers) so the real POST is allowed.
        self.send_response(200)
        self.end_headers()

    def do_GET(self):
        # Ensure feedback.json always exists and is servable, same as
        # flags.json -- avoids a 404 on first run before any feedback
        # has ever been submitted.
        name = self.path.lstrip("/").split("?")[0]
        if name not in SERVED_FILES:
            self.send_response(404)
            self.end_headers()
            return
        if name == "feedback.json" and not FEEDBACK_FILE.exists():
            save_feedback({})
        super().do_GET()

    def do_POST(self):
        origin = self.headers.get("Origin")
        if origin is not None and origin not in ALLOWED_ORIGINS:
            self.send_response(403)
            self.end_headers()
            return
        if self.path.rstrip("/") != "/feedback":
            self.send_response(404)
            self.end_headers()
            return

        try:
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            payload = json.loads(body)

            flag_id = payload.get("flag_id")
            feedback_type = payload.get("feedback_type")
            value = payload.get("value")  # "up", "down", or null (to clear)

            if not flag_id or feedback_type not in VALID_FEEDBACK_TYPES or value not in VALID_VALUES:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b'{"error": "invalid payload"}')
                return

            feedback = load_feedback()
            entry = feedback.get(flag_id, {"detection": None, "summary": None})
            entry[feedback_type] = value
            entry["updated_at"] = datetime.now(timezone.utc).isoformat()
            feedback[flag_id] = entry
            save_feedback(feedback)

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(entry).encode("utf-8"))

        except Exception as exc:
            self.send_response(500)
            self.end_headers()
            self.wfile.write(b'{"error": "server error"}')


if __name__ == "__main__":
    if not FEEDBACK_FILE.exists():
        save_feedback({})
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer((HOST, PORT), CORSRequestHandler) as httpd:
        print(f"GerritWatch local server on http://{HOST}:{PORT} (flags.json, feedback.json) ...")
        httpd.serve_forever()
