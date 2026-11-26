"""Vercel entry for KOAN play by link: GET /play and /play/... (see vercel.json).

All the work is in koantext.py (host/koantext.py in the project). This file finds it, works
out the two addresses, and moves bytes. Nothing is stored.

PAGE_URL  the page for people, from the environment (optional).
PLAY_URL  the front door, such as https://example.org/play, from the environment; when it
          is not set, https:// + the request's Host header + /play.
"""

import os
import sys
from http.server import BaseHTTPRequestHandler
from pathlib import Path

_HERE = Path(__file__).resolve().parent
# Deployed: api/_lib/ holds koantext.py and engine/. In the project: host/ and engine/.
for _dir in (_HERE / "_lib", _HERE.parent.parent / "host"):
    if (_dir / "koantext.py").is_file():
        sys.path.insert(0, str(_dir))
        break

import koantext                                             # noqa: E402


def respond(path, host, environ=None):
    """(status, headers, body) for one request target and Host header."""
    environ = os.environ if environ is None else environ
    page_url, play_url = koantext.load_config(environ)
    if not play_url:
        host = "".join(c for c in (host or "") if c.isalnum() or c in ".-:") or "localhost"
        play_url = "https://%s/play" % host
    return koantext.handle(path, play_url, page_url)


class handler(BaseHTTPRequestHandler):                      # the name Vercel looks for
    def _answer(self, with_body):
        status, headers, body = respond(self.path, self.headers.get("Host"))
        self.send_response(status)
        for k, v in headers.items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if with_body:
            self.wfile.write(body)

    def do_GET(self):
        self._answer(True)

    def do_HEAD(self):
        self._answer(False)
