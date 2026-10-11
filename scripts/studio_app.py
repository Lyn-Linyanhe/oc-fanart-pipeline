#!/usr/bin/env python3
"""Local creator studio. Credentials never leave the server."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import shutil
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import ROOT, load_bible, load_settings, ref_images
from generate import build_prompt, generate_one
from studio_lib import (
    IMAGE_EXT, REVIEW_STATUSES, allowed_path, atomic_json, cfg, draft_from_inspire,
    ensure_thumb, find_result, inspiration_vision, is_under, job_id, library_root, list_inspire,
    list_results, local_store, now_iso, original_dir, pipeline_characters,
    results_root, save_draft, save_preset, save_review, undo_review, vision_counts,
)

STATIC = ROOT / "studio"
JOBS: list[dict] = []
LOCK = threading.RLock()
WORKER_STARTED = False


def log(message: str) -> None:
    sys.stderr.write(message.rstrip() + "\n")


def public_settings() -> dict:
    base = cfg()
    out = {
        "has_key": False, "model": base.get("model", ""),
        "size": base.get("size", "auto"), "quality": base.get("quality", "high"),
        "size_presets": base.get("size_presets") or {},
    }
    try:
        settings = load_settings()
        out.update({key: settings[key] for key in ("model", "size", "quality")})
        out.update(has_key=True, profile=settings.get("profile_name") or "本机配置")
    except (SystemExit, OSError, ValueError) as exc:
        out["error"] = str(exc)
    out["models"] = list(dict.fromkeys([out["model"], *base.get("models", []),
                                        "gpt-image-2.5-sunburst", "gpt-image-2.5-flare", "gpt-image-2"]))
    return out


def public_job(job: dict) -> dict:
    keys = ("id", "character", "status", "error", "sec", "submitted_at", "finished_at",
            "right", "left", "inspire", "parent_result_id", "redo_of", "model", "size", "quality")
    return {key: job.get(key) for key in keys}


def persist_jobs() -> None:
    atomic_json(results_root() / "jobs.json", {j["id"]: j for j in JOBS})


def restore_jobs() -> None:
    saved = local_store("jobs.json")
    with LOCK:
        JOBS[:] = [j for j in saved.values() if isinstance(j, dict)]
        for job in JOBS:
            if job.get("status") in ("queued", "running"):
                job.update(status="interrupted", error="服务中断，未自动重新提交。请核对产物和计费后再创建任务。")
        if JOBS:
            persist_jobs()


def checked_character(value) -> str:
    char_id = str(value or "")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", char_id) or not (ROOT / "characters" / char_id / "bible.json").is_file():
        raise ValueError("请先选择已登记的目标角色")
    return char_id


def checked_image(value) -> Path:
    path = Path(str(value or ""))
    if path.suffix.lower() not in IMAGE_EXT or not path.is_file() or not allowed_path(path):
        raise ValueError("参考图不存在或不在本地素材库中")
    return path.resolve()


def parent_for(body: dict) -> dict | None:
    rid = body.get("parent_result_id") or ""
    if rid:
        return find_result(str(rid))
    return None


def inspiration_source(body: dict, parent: dict | None) -> tuple[str, str]:
    display = str(body.get("inspire") or (parent or {}).get("inspire") or "")
    original = display
    if parent and display:
        inherited = [parent.get("inspire"), parent.get("source_inspire")]
        if any(p and Path(display).resolve() == Path(p).resolve() for p in inherited):
            original = parent.get("source_inspire") or display
    return display, original


def reference_plan(body: dict, char_id: str, parent: dict | None) -> list[dict]:
    mode = body.get("mode", "redo")
    if mode not in ("redo", "refine"):
        raise ValueError("生成模式不合法")
    plan = []
    if mode == "refine":
        if not parent:
            raise ValueError("精修需要先选择一张成图")
        plan.append({"path": str(checked_image(parent["path"])), "role": "base", "label": "当前成图 · 精修底图"})
    refs = ref_images(char_id)
    if not refs:
        raise ValueError("目标角色缺少身份参考图，请先补充 refs")
    plan.append({"path": str(checked_image(refs[0])), "role": "identity", "label": "角色身份"})
    send = body.get("send_inspire", True)
    if not isinstance(send, bool):
        raise ValueError("参考图开关必须为布尔值")
    source, original = inspiration_source(body, parent)
    if source:
        checked_image(source)
    if send:
        if not source:
            raise ValueError("请先选择灵感图，或明确关闭随请求发送灵感图")
        split = inspiration_vision(Path(original)).get("split_wide")
        if split is True or (isinstance(split, dict) and split.get("is_split_wide") is True):
            raise ValueError("该灵感属于已识别的宽图拆分组，当前单图入口不能只提交半张；请完整整理构图后使用")
        plan.append({"path": str(checked_image(source)), "role": "inspiration", "label": "灵感 · 构图与画风"})
    return [{**item, "order": i} for i, item in enumerate(plan, 1)]


def prepare(body: dict) -> dict:
    parent = parent_for(body)
    char_id = checked_character(body.get("character") or (parent or {}).get("char_id"))
    if parent and parent["char_id"] != char_id:
        raise ValueError("重做版本必须使用原目标角色；换角色请新建创作")
    plan = reference_plan(body, char_id, parent)
    bible = load_bible(char_id)
    positive, negative = build_prompt(bible, {
        "shot": body.get("shot") or (parent or {}).get("shot", ""),
        "pose": body.get("pose") or (parent or {}).get("pose", ""),
        "style": body.get("style") or (parent or {}).get("style", ""),
        "pose_ref": "attached" if any(p["role"] == "inspiration" for p in plan) else None,
    })
    prompt = positive + ("\nNegative: " + negative if negative else "")
    _, original = inspiration_source(body, parent)
    same_inputs = (parent and parent.get("mode", "redo") != "refine" and
                   parent.get("send_inspire") == any(p["role"] == "inspiration" for p in plan) and
                   all(key not in body or body[key] == parent.get(key, "") for key in ("shot", "pose", "style")) and
                   original == (parent.get("source_inspire") or parent.get("inspire")))
    source = "recorded" if same_inputs and parent.get("final_prompt") is not None else "reconstructed"
    if source == "recorded":
        prompt = parent["final_prompt"]
    if body.get("mode") == "refine":
        prompt = (
            "Edit the FIRST image, which is the selected generated artwork. Preserve its composition, "
            "character identity, expression and unmentioned details. Do not create a different scene.\n"
            "The SECOND image is identity guidance only; do not import its pose or background.\n"
        )
        if len(plan) > 2:
            prompt += "The THIRD image is style/composition inspiration only; do not copy its character identity.\n"
        prompt += "Requested changes: improve the image only as described below.\n"
        source = "refinement_template"
    return {"character": char_id, "final_prompt": prompt, "prompt_source": source, "references": plan}


def enqueue(body: dict) -> dict:
    client_id = str(body.get("client_request_id") or job_id())
    if len(client_id) > 100:
        raise ValueError("任务标识过长")
    with LOCK:
        duplicate = next((j for j in JOBS if j.get("client_request_id") == client_id), None)
        if duplicate:
            return public_job(duplicate)
    parent = parent_for(body)
    char_id = checked_character(body.get("character"))
    if parent and parent["char_id"] != char_id:
        raise ValueError("目标角色和原图不一致")
    plan = reference_plan(body, char_id, parent)
    settings = load_settings()
    model = str(body.get("model") or settings["model"])
    if model not in public_settings()["models"]:
        raise ValueError("请从模型列表选择模型")
    size = str(body.get("size") or settings["size"])
    if size not in set((cfg().get("size_presets") or {}).values()) | {settings["size"]}:
        raise ValueError("请从比例列表选择请求尺寸")
    quality = str(body.get("quality") or settings["quality"])
    if quality not in ("low", "medium", "high", "auto"):
        raise ValueError("质量参数不合法")
    if not isinstance(body.get("final_prompt"), str) or not body["final_prompt"].strip():
        raise ValueError("请先填写完整发送提示词")
    text = body["final_prompt"]
    if len(text) > 40000:
        raise ValueError("提示词过长，请缩短到 40000 字符以内")
    with LOCK:
        duplicate = next((j for j in JOBS if j.get("client_request_id") == client_id), None)
        if duplicate:
            return public_job(duplicate)
        jid = job_id()
        out_dir = original_dir(char_id)
        inputs = out_dir / "_inputs" / jid
        inputs.mkdir(parents=True)
        frozen = []
        for item in plan:
            src = Path(item["path"])
            dest = inputs / f"{item['order']:02d}_{item['role']}{src.suffix.lower()}"
            shutil.copy2(src, dest)
            frozen.append({**item, "source": str(src), "path": str(dest),
                           "sha256": hashlib.sha256(dest.read_bytes()).hexdigest()})
        display_source, source_inspire = inspiration_source(body, parent)
        display_input = ""
        if display_source:
            src = checked_image(display_source)
            display = inputs / ("display_inspiration" + src.suffix.lower())
            shutil.copy2(src, display)
            display_input = str(display)
        job = {
            "id": jid, "client_request_id": client_id, "character": char_id,
            "model": model, "size": size, "quality": quality, "final_prompt": text,
            "reference_inputs": frozen, "mode": body.get("mode", "redo"),
            "inspire": source_inspire, "display_input": display_input,
            "send_inspire": any(p["role"] == "inspiration" for p in frozen),
            "parent_result_id": (parent or {}).get("result_id", ""),
            "redo_of": (parent or {}).get("id", body.get("redo_of") or ""),
            "shot": str(body.get("shot") or ""), "pose": str(body.get("pose") or ""),
            "style": str(body.get("style") or ""),
            "out_dir": str(out_dir), "status": "queued", "error": "", "right": "", "left": "",
            "submitted_at": now_iso(), "connection_base": settings["base_url"],
            "connection_profile": settings.get("profile_id"), "key_source": settings.get("key_source"),
        }
        JOBS.append(job)
        try:
            persist_jobs()
        except OSError:
            JOBS.remove(job)
            raise ValueError("任务保存失败，本次没有提交生图请求")
    start_worker()
    return public_job(job)


def start_worker() -> None:
    global WORKER_STARTED
    with LOCK:
        if WORKER_STARTED:
            return
        WORKER_STARTED = True
    def loop() -> None:
        while True:
            with LOCK:
                current = next((j for j in JOBS if j["status"] == "queued"), None)
                if current:
                    current["status"] = "running"
                    try:
                        persist_jobs()
                    except OSError:
                        current.update(status="fail", error="无法保存任务状态，尚未提交生图请求")
                        current = None
            if current is None:
                time.sleep(0.4)
                continue
            run_job(current)
    threading.Thread(target=loop, daemon=True).start()


def run_job(job: dict) -> None:
    started = time.monotonic()
    settings = {}
    try:
        settings = load_settings()
        if (settings["base_url"] != job["connection_base"] or
                settings.get("profile_id") != job["connection_profile"] or
                settings.get("key_source") != job["key_source"]):
            raise ValueError("本机线路已改变，本任务已停止，请检查配置后重新提交")
        raw = generate_one(settings, load_bible(job["character"]), job)
        from PIL import Image
        with Image.open(io.BytesIO(raw)) as image:
            image.load()
            actual = {"width": image.width, "height": image.height, "format": image.format}
        ext = {"PNG": ".png", "JPEG": ".jpg", "WEBP": ".webp"}.get(actual["format"])
        if not ext:
            raise ValueError("返回的图片格式不支持")
        out_dir = Path(job["out_dir"])
        dest = out_dir / (job["id"] + ext)
        with dest.open("xb") as f:
            f.write(raw)
        warning = ""
        left = ""
        try:
            inspiration = next((p for p in job["reference_inputs"] if p["role"] == "inspiration"), None)
            display = job.get("display_input") or (inspiration or {}).get("path")
            if display:
                src = Path(display)
                copy = out_dir / ("left_" + job["id"] + src.suffix)
                shutil.copy2(src, copy)
                left = str(copy)
        except OSError:
            warning = "成图已保存，灵感副本复制失败；请求快照中的参考图仍保留"
        with LOCK:
            job.update(right=str(dest), left=left, bytes=len(raw), actual=actual,
                       sec=round(time.monotonic() - started, 1), status="ok",
                       error=warning, finished_at=now_iso())
            rec = {k: v for k, v in job.items() if k not in ("connection_base", "connection_profile", "key_source")}
            rec.update(ok=True, path=str(dest))
            try:
                with (out_dir / "run_log.jsonl").open("a", encoding="utf-8") as f:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            except OSError:
                job["error"] = "成图已保存，但运行日志写入失败；任务快照仍保留"
            try:
                persist_jobs()
            except OSError:
                job["error"] = "成图已保存，但任务状态写入失败；请检查磁盘空间和权限"
        log(f"studio {job['id']} ok {len(raw)}B")
    except (Exception, SystemExit) as exc:
        message = str(exc)
        secret = settings.get("api_key")
        if secret:
            message = message.replace(secret, "[redacted]")
        message = re.sub(r"sk-[A-Za-z0-9_-]+", "[redacted]", message)
        with LOCK:
            job.update(status="fail", error=message[:1200], sec=round(time.monotonic() - started, 1),
                       finished_at=now_iso())
            try:
                persist_jobs()
            except OSError:
                log("studio task state could not be saved")
        log(f"studio {job['id']} failed; no automatic retry")


class Handler(BaseHTTPRequestHandler):
    server_version = "fanart-studio/0.2"

    def log_message(self, fmt: str, *args) -> None:
        if not self.path.startswith(("/api/state", "/api/thumb", "/api/file")):
            log("%s - %s" % (self.address_string(), fmt % args))

    def _guard(self) -> bool:
        port = self.server.server_address[1]
        if (self.headers.get("Host") or "").lower() not in (f"127.0.0.1:{port}", f"localhost:{port}"):
            self._json(403, {"ok": False, "error": "仅限本机访问"})
            return False
        origin = (self.headers.get("Origin") or "").rstrip("/")
        if origin and origin not in (f"http://127.0.0.1:{port}", f"http://localhost:{port}"):
            self._json(403, {"ok": False, "error": "跨站请求已拒绝"})
            return False
        return True

    def _send(self, code: int, body: bytes, ctype: str, filename: str = "") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if filename:
            self.send_header("Content-Disposition", "attachment; filename*=UTF-8''" + urllib.parse.quote(filename))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, code: int, data) -> None:
        self._send(code, json.dumps(data, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def _body(self) -> dict:
        size = int(self.headers.get("Content-Length") or "0")
        if size < 0 or size > 250000:
            raise ValueError("请求过大")
        body = json.loads(self.rfile.read(size).decode("utf-8") or "{}")
        if not isinstance(body, dict):
            raise ValueError("JSON 必须是对象")
        return body

    def do_GET(self) -> None:
        if not self._guard():
            return
        try:
            self._get()
        except (ValueError, OSError, SystemExit) as exc:
            self._json(400, {"ok": False, "error": str(exc)})

    def _get(self) -> None:
        url = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(url.query)
        get = lambda name, default="": (qs.get(name) or [default])[0]
        if url.path in ("/", "/index.html"):
            self._send(200, (STATIC / "index.html").read_bytes(), "text/html; charset=utf-8")
        elif url.path.startswith("/static/"):
            target = (STATIC / url.path.removeprefix("/static/")).resolve()
            if not is_under(target, STATIC) or not target.is_file() or target.suffix not in (".js", ".css"):
                self._send(404, b"missing", "text/plain")
                return
            ctype = "text/javascript" if target.suffix == ".js" else "text/css"
            self._send(200, target.read_bytes(), ctype + "; charset=utf-8")
        elif url.path == "/api/state":
            with LOCK:
                jobs = [public_job(j) for j in JOBS[-80:]]
            self._json(200, {"ok": True, "library": str(library_root()), "settings": public_settings(),
                             "characters": pipeline_characters(), "jobs": jobs})
        elif url.path == "/api/results":
            self._json(200, {"ok": True, **list_results(get("series"), get("status", "all"))})
        elif url.path == "/api/editor":
            self._json(200, {"ok": True, "drafts": local_store("drafts.json"), "presets": local_store("presets.json")})
        elif url.path == "/api/inspire":
            limit = min(200, max(1, int(get("limit", "60"))))
            offset = max(0, int(get("offset", "0")))
            self._json(200, {"ok": True, **list_inspire(get("exclude"), get("series"), limit, offset, get("vision", "all")),
                             "counts": vision_counts(get("exclude"), get("series"))})
        elif url.path == "/api/draft":
            self._json(200, {"ok": True, **draft_from_inspire(checked_image(get("path")))})
        elif url.path in ("/api/file", "/api/thumb"):
            src = checked_image(get("path"))
            filename = src.name if get("download") == "1" else ""
            if url.path == "/api/thumb":
                src = ensure_thumb(src)
            ctype = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}[src.suffix.lower()]
            self._send(200, src.read_bytes(), ctype, filename)
        else:
            self._json(404, {"ok": False, "error": "接口不存在"})

    def do_POST(self) -> None:
        if not self._guard():
            return
        try:
            body = self._body()
            route = urllib.parse.urlparse(self.path).path
            if route == "/api/prepare":
                self._json(200, {"ok": True, **prepare(body)})
            elif route == "/api/generate":
                self._json(200, {"ok": True, "job": enqueue(body)})
            elif route == "/api/review":
                rid = str(body.get("result_id") or "")
                find_result(rid)
                if body.get("undo") is True:
                    review = undo_review(rid)
                else:
                    note = body.get("note")
                    if note is not None and (not isinstance(note, str) or len(note) > 4000):
                        raise ValueError("备注应为不超过 4000 字符的文字")
                    review = save_review(rid, str(body.get("status") or ""), note)
                self._json(200, {"ok": True, "review": review})
            elif route == "/api/draft/save":
                key, value = str(body.get("key") or ""), body.get("draft")
                if not key or len(key) > 600 or (value is not None and not isinstance(value, dict)):
                    raise ValueError("草稿格式不合法")
                save_draft(key, value)
                self._json(200, {"ok": True})
            elif route == "/api/preset":
                value = body.get("preset")
                if value is not None and not isinstance(value, dict):
                    raise ValueError("模板格式不合法")
                self._json(200, {"ok": True, "presets": save_preset(str(body.get("name") or ""), value)})
            elif route == "/api/cancel":
                with LOCK:
                    job = next((j for j in JOBS if j["id"] == body.get("id")), None)
                    if not job or job["status"] != "queued":
                        raise ValueError("只能取消尚未开始的任务")
                    job.update(status="cancelled", finished_at=now_iso())
                    persist_jobs()
                self._json(200, {"ok": True})
            else:
                self._json(404, {"ok": False, "error": "接口不存在"})
        except (ValueError, OSError, SystemExit) as exc:
            self._json(400, {"ok": False, "error": str(exc)})


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=int(cfg().get("studio_port") or 8788))
    args = parser.parse_args()
    restore_jobs()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    log(f"studio http://127.0.0.1:{args.port}/")
    server.serve_forever()


if __name__ == "__main__":
    main()
