#!/usr/bin/env python3
"""Vision screening + structured description for the 二创 library.

Reads images already collected on the desktop, asks a cheap vision model to
screen them and describe them in structured JSON, and writes the results back
as sidecar JSON. It never uploads character reference images and never calls the
image-generation endpoints.

Config comes from %USERPROFILE%/.zcode/oc-fanart-vision.local.json, which is
separate from the image-generation profile in image-edit.local.json. The key is
never printed. A failed POST is never retried automatically.
"""
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
from common import load_json  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
VISION_CFG = Path.home() / ".zcode" / "oc-fanart-vision.local.json"
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp"}
DEFAULT_LIBRARY = Path.home() / "Desktop" / "二创"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"

SYSTEM_PROMPT = (
    "你是动漫二创素材库的筛图与结构化描述助手。只返回合法 JSON，不要 Markdown 代码块，不要多余解释。"
    "只描述图中确实可见的内容，不确定的字段填 null 或空数组，不要凭角色名推测。"
    "visible_facts 只写肉眼可见的事实；interpretations 只写对情境或心理的推测，两者绝不能混放。"
)

IMAGE_PROMPT = (
    "这是一张可能用于动漫人物二创的构图参考素材。请判断它能否作为构图灵感，并做简短结构化描述。"
    "返回 JSON，字段必须包含："
    "usable(bool，能否作为构图灵感)、"
    "reason(string，主要判断依据)、"
    "contains_person(bool)、"
    "character_count(int，人物数量，没有人填 0)、"
    "contains_large_text(bool，仅当文字占据画面主体、几乎没有人物或可参考构图时填 true；"
    "插画里带标题、台词、水印或竖排装饰文字，只要主体仍是人物和画面构图，就填 false)、"
    "shot(string，景别与机位)、"
    "action(string，动作、手势与身体状态)、"
    "expression(string，表情与神态)、"
    "scene(string，地点、环境与前中后景)、"
    "lighting_and_palette(string，光线、色彩与氛围)、"
    "decorations(array of string，装饰、道具与设计元素)、"
    "visible_facts(array of string)、"
    "interpretations(array of string)。"
    "每个 string 字段不超过 60 字，数组最多 6 项，回答务必简短，确保 JSON 完整收尾。"
)

SPLIT_WIDE_PROMPT = (
    "下面按顺序给出同一篇笔记里的两张图。请判断它们是不是把一张过宽的图切成左右两半分别发布，"
    "滑动切换才是完整构图（split_wide）。返回 JSON，字段必须包含："
    "is_split_wide(bool)、"
    "confidence(0 到 1 的数字)、"
    "reason(string，判断依据)、"
    "left_contains_person(bool)、right_contains_person(bool)、"
    "combined_composition(string，若确为宽图对，描述合起来后的完整构图；否则填 null)。"
    "注意：两张独立的完整图、上下两格漫画、或情侣头像对，都不算 split_wide。"
    "回答尽量简短，各字符串字段不超过 80 字。"
)


def load_vision_settings() -> dict:
    if not VISION_CFG.exists():
        raise SystemExit(f"找不到视觉模型配置：{VISION_CFG}")
    cfg = load_json(VISION_CFG)
    base = str(cfg.get("base_url") or "").strip().rstrip("/")
    key = str(cfg.get("api_key") or "").strip()
    model = str(cfg.get("model") or "").strip()
    if not base or not key or not model:
        raise SystemExit("视觉模型配置缺少 base_url / api_key / model")
    return {"base_url": base, "api_key": key, "model": model}


def data_uri(path: Path) -> str:
    mime = mimetypes.guess_type(path.name)[0] or "image/webp"
    return f"data:{mime};base64," + base64.b64encode(path.read_bytes()).decode("ascii")


def call_vision(settings: dict, images: list[Path], prompt: str, timeout: int, retries: int) -> dict:
    content: list[dict] = [{"type": "text", "text": prompt}]
    for img in images:
        content.append({"type": "image_url", "image_url": {"url": data_uri(img)}})
    payload = {
        "model": settings["model"],
        "temperature": 0,
        "max_tokens": 1600,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": content},
        ],
    }
    req = urllib.request.Request(
        settings["base_url"] + "/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": "Bearer " + settings["api_key"],
            "Content-Type": "application/json",
            "User-Agent": UA,
        },
    )
    last_err: Exception | None = None
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            text = body["choices"][0]["message"]["content"]
            return {"ok": True, "raw": text, "usage": body.get("usage") or {}}
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:400]
            # HTTP errors are deterministic: never retry them.
            return {"ok": False, "error_kind": f"http_{exc.code}", "error": detail}
        except Exception as exc:  # network layer only
            last_err = exc
            if attempt < retries:
                time.sleep(3)
    return {"ok": False, "error_kind": "network", "error": str(last_err)[:300]}


def parse_json_text(text: str) -> dict | None:
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.split("```")[1] if "```" in text[3:] else text.strip("`")
        if text.lstrip().lower().startswith("json"):
            text = text.lstrip()[4:]
    start, end = text.find("{"), text.rfind("}")
    if start == -1:
        return None
    candidate = text[start : end + 1] if end > start else text[start:]
    for attempt in (candidate, repair_truncated_json(candidate)):
        if not attempt:
            continue
        try:
            obj = json.loads(attempt)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj
    return None


def repair_truncated_json(text: str) -> str:
    """Best-effort close of a JSON object the model cut off mid-value."""
    text = text.rstrip()
    # Trim trailing junk the model may append after an unfinished value,
    # e.g. a lone quote, comma or colon left dangling at the end.
    text = text.rstrip().rstrip("\"',: \n\t")
    # Now drop any trailing incomplete element and close open brackets.
    for cut in range(len(text), 0, -1):
        body = text[:cut].rstrip().rstrip(",")
        if not body:
            return ""
        open_stack: list[str] = []
        in_str = False
        escaped = False
        balanced = True
        for ch in body:
            if in_str:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch in "{[":
                open_stack.append(ch)
            elif ch in "}]":
                if not open_stack:
                    balanced = False
                    break
                open_stack.pop()
        if not balanced or in_str:
            continue
        closers = "".join("}" if c == "{" else "]" for c in reversed(open_stack))
        candidate = body + closers
        # Only accept a repair that actually parses; walking backwards finds the
        # longest valid prefix even when the tail is malformed.
        try:
            obj = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return candidate
    return ""


def iter_images(folder: Path) -> list[Path]:
    if not folder.is_dir():
        return []
    return [
        p
        for p in sorted(folder.iterdir())
        if p.is_file()
        and p.suffix.lower() in IMAGE_EXT
        and not p.name.startswith("_")  # 跳过调试截图、登录墙等非素材文件
        and "_vision" not in p.parts
    ]


def group_notes(folder: Path) -> list[dict]:
    """Yield note groups: a note folder holds one note; a flat folder holds singles."""
    groups: list[dict] = []
    subdirs = sorted(p for p in folder.iterdir() if p.is_dir()) if folder.is_dir() else []
    if subdirs:
        for d in subdirs:
            imgs = iter_images(d)
            if imgs:
                groups.append({"note_id": d.name, "folder": d, "images": imgs})
        flat = iter_images(folder)
        groups.extend({"note_id": p.stem, "folder": folder, "images": [p]} for p in flat)
    else:
        groups.extend({"note_id": p.stem, "folder": folder, "images": [p]} for p in iter_images(folder))
    return groups


def describe_group(settings: dict, group: dict, timeout: int, retries: int, out_dir: Path, force: bool = False) -> dict:
    images = group["images"]
    note_id = group["note_id"]
    dest = out_dir / f"{note_id}.json"
    prev: dict = {}
    if force and dest.exists():
        raise SystemExit(f"--force 不会覆盖已有结果：{dest}")
    if dest.exists():
        try:
            prev = load_json(dest)
        except (OSError, json.JSONDecodeError):
            prev = {}
    prev_by_file = {
        row.get("file"): row
        for row in (prev.get("images") or [])
        if isinstance(row, dict) and row.get("file")
    }
    results: list[dict] = []
    split_wide = None

    for i, img in enumerate(images, 1):
        old = prev_by_file.get(img.name)
        if old and old.get("parsed_ok"):
            row = dict(old)
            row["index"] = i
            row["n_in_note"] = len(images)
            row["cached"] = True
            results.append(row)
            print(f"    [{i}/{len(images)}] {img.name} -> cached", flush=True)
            continue
        res = call_vision(settings, [img], IMAGE_PROMPT, timeout, retries)
        row = {"file": img.name, "index": i, "n_in_note": len(images)}
        if res["ok"]:
            parsed = parse_json_text(res.get("raw", ""))
            if parsed:
                row.update(parsed)
                row["parsed_ok"] = True
            else:
                row["parsed_ok"] = False
                row["raw"] = res.get("raw", "")[:1200]
        else:
            row["parsed_ok"] = False
            row["error_kind"] = res.get("error_kind")
            row["error"] = res.get("error")
        row["usage"] = res.get("usage") or {}
        results.append(row)
        print(f"    [{i}/{len(images)}] {img.name} -> " + (
            "ok" if row.get("parsed_ok") else f"FAIL {row.get('error_kind') or 'parse'}"
        ), flush=True)

    if len(images) == 2:
        cached_split = prev.get("split_wide") if isinstance(prev.get("split_wide"), dict) else None
        if cached_split and cached_split.get("is_split_wide") is not None:
            split_wide = cached_split
            print("    split_wide -> cached")
        else:
            res = call_vision(settings, images, SPLIT_WIDE_PROMPT, timeout, retries)
            if res["ok"]:
                parsed = parse_json_text(res.get("raw", ""))
                if parsed:
                    split_wide = parsed
                else:
                    split_wide = {"is_split_wide": None, "reason": "parse_failed", "raw": res.get("raw", "")[:800]}
            else:
                split_wide = {"is_split_wide": None, "reason": res.get("error_kind"), "error": res.get("error")}

    out = {
        "schema_version": "0.1",
        "note_id": note_id,
        "folder": str(group["folder"]),
        "model": settings["model"],
        "image_count": len(images),
        "split_wide": split_wide,
        "images": results,
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="视觉筛图 + 结构化描述（不调用生图接口）")
    ap.add_argument("--dir", required=True, help="要处理的文件夹；含子目录时每个子目录当作一篇笔记")
    ap.add_argument("--out", default="", help="结果输出目录；默认写到桌面库 <dir>/_vision/")
    ap.add_argument("--limit-groups", type=int, default=0, help="最多处理几组，0=全部")
    ap.add_argument("--limit-images", type=int, default=0, help="每组最多处理几张图，0=全部")
    ap.add_argument("--timeout", type=int, default=180)
    ap.add_argument("--retries", type=int, default=1, help="仅网络层失败的重试次数；HTTP 错误不重试")
    ap.add_argument("--dry-run", action="store_true", help="只列出将要处理的组，不调用模型")
    args = ap.parse_args()

    folder = Path(args.dir)
    if not folder.is_dir():
        raise SystemExit(f"不是文件夹：{folder}")
    groups = group_notes(folder)
    if args.limit_groups:
        groups = groups[: args.limit_groups]
    if args.limit_images:
        for g in groups:
            g["images"] = g["images"][: args.limit_images]
    if not groups:
        raise SystemExit("没有找到图片")

    total = sum(len(g["images"]) for g in groups)
    if args.dry_run:
        for g in groups:
            print(f"  {g['note_id']}: {len(g['images'])} 张")
        print(f"groups={len(groups)} images={total}（dry-run，未调用模型）")
        return

    settings = load_vision_settings()
    out_dir = Path(args.out) if args.out else folder / "_vision"
    print(f"model={settings['model']} base={settings['base_url']} groups={len(groups)} images={total}")
    print("说明：只上传构图库图片，不上传角色身份图；POST 失败不自动重试。")
    for i, g in enumerate(groups, 1):
        print(f"[{i}/{len(groups)}] {g['note_id']} ({len(g['images'])} 张)", flush=True)
        row = describe_group(settings, g, args.timeout, args.retries, out_dir)
        sw = row.get("split_wide") or {}
        if sw:
            print(f"    split_wide={sw.get('is_split_wide')} conf={sw.get('confidence')}")
    print(f"结果写入 {out_dir}")


if __name__ == "__main__":
    main()
