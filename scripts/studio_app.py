#!/usr/bin/env python3
"""Local fanart studio: identity lock + other-IP composition + side-by-side compare.

Bind 127.0.0.1 only. Keys stay in ~/.zcode/image-edit.local.json.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import threading
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import ROOT, load_bible, load_settings  # noqa: E402
from generate import build_prompt, generate_one  # noqa: E402
from studio_lib import (  # noqa: E402
    allowed_path,
    cfg,
    draft_from_inspire,
    ensure_thumb,
    job_id,
    library_root,
    list_inspire,
    list_results,
    original_dir,
    pipeline_characters,
    save_review,
    vision_counts,
)

STATIC = ROOT / "studio"
JOBS: list[dict] = []
LOCK = threading.Lock()
WORKER_STARTED = False


def log(msg: str) -> None:
    sys.stderr.write(msg.rstrip() + "\n")


def public_job(job: dict) -> dict:
    out = {k: v for k, v in job.items() if k not in ("raw_error_full",)}
    return out


def start_worker() -> None:
    global WORKER_STARTED
    if WORKER_STARTED:
        return
    WORKER_STARTED = True

    def loop() -> None:
        while True:
            with LOCK:
                pending = next((j for j in JOBS if j["status"] == "queued"), None)
                if pending:
                    pending["status"] = "running"
                    current = pending
                else:
                    current = None
            if current is None:
                import time
                time.sleep(0.4)
                continue
            run_job(current)

    threading.Thread(target=loop, daemon=True).start()


def run_job(job: dict) -> None:
    import time
    t0 = time.time()
    try:
        settings = load_settings()
        bible = load_bible(job["character"])
        payload = {
            "id": job["id"],
            "character": job["character"],
            "shot": job.get("shot") or "",
            "pose": job.get("pose") or "",
            "style": job.get("style") or job.get("mood") or "",
            "mood": job.get("mood") or "",
            "pose_ref": job.get("pose_ref") or None,
            "prompt_override": job.get("prompt_override") or "",
            "negative_override": job.get("negative_override") or "",
            "size": job.get("size") or "",
            "quality": job.get("quality") or "",
        }
        raw = generate_one(settings, bible, payload)
        out_dir = Path(job["out_dir"])
        out_dir.mkdir(parents=True, exist_ok=True)
        dest = out_dir / f"{job['id']}.png"
        dest.write_bytes(raw)
        inspire = Path(job.get("inspire") or "")
        if inspire.is_file():
            left = out_dir / f"left_{job['id']}{inspire.suffix.lower()}"
            shutil.copy2(inspire, left)
            job["left"] = str(left)
        job["right"] = str(dest)
        job["bytes"] = len(raw)
        job["status"] = "ok"
        job["sec"] = round(time.time() - t0, 1)
        write_run_log(job, out_dir)
        log(f"studio {job['id']} ok {len(raw)}B {job['sec']}s")
    except Exception as e:
        job["status"] = "fail"
        job["error"] = str(e)
        job["sec"] = round(time.time() - t0, 1)
        log(f"studio {job['id']} fail {e}\n{traceback.format_exc()}")


def write_run_log(job: dict, out_dir: Path) -> None:
    """Append a success record so the result list can index studio output."""
    rec = {
        "id": job["id"],
        "ok": True,
        "path": job.get("right") or "",
        "bytes": job.get("bytes") or 0,
        "sec": job.get("sec"),
        "inspire": job.get("inspire") or "",
        "shot": job.get("shot") or "",
        "pose": job.get("pose") or "",
        "style": job.get("style") or "",
        "send_inspire": bool(job.get("send_inspire")),
        "size": job.get("size") or "",
        "quality": job.get("quality") or "",
    }
    if job.get("redo_of"):
        rec["redo_of"] = job["redo_of"]
    try:
        with (out_dir / "run_log.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError as e:
        log(f"studio run_log write failed: {e}")


class Handler(BaseHTTPRequestHandler):
    server_version = "fanart-studio/0.1"

    def log_message(self, fmt: str, *args) -> None:
        log("%s - %s" % (self.address_string(), fmt % args))

    def _guard(self) -> bool:
        host = (self.headers.get("Host") or "").strip().lower()
        port = self.server.server_address[1]
        if host not in (f"127.0.0.1:{port}", f"localhost:{port}"):
            self._json(403, {"ok": False, "error": "仅本机 127.0.0.1"})
            return False
        origin = (self.headers.get("Origin") or "").strip().rstrip("/")
        if origin and origin not in (f"http://127.0.0.1:{port}", f"http://localhost:{port}"):
            self._json(403, {"ok": False, "error": "跨站请求已拒绝"})
            return False
        return True

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def _read_json(self) -> dict:
        n = int(self.headers.get("Content-Length") or "0")
        raw = self.rfile.read(n) if n else b"{}"
        obj = json.loads(raw.decode("utf-8"))
        if not isinstance(obj, dict):
            raise ValueError("JSON 必须是对象")
        return obj

    def do_GET(self) -> None:
        if not self._guard():
            return
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)
        if path in ("/", "/index.html"):
            html = STATIC / "index.html"
            self._send(200, html.read_bytes(), "text/html; charset=utf-8")
            return
        if path.startswith("/static/"):
            rel = path[len("/static/"):]
            target = (STATIC / rel).resolve()
            if not str(target).startswith(str(STATIC.resolve())) or not target.is_file():
                self._send(404, b"missing", "text/plain")
                return
            ctype = "text/javascript; charset=utf-8" if target.suffix == ".js" else "text/css; charset=utf-8"
            self._send(200, target.read_bytes(), ctype)
            return
        if path == "/api/state":
            settings = {}
            try:
                s = load_settings()
                settings = {
                    "model": s["model"],
                    "base_url": s["base_url"],
                    "size": s["size"],
                    "quality": s["quality"],
                    "profile": s.get("profile_name") or s.get("profile_id") or "",
                    "has_key": True,
                    "size_presets": cfg().get("size_presets") or {},
                }
            except SystemExit as e:
                settings = {"has_key": False, "error": str(e)}
            with LOCK:
                jobs = [public_job(j) for j in JOBS[-40:]]
            self._json(200, {
                "ok": True,
                "library": str(library_root()),
                "characters": pipeline_characters(),
                "settings": settings,
                "jobs": jobs,
            })
            return
        if path == "/api/prompt":
            # Preview the exact prompt that would be sent for these parameters.
            try:
                character = (qs.get("character") or [""])[0]
                bible = load_bible(character)
            except SystemExit as e:
                self._json(400, {"ok": False, "error": str(e)})
                return
            has_ref = (qs.get("send_inspire") or [""])[0] in ("1", "true", "yes")
            job = {
                "character": character,
                "shot": (qs.get("shot") or [""])[0],
                "pose": (qs.get("pose") or [""])[0],
                "style": (qs.get("style") or [""])[0],
                "pose_ref": "1" if has_ref else "",
            }
            p, n = build_prompt(bible, job)
            self._json(200, {"ok": True, "prompt": p, "negative": n, "has_ref_image": has_ref})
            return
        if path == "/api/results":
            series = (qs.get("series") or [""])[0]
            status = (qs.get("status") or ["all"])[0]
            self._json(200, {"ok": True, **list_results(series, status)})
            return
        if path == "/api/inspire":
            exclude = (qs.get("exclude") or [""])[0]
            series = (qs.get("series") or [""])[0]
            vfilter = (qs.get("vision") or ["all"])[0]
            try:
                limit = min(200, max(1, int((qs.get("limit") or ["60"])[0])))
                offset = max(0, int((qs.get("offset") or ["0"])[0]))
            except ValueError:
                limit, offset = 60, 0
            self._json(200, {
                "ok": True,
                **list_inspire(exclude, series, limit, offset, vfilter),
                "counts": vision_counts(exclude, series),
            })
            return
        if path == "/api/draft":
            raw = (qs.get("path") or [""])[0]
            src = Path(raw)
            if not src.is_file() or not allowed_path(src):
                self._json(404, {"ok": False, "error": "灵感图不存在"})
                return
            self._json(200, {"ok": True, **draft_from_inspire(src)})
            return
        if path in ("/api/thumb", "/api/file"):
            raw = (qs.get("path") or [""])[0]
            src = Path(raw)
            if not src.is_file() or not allowed_path(src):
                self._send(404, b"not found", "text/plain")
                return
            if path == "/api/thumb":
                try:
                    src = ensure_thumb(src)
                except Exception:
                    pass
            data = src.read_bytes()
            ext = src.suffix.lower()
            ctype = {
                ".png": "image/png",
                ".jpg": "image/jpeg",
                ".jpeg": "image/jpeg",
                ".webp": "image/webp",
            }.get(ext, "application/octet-stream")
            self._send(200, data, ctype)
            return
        self._send(404, b"not found", "text/plain")

    def do_POST(self) -> None:
        if not self._guard():
            return
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/review":
            body = self._read_json()
            rid = str(body.get("result_id") or "").strip()
            status = str(body.get("status") or "").strip()
            note = str(body.get("note") or "")
            if not rid or status not in ("unreviewed", "pass", "manual_review", "quarantine"):
                self._json(400, {"ok": False, "error": "result_id 或 status 不合法"})
                return
            rec = save_review(rid, status, note)
            self._json(200, {"ok": True, "review": rec})
            return
        if path != "/api/generate":
            self._send(404, b"not found", "text/plain")
            return
        body = self._read_json()
        character = str(body.get("character") or "").strip()
        if not character:
            self._json(400, {"ok": False, "error": "先选身份角色"})
            return
        inspire = str(body.get("inspire") or "").strip()
        send_inspire = bool(body.get("send_inspire"))
        pose_ref = inspire if send_inspire and inspire else None
        if pose_ref and not Path(pose_ref).is_file():
            self._json(400, {"ok": False, "error": "灵感图不存在"})
            return
        jid = job_id()
        out_dir = original_dir(character)
        job = {
            "id": jid,
            "character": character,
            "shot": str(body.get("shot") or "").strip(),
            "pose": str(body.get("pose") or "").strip(),
            "style": str(body.get("style") or body.get("mood") or "").strip(),
            "mood": str(body.get("mood") or "").strip(),
            "inspire": inspire,
            "pose_ref": pose_ref,
            "send_inspire": send_inspire,
            "prompt_override": str(body.get("prompt_override") or "").strip(),
            "negative_override": str(body.get("negative_override") or "").strip(),
            "size": str(body.get("size") or "").strip(),
            "quality": str(body.get("quality") or "").strip(),
            "redo_of": str(body.get("redo_of") or "").strip(),
            "out_dir": str(out_dir),
            "status": "queued",
            "error": "",
            "right": "",
            "left": "",
        }
        with LOCK:
            JOBS.append(job)
        start_worker()
        self._json(200, {"ok": True, "job": public_job(job)})


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=int(cfg().get("studio_port") or 8788))
    args = parser.parse_args()
    host = "127.0.0.1"
    httpd = ThreadingHTTPServer((host, args.port), Handler)
    log("studio http://%s:%d/  library=%s" % (host, args.port, library_root()))
    httpd.serve_forever()


if __name__ == "__main__":
    main()
