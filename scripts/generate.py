#!/usr/bin/env python3
"""Generate one batch of character images from jobs/*.json."""
from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (  # noqa: E402
    ROOT,
    headers,
    load_bible,
    load_json,
    load_settings,
    pose_path,
    ref_images,
)


def build_prompt(bible: dict, job: dict) -> tuple[str, str]:
    """Compose the final prompt.

    When an inspiration image is attached it becomes the SECOND uploaded image,
    so the prompt must state each image's job explicitly: the first image owns
    identity (face, hair, ears, outfit, colors), the second is borrowed only for
    camera, pose, composition, lighting and rendering. Without this split the
    model tends to copy the other character's face.
    """
    has_ref_image = bool(job.get("pose_ref"))
    parts = [
        bible.get("style") or "anime illustration",
        bible.get("lock") or "",
        job.get("shot") or "",
        job.get("pose") or "",
        job.get("style") or job.get("mood") or "",
    ]
    if has_ref_image:
        parts.extend([
            "The FIRST reference image defines the character identity: keep that exact face, hair, "
            "ears, eye color, outfit and color scheme.",
            "The SECOND reference image is ONLY a composition and rendering reference: borrow its camera "
            "angle, body pose, gesture, framing, lighting, palette and painting style.",
            "Do NOT copy the second image's character: never use its face, facial features, hairstyle, "
            "hair color, outfit, accessories or species. The character must remain the one from the first image.",
            "If the two references conflict, identity always follows the first image and composition follows the second.",
        ])
    else:
        parts.extend([
            "same character as the first reference image, keep that face, hair, ears, outfit and colors",
            "do not copy another character's face or hairstyle",
        ])
    prompt = ", ".join(p.strip() for p in parts if p and str(p).strip())
    negative = bible.get("forbid") or "watermark, text, extra fingers"
    if has_ref_image:
        negative += (
            ", copying the second reference image's face, changing the character's hair color, "
            "changing the character's outfit, mixing identities of two different characters"
        )
    return prompt, negative


def post_json(url: str, payload: dict, settings: dict) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            **headers(settings),
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=settings["timeout_sec"]) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {e.code} {url}: {body[:800]}") from e


def post_multipart(url: str, fields: dict, files: list[tuple[str, Path]], settings: dict) -> dict:
    boundary = "----FanartBoundary7MA4YWxkTrZu0gW"
    chunks: list[bytes] = []
    for k, v in fields.items():
        chunks.append(f"--{boundary}\r\n".encode())
        chunks.append(f'Content-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
    for field, path in files:
        raw = path.read_bytes()
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        chunks.append(f"--{boundary}\r\n".encode())
        chunks.append(
            f'Content-Disposition: form-data; name="{field}"; filename="{path.name}"\r\n'.encode()
        )
        chunks.append(f"Content-Type: {mime}\r\n\r\n".encode())
        chunks.append(raw)
        chunks.append(b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode())
    body = b"".join(chunks)
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            **headers(settings),
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=settings["timeout_sec"]) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        err = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {e.code} {url}: {err[:800]}") from e


def decode_image(item: dict) -> bytes:
    b64 = item.get("b64_json")
    if b64:
        return base64.b64decode(b64)
    url = item.get("url")
    if not url:
        raise RuntimeError(f"响应里没有图片: {list(item)}")
    with urllib.request.urlopen(url, timeout=60) as resp:
        return resp.read()


def resolve_prompt(bible: dict, job: dict) -> str:
    if job.get("final_prompt") is not None:
        text = str(job["final_prompt"])
        if not text.strip():
            raise ValueError("提示词不能为空")
        return text
    if job.get("prompt_override"):
        prompt = str(job["prompt_override"])
        negative = (str(job["negative_override"]) if job.get("negative_override") is not None
                    else str(bible.get("forbid") or "watermark, text, extra fingers"))
    else:
        prompt, negative = build_prompt(bible, job)
    return prompt + (f"\nNegative: {negative}" if negative else "")


def generate_one(settings: dict, bible: dict, job: dict) -> bytes:
    prompt = resolve_prompt(bible, job)
    files: list[tuple[str, Path]] = []
    if "reference_inputs" in job:
        for item in job["reference_inputs"]:
            path = Path(item["path"])
            if not path.is_file():
                raise ValueError("本次参考图已丢失，请重新选择")
            files.append(("image", path))
    else:
        refs = ref_images(job["character"])
        pose = pose_path(job.get("pose_ref"))
        if refs:
            files.append(("image", refs[0]))
        if pose:
            files.append(("image", pose))
    size = str(job.get("size") or settings["size"])
    quality = str(job.get("quality") or settings["quality"])
    fields = {
        "model": str(job.get("model") or settings["model"]),
        "prompt": prompt,
        "size": size,
        "quality": quality,
    }
    if files:
        result = post_multipart(f"{settings['base_url']}/images/edits", fields, files, settings)
    else:
        result = post_json(f"{settings['base_url']}/images/generations", {**fields, "n": 1}, settings)
    data = result.get("data") or []
    if not data:
        raise RuntimeError("接口返回空图片结果")
    return decode_image(data[0])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--jobs", default=str(ROOT / "jobs" / "batch01.json"))
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 条，0=全部")
    ap.add_argument("--out", default="", help="输出目录，默认仓库 out/")
    args = ap.parse_args()

    settings = load_settings()
    batch = load_json(Path(args.jobs))
    jobs = batch["jobs"]
    if args.limit:
        jobs = jobs[: args.limit]

    out_dir = Path(args.out) if args.out else (ROOT / "out")
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "run_log.jsonl"

    print(
        f"model={settings['model']} base={settings['base_url']} "
        f"key_source={settings.get('key_source')} profile={settings.get('profile_id') or '-'} "
        f"jobs={len(jobs)}"
    )
    for i, job in enumerate(jobs, 1):
        jid = job.get("id") or f"job{i:03d}"
        dest = out_dir / f"{jid}.png"
        rec = {"id": jid, "ok": False, "path": str(dest)}
        print(f"[{i}/{len(jobs)}] {jid} …", flush=True)
        t0 = time.time()
        try:
            bible = load_bible(job["character"])
            raw = generate_one(settings, bible, job)
            if len(raw) < settings["min_bytes"]:
                raise RuntimeError(f"图片过小 {len(raw)} bytes")
            out_dir.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(raw)
            rec.update(
                ok=True,
                bytes=len(raw),
                sec=round(time.time() - t0, 1),
                inspire=job.get("inspire") or "",
                shot=job.get("shot") or "",
                pose=job.get("pose") or "",
            )
            print(f"  ok {dest.name} {rec['bytes']}B {rec['sec']}s")
        except Exception as e:
            rec.update(error=str(e), sec=round(time.time() - t0, 1))
            print(f"  FAIL {e}")
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError as e:
            print(f"  log write failed: {e}")


if __name__ == "__main__":
    main()
