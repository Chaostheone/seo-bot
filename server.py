#!/usr/bin/env python3
"""Local SEO auditor. Bind to 127.0.0.1 and open the UI in a browser."""

from __future__ import annotations

import json
import os
import threading
import traceback
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from auditor import audit_site

ROOT = Path(__file__).resolve().parent
HOST = "127.0.0.1"
PORT = int(os.environ.get("SEO_BOT_PORT", "8765"))
JOBS: dict[str, dict] = {}
LOCK = threading.Lock()


def suggest_copy(page: dict) -> dict:
    key = os.environ.get("XAI_API_KEY", "").strip()
    if not key:
        raise RuntimeError("Set XAI_API_KEY in the environment to generate rewrites.")
    issues = "; ".join(item["message"] for item in page.get("issues") or [])
    prompt = (
        "You write search snippets. Reply with JSON only, keys title, description, h1. "
        "title about 50-60 characters, description 120-155 characters, h1 a short heading. "
        "Use only facts from the page text. Do not invent products, prices, or claims.\n\n"
        f"URL: {page.get('url')}\n"
        f"Current title: {page.get('title')}\n"
        f"Current description: {page.get('description')}\n"
        f"Current H1: {', '.join(page.get('h1') or [])}\n"
        f"Issues: {issues}\n"
        f"Page text:\n{(page.get('excerpt') or '')[:1200]}"
    )
    payload = json.dumps({"model": "grok-4.7", "input": prompt}).encode()
    req = Request(
        "https://api.x.ai/v1/responses",
        data=payload,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
    )
    try:
        with urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode())
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        raise RuntimeError(f"Model request failed ({exc.code}). {detail}") from exc
    except URLError as exc:
        raise RuntimeError(f"Model request failed: {exc.reason}") from exc
    text = data.get("output_text") or ""
    if not text:
        for block in data.get("output") or []:
            for part in block.get("content") or []:
                if part.get("text"):
                    text += part["text"]
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end < start:
        raise RuntimeError("The model did not return JSON.")
    parsed = json.loads(text[start : end + 1])
    return {
        "title": str(parsed.get("title", "")).strip(),
        "description": str(parsed.get("description", "")).strip(),
        "h1": str(parsed.get("h1", "")).strip(),
    }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:
        print("[seo-bot]", fmt % args)

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, payload: dict) -> None:
        self._send(code, json.dumps(payload).encode(), "application/json; charset=utf-8")

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            html = (ROOT / "index.html").read_bytes()
            self._send(200, html, "text/html; charset=utf-8")
            return
        if path.startswith("/api/audit/"):
            job_id = path.rsplit("/", 1)[-1]
            with LOCK:
                job = JOBS.get(job_id)
            if not job:
                self._json(404, {"error": "Unknown audit."})
                return
            self._json(200, job)
            return
        self._json(404, {"error": "Not found."})

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(min(length, 100_000))
        try:
            data = json.loads(raw.decode() or "{}")
        except json.JSONDecodeError:
            self._json(400, {"error": "Send JSON."})
            return
        path = self.path.split("?", 1)[0]
        if path == "/api/audit":
            self._start_audit(data)
            return
        if path == "/api/suggest":
            self._suggest(data)
            return
        self._json(404, {"error": "Not found."})

    def _start_audit(self, data: dict) -> None:
        url = str(data.get("url") or "")
        try:
            max_pages = int(data.get("max_pages") or 12)
        except (TypeError, ValueError):
            max_pages = 12
        use_sitemap = bool(data.get("use_sitemap", True))
        job_id = uuid.uuid4().hex[:12]
        with LOCK:
            JOBS[job_id] = {"id": job_id, "status": "running", "url": url}
        thread = threading.Thread(
            target=self._run_audit,
            args=(job_id, url, max_pages, use_sitemap),
            daemon=True,
        )
        thread.start()
        self._json(202, {"id": job_id})

    def _run_audit(self, job_id: str, url: str, max_pages: int, use_sitemap: bool) -> None:
        try:
            result = audit_site(url, max_pages=max_pages, use_sitemap=use_sitemap)
            result["status"] = "done"
            result["id"] = job_id
            with LOCK:
                JOBS[job_id] = result
        except Exception as exc:  # noqa: BLE001 — surface crawl failures to the UI
            with LOCK:
                JOBS[job_id] = {
                    "id": job_id,
                    "status": "error",
                    "error": str(exc),
                    "detail": traceback.format_exc(limit=3),
                }

    def _suggest(self, data: dict) -> None:
        try:
            suggestion = suggest_copy(data)
        except Exception as exc:  # noqa: BLE001
            self._json(400, {"error": str(exc)})
            return
        self._json(200, suggestion)


def main() -> None:
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"SEO bot UI at http://{HOST}:{PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()
