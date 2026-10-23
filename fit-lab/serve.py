#!/usr/bin/env python3
"""fit-lab local server (Python standard library only).

Serves the fit-lab web app and a data root of campaigns:

    <data-root>/<campaign-id>/campaign.json
    <data-root>/<campaign-id>/images/*.png

Routes
    GET  /                       app (index.html, app.js, style.css)
    GET  /data/<id>/<path>       files inside a campaign folder (images)
    GET  /api/campaigns          list of campaign summaries
    GET  /api/campaigns/<id>     campaign.json
    PUT  /api/campaigns/<id>     replace campaign.json (atomic write, keeps one .bak)

Usage
    python serve.py --root <data-root> [--port 8765] [--host 127.0.0.1]
    (or set FIT_LAB_ROOT instead of --root)
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import shutil
import sys
import tempfile
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

SCHEMA = "fit-lab/1"
APP_DIR = Path(__file__).resolve().parent
APP_FILES = {"/": "index.html", "/index.html": "index.html", "/app.js": "app.js", "/voice-parse.js": "voice-parse.js",
             "/style.css": "style.css"}
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
MAX_BODY = 20 * 1024 * 1024


def campaign_summary(cid: str, data: dict) -> dict:
    variants = [v.get("id") for v in data.get("variants") or [] if isinstance(v, dict)]
    items = data.get("items") or []
    cells = measured = 0
    for it in items:
        keys = ["*"] if "*" in (it.get("cad") or {}) or not variants else variants
        got = {m.get("variant") for m in it.get("measurements") or []}
        cells += len(keys)
        measured += sum(1 for k in keys if k in got)
    return {
        "id": cid,
        "title": data.get("title", cid),
        "project": data.get("project", ""),
        "material": data.get("material", ""),
        "process": data.get("process", ""),
        "vendor": data.get("vendor", ""),
        "ordered": data.get("ordered", ""),
        "received": data.get("received", ""),
        "variants": variants,
        "items": len(items),
        "cells": cells,
        "measured": measured,
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "fit-lab/1"
    root: Path = Path(".")

    # ---- helpers -------------------------------------------------------
    def log_message(self, fmt, *args):  # quieter log
        sys.stderr.write("%s %s\n" % (self.command, fmt % args))

    def send_json(self, obj, status=HTTPStatus.OK):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_error_json(self, status, message):
        self.send_json({"error": message}, status)

    def send_file(self, path: Path):
        if not path.is_file():
            self.send_error_json(HTTPStatus.NOT_FOUND, "not found")
            return
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        data = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")   # always pick up edited app files
        self.end_headers()
        self.wfile.write(data)

    def campaign_dir(self, cid: str) -> Path | None:
        if not ID_RE.match(cid):
            return None
        return self.root / cid

    # ---- routes --------------------------------------------------------
    def do_GET(self):
        path = unquote(urlparse(self.path).path)
        if path in APP_FILES:
            self.send_file(APP_DIR / APP_FILES[path])
        elif path == "/api/campaigns":
            self.list_campaigns()
        elif path.startswith("/api/campaigns/"):
            d = self.campaign_dir(path[len("/api/campaigns/"):])
            if d is None:
                self.send_error_json(HTTPStatus.BAD_REQUEST, "bad campaign id")
            else:
                self.send_file(d / "campaign.json")
        elif path.startswith("/data/"):
            self.serve_data(path[len("/data/"):])
        else:
            self.send_error_json(HTTPStatus.NOT_FOUND, "not found")

    def do_PUT(self):
        path = unquote(urlparse(self.path).path)
        if not path.startswith("/api/campaigns/"):
            self.send_error_json(HTTPStatus.NOT_FOUND, "not found")
            return
        cid = path[len("/api/campaigns/"):]
        d = self.campaign_dir(cid)
        if d is None:
            self.send_error_json(HTTPStatus.BAD_REQUEST, "bad campaign id")
            return
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_BODY:
            self.send_error_json(HTTPStatus.BAD_REQUEST, "missing or oversized body")
            return
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            self.send_error_json(HTTPStatus.BAD_REQUEST, f"invalid JSON: {e}")
            return
        if not isinstance(data, dict) or data.get("schema") != SCHEMA:
            self.send_error_json(HTTPStatus.BAD_REQUEST, f'"schema" must be "{SCHEMA}"')
            return
        if data.get("id") != cid:
            self.send_error_json(HTTPStatus.BAD_REQUEST, '"id" must match the URL')
            return
        if not isinstance(data.get("items"), list):
            self.send_error_json(HTTPStatus.BAD_REQUEST, '"items" must be a list')
            return
        d.mkdir(parents=True, exist_ok=True)
        target = d / "campaign.json"
        text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
        fd, tmp = tempfile.mkstemp(prefix=".campaign-", suffix=".tmp", dir=d)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
                f.write(text)
                f.flush()
                os.fsync(f.fileno())
            if target.exists():
                shutil.copy2(target, d / "campaign.json.bak")
            os.replace(tmp, target)
        except OSError as e:
            if os.path.exists(tmp):
                os.unlink(tmp)
            self.send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, f"write failed: {e}")
            return
        self.send_json({"ok": True, "summary": campaign_summary(cid, data)})

    def list_campaigns(self):
        out = []
        if self.root.is_dir():
            for d in sorted(self.root.iterdir()):
                f = d / "campaign.json"
                if not (d.is_dir() and ID_RE.match(d.name) and f.is_file()):
                    continue
                try:
                    data = json.loads(f.read_text(encoding="utf-8"))
                    out.append(campaign_summary(d.name, data))
                except (OSError, json.JSONDecodeError) as e:
                    out.append({"id": d.name, "title": d.name, "error": str(e)})
        self.send_json({"root": str(self.root), "campaigns": out})

    def serve_data(self, rel: str):
        root = self.root.resolve()
        target = (root / rel).resolve()
        if root not in target.parents:
            self.send_error_json(HTTPStatus.FORBIDDEN, "outside data root")
            return
        self.send_file(target)


def main():
    ap = argparse.ArgumentParser(description="fit-lab: CAD vs measured log for printed parts")
    ap.add_argument("--root", default=os.environ.get("FIT_LAB_ROOT"),
                    help="data root, a folder of campaigns (default: $FIT_LAB_ROOT)")
    ap.add_argument("--port", type=int, default=8765, help="port (default: 8765)")
    ap.add_argument("--host", default="127.0.0.1", help="bind address (default: 127.0.0.1)")
    args = ap.parse_args()
    if not args.root:
        ap.error("give the data root with --root or FIT_LAB_ROOT")

    Handler.root = Path(args.root).resolve()
    if not Handler.root.is_dir():
        print(f"warning: data root {Handler.root} does not exist (created on first save)", file=sys.stderr)
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"fit-lab: http://{args.host}:{args.port}/  (data root: {Handler.root})")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
