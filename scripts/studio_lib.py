#!/usr/bin/env python3
"""Desktop 二创 library helpers for the studio."""
from __future__ import annotations

import hashlib
import json
import re
import threading
import uuid
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

from common import ROOT, character_dir, load_json, load_settings

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp"}


def cfg() -> dict:
    return load_json(ROOT / "config.json")


def library_root() -> Path:
    raw = str(cfg().get("library") or "").strip()
    p = Path(raw) if raw else Path.home() / "Desktop" / "二创"
    return p


def is_under(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except (ValueError, OSError):
        return False


def allowed_path(path: Path) -> bool:
    path = path.resolve()
    return is_under(path, library_root()) or is_under(path, ROOT)


def iter_images(folder: Path) -> list[Path]:
    if not folder.is_dir():
        return []
    out = []
    for p in sorted(folder.iterdir()):
        if p.is_file() and p.suffix.lower() in IMAGE_EXT:
            out.append(p)
    return out


def all_images(folder: Path) -> list[Path]:
    """Recursive image listing: supports the new one-note-one-folder layout."""
    if not folder.is_dir():
        return []
    out = []
    for p in sorted(folder.rglob("*")):
        if not p.is_file() or p.suffix.lower() not in IMAGE_EXT:
            continue
        if "_vision" in p.parts or any(part.startswith("_") for part in p.parts):
            continue
        out.append(p)
    return out


def vision_index(folder: Path) -> dict[str, dict]:
    """Map image absolute path -> vision row, from <folder>/_vision/*.json."""
    out: dict[str, dict] = {}
    vdir = folder / "_vision"
    if not vdir.is_dir():
        return out
    for jf in sorted(vdir.glob("*.json")):
        try:
            data = load_json(jf)
        except (OSError, json.JSONDecodeError):
            continue
        note_folder = Path(str(data.get("folder") or folder))
        for row in (data.get("images") or []):
            if not isinstance(row, dict) or not row.get("file"):
                continue
            key = str((note_folder / str(row["file"])).resolve())
            merged = dict(row)
            merged["split_wide"] = data.get("split_wide")
            merged["note_id"] = data.get("note_id") or jf.stem
            out[key] = merged
    return out


def inspiration_vision(path: Path) -> dict:
    resolved = str(path.resolve())
    for folder in path.resolve().parents:
        if not is_under(folder, library_root()):
            break
        row = vision_index(folder).get(resolved)
        if row:
            return row
    return {}


def pipeline_characters() -> list[dict]:
    chars_dir = ROOT / "characters"
    rows = []
    if not chars_dir.is_dir():
        return rows
    for d in sorted(chars_dir.iterdir()):
        bible_path = d / "bible.json"
        if not bible_path.is_file():
            continue
        try:
            bible = load_json(bible_path)
        except (OSError, json.JSONDecodeError):
            continue
        refs = character_dir(d.name) / "refs"
        images = iter_images(refs)
        desk = bible.get("desktop") if isinstance(bible.get("desktop"), dict) else {}
        rows.append({
            "id": d.name,
            "name": bible.get("name") or d.name,
            "series": desk.get("series") or "",
            "desktop_character": desk.get("character") or "",
            "has_ref": bool(images),
            "ref_count": len(images),
            "ref_file": images[0].name if images else "",
            "ref_path": str(images[0]) if images else "",
            "lock": (bible.get("lock") or "")[:180],
        })
    return rows


def series_dirs() -> list[Path]:
    root = library_root()
    if not root.is_dir():
        return []
    return [p for p in sorted(root.iterdir()) if p.is_dir() and not p.name.startswith(".")]


def inspire_folders(series: Path) -> list[Path]:
    folders = []
    for name in ("他山之石",):
        p = series / name
        if p.is_dir():
            folders.append(p)
    un = series / "未分类" / "他山之石"
    if un.is_dir():
        folders.append(un)
    roles = series / "角色"
    if roles.is_dir():
        for ch in sorted(roles.iterdir()):
            p = ch / "他山之石"
            if p.is_dir():
                folders.append(p)
    return folders


def _catalog_lookup(folder: Path) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for name in ("catalog.json", "index.json"):
        p = folder / name
        if not p.is_file():
            continue
        try:
            data = load_json(p)
        except (OSError, json.JSONDecodeError):
            continue
        rows = []
        if isinstance(data, dict):
            rows = data.get("notes") or data.get("items") or []
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            fn = str(row.get("file") or "")
            if fn:
                out[fn] = row
    return out


def draft_from_inspire(path: Path) -> dict:
    """Split a cheap draft into pose vs style. Not a vision model."""
    title = path.stem
    folder = path.parent
    cat = _catalog_lookup(folder).get(path.name) or {}
    title = str(cat.get("title") or title)
    desc = str(cat.get("desc") or "")
    blob = f"{title} {desc}"
    shot = "medium shot"
    if re.search(r"特写|close", blob, re.I):
        shot = "close-up"
    elif re.search(r"全身|full", blob, re.I):
        shot = "full body"
    elif re.search(r"侧|profile", blob, re.I):
        shot = "side profile bust"
    elif re.search(r"背影|from behind", blob, re.I):
        shot = "over-the-shoulder"
    pose = desc or title
    style = "anime illustration, keep the inspiration's lighting and color mood, clean character rendering"
    if re.search(r"水彩|water", blob, re.I):
        style = "soft watercolor lighting, thin washes, muted palette"
    elif re.search(r"漫画|分镜|manga", blob, re.I):
        style = "manga panel lighting, strong contrast, graphic ink"
    elif re.search(r"线稿|速写|sketch", blob, re.I):
        style = "sketchy colored lineart, paper-like background"
    return {
        "shot": shot,
        "pose": pose[:180],
        "style": style,
        "title": title,
        "series": folder.parts[-3] if len(folder.parts) >= 3 else "",
    }


def list_inspire(
    exclude_series: str = "",
    series: str = "",
    limit: int = 80,
    offset: int = 0,
    vision_filter: str = "all",
) -> dict:
    items = []
    series_names = []
    for sdir in series_dirs():
        series_names.append(sdir.name)
        if exclude_series and sdir.name == exclude_series:
            continue
        if series and sdir.name != series:
            continue
        for folder in inspire_folders(sdir):
            catalog = _catalog_lookup(folder)
            vindex = vision_index(folder)
            for img in all_images(folder):
                if img.name.startswith("_"):
                    continue
                meta = catalog.get(img.name) or {}
                v = vindex.get(str(img.resolve())) or {}
                item = {
                    "path": str(img),
                    "file": img.name,
                    "series": sdir.name,
                    "bytes": img.stat().st_size,
                    "folder": str(folder),
                    "title": str(meta.get("title") or img.stem),
                    "desc": str(meta.get("desc") or ""),
                    "vision": {
                        "done": bool(v),
                        "usable": v.get("usable"),
                        "contains_person": v.get("contains_person"),
                        "contains_large_text": v.get("contains_large_text"),
                        "character_count": v.get("character_count"),
                        "shot": v.get("shot"),
                        "action": v.get("action"),
                        "expression": v.get("expression"),
                        "scene": v.get("scene"),
                        "lighting_and_palette": v.get("lighting_and_palette"),
                        "decorations": v.get("decorations") or [],
                        "visible_facts": v.get("visible_facts") or [],
                        "interpretations": v.get("interpretations") or [],
                        "reason": v.get("reason"),
                        "note_id": v.get("note_id"),
                        "split_wide": v.get("split_wide"),
                    },
                }
                if not _vision_match(item["vision"], vision_filter, item["vision"]["done"]):
                    continue
                items.append(item)
    total = len(items)
    page = items[offset: offset + limit]
    return {
        "total": total,
        "offset": offset,
        "items": page,
        "series": [n for n in series_names if n != exclude_series],
    }


def _vision_match(v: dict, mode: str, done: bool) -> bool:
    """Filter by vision state.

    `done` must be passed explicitly by the caller, because "was this image
    screened" is known from the index lookup, not from the row contents.
    """
    mode = (mode or "all").lower()
    if mode in ("", "all"):
        return True
    if mode == "done":
        return done
    if mode == "todo":
        return not done
    if not done:
        return False
    if mode == "usable":
        return bool(v.get("usable")) and v.get("contains_person") is not False
    if mode == "person":
        return v.get("contains_person") is True
    if mode == "no_person":
        return v.get("contains_person") is False
    if mode == "text":
        return v.get("contains_large_text") is True
    if mode == "maybe":
        return v.get("usable") is not True
    return True



def vision_counts(exclude_series: str = "", series: str = "") -> dict:
    """Counts per filter bucket across the scanned library."""
    counts = {"all": 0, "done": 0, "todo": 0, "usable": 0, "person": 0,
              "no_person": 0, "text": 0, "maybe": 0}
    for sdir in series_dirs():
        if exclude_series and sdir.name == exclude_series:
            continue
        if series and sdir.name != series:
            continue
        for folder in inspire_folders(sdir):
            vindex = vision_index(folder)
            for img in all_images(folder):
                if img.name.startswith("_"):
                    continue
                v = vindex.get(str(img.resolve())) or {}
                counts["all"] += 1
                for mode in ("done", "todo", "usable", "person", "no_person", "text", "maybe"):
                    if _vision_match(v, mode, bool(v)):
                        counts[mode] += 1
    return counts


def results_root() -> Path:
    """Where generated results and their review state live."""
    root = library_root() / "_results"
    root.mkdir(parents=True, exist_ok=True)
    return root


def review_path() -> Path:
    return results_root() / "review.json"


REVIEW_STATUSES = ("unreviewed", "pass", "manual_review", "quarantine")
STORE_LOCK = threading.RLock()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def atomic_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


def local_store(name: str) -> dict:
    path = results_root() / name
    if not path.exists():
        return {}
    data = load_json(path)
    if not isinstance(data, dict):
        raise ValueError(f"{name} 格式错误，请保留文件后检查")
    return data


def load_reviews() -> dict:
    return local_store("review.json")


def save_review(result_id: str, status: str, note: str | None = None) -> dict:
    if status not in REVIEW_STATUSES:
        raise ValueError("审核状态不合法")
    with STORE_LOCK:
        reviews = load_reviews()
        old = reviews.get(result_id) or {"status": "unreviewed", "note": ""}
        history = list(old.get("history") or [])
        history.append({k: old.get(k) for k in ("status", "note", "updated_at")})
        reviews[result_id] = {
            "status": status,
            "note": old.get("note", "") if note is None else note,
            "updated_at": now_iso(),
            "history": history[-100:],
        }
        atomic_json(review_path(), reviews)
        return reviews[result_id]


def undo_review(result_id: str) -> dict:
    with STORE_LOCK:
        reviews = load_reviews()
        old = reviews.get(result_id) or {}
        history = list(old.get("history") or [])
        if not history:
            raise ValueError("这张图没有可撤销的审核操作")
        previous = history.pop()
        previous.update(history=history, updated_at=now_iso())
        reviews[result_id] = previous
        atomic_json(review_path(), reviews)
        return previous


def save_draft(key: str, value: dict | None) -> None:
    with STORE_LOCK:
        drafts = local_store("drafts.json")
        if value is None:
            drafts.pop(key, None)
        else:
            drafts[key] = {**value, "updated_at": now_iso()}
        atomic_json(results_root() / "drafts.json", drafts)


def save_preset(name: str, value: dict | None) -> dict:
    if not name.strip() or len(name) > 80:
        raise ValueError("模板名称应为 1～80 个字")
    with STORE_LOCK:
        presets = local_store("presets.json")
        if value is None:
            presets.pop(name, None)
        else:
            presets[name] = {**value, "updated_at": now_iso()}
        atomic_json(results_root() / "presets.json", presets)
        return presets


def _log_index(out_dir: Path) -> dict[str, dict]:
    """Map result id -> latest run_log record for one output folder."""
    log = out_dir / "run_log.jsonl"
    out: dict[str, dict] = {
        j["id"]: dict(j, ok=True)
        for j in local_store("jobs.json").values()
        if isinstance(j, dict) and j.get("id") and j.get("out_dir") == str(out_dir)
    }
    if not log.is_file():
        return out
    try:
        lines = log.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return out
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(rec, dict) or not rec.get("ok"):
            continue
        rid = str(rec.get("id") or "")
        if rid:
            out[rid] = rec
    return out


def _result_dirs() -> list[Path]:
    """Every 我的原创/ out dir that may hold generated images.

    Only result folders count: `refs/` holds identity references, not pipeline
    output, and would otherwise be listed as if it were generated.
    """
    dirs: list[Path] = []
    root = library_root()
    if not root.is_dir():
        return dirs
    for series in series_dirs():
        roles = series / "角色"
        if not roles.is_dir():
            continue
        for ch in sorted(roles.iterdir()):
            own = ch / "我的原创"
            if not own.is_dir():
                continue
            for d in sorted(own.iterdir()):
                if not d.is_dir() or d.name == "refs":
                    continue
                dirs.append(d)
    return dirs


def _prompt_for(char_id: str, shot: str, pose: str, style: str, send_ref: bool) -> tuple[str, str]:
    """Rebuild the prompt a result would use, so the UI can show/edit it."""
    if not char_id:
        return ("", "")
    try:
        from generate import build_prompt
        bible = load_json(character_dir(char_id) / "bible.json")
    except (OSError, json.JSONDecodeError, ImportError, SystemExit):
        return ("", "")
    return build_prompt(bible, {
        "shot": shot, "pose": pose, "style": style,
        "pose_ref": "1" if send_ref else "",
    })


def _char_id_for(series: str, character_name: str) -> str:
    """Reverse-map a desktop character folder name to a pipeline character id."""
    if not character_name:
        return ""
    for row in pipeline_characters():
        if row.get("desktop_character") == character_name:
            if not series or row.get("series") == series:
                return str(row.get("id") or "")
    return ""


@lru_cache(maxsize=1024)
def _image_info(path: str, mtime: int, size: int) -> dict:
    try:
        from PIL import Image
        with Image.open(path) as im:
            return {"width": im.width, "height": im.height, "format": im.format}
    except (OSError, ImportError):
        return {}


def list_results(series_filter: str = "", status_filter: str = "all") -> dict:
    reviews = load_reviews()
    characters = pipeline_characters()
    items: list[dict] = []
    for out_dir in _result_dirs():
        try:
            relative = out_dir.relative_to(library_root())
            series = relative.parts[0]
        except ValueError:
            continue
        character = out_dir.parents[1].name
        char_id = next((c["id"] for c in characters
                        if c["series"] == series and c["desktop_character"] == character), "")
        logs = _log_index(out_dir)
        for img in iter_images(out_dir):
            if img.name.startswith(("left_", "_")):
                continue
            rid = img.stem
            rec = logs.get(rid) or {}
            source = str(rec.get("inspire") or "")
            copies = [p for p in sorted(out_dir.glob(f"left_{rid}.*")) if p.suffix.lower() in IMAGE_EXT]
            inspire = str(copies[0]) if copies else str(rec.get("display_input") or source)
            if inspire and (not allowed_path(Path(inspire)) or not Path(inspire).is_file()):
                inspire = ""
            result_id = f"{series}|{character}|{out_dir.name}|{rid}"
            review = reviews.get(result_id) or {"status": "unreviewed", "note": "", "updated_at": ""}
            shot, pose = str(rec.get("shot") or ""), str(rec.get("pose") or "")
            style = str(rec.get("style") or rec.get("mood") or "")
            p, n = _prompt_for(char_id, shot, pose, style, bool(inspire))
            exact = rec.get("final_prompt")
            if exact is None and rec.get("prompt_override"):
                exact = str(rec["prompt_override"])
                if rec.get("negative_override"):
                    exact += "\nNegative: " + str(rec["negative_override"])
            stat = img.stat()
            info = _image_info(str(img), stat.st_mtime_ns, stat.st_size)
            items.append({
                "result_id": result_id, "id": rid, "series": series,
                "character": character, "char_id": char_id, "folder": out_dir.name,
                "path": str(img), "bytes": stat.st_size, "mtime": stat.st_mtime,
                "inspire": inspire, "source_inspire": source,
                "shot": shot, "pose": pose, "style": style,
                "send_inspire": rec.get("send_inspire") if isinstance(rec.get("send_inspire"), bool) else None,
                "final_prompt": exact,
                "prompt_source": "recorded" if exact is not None else "reconstructed",
                "suggested_prompt": exact if exact is not None else p + ("\nNegative: " + n if n else ""),
                "prompt_preview": p, "negative_preview": n,
                "model": rec.get("model"), "size": rec.get("size") or None,
                "quality": rec.get("quality") or None,
                "reference_inputs": rec.get("reference_inputs") or [],
                "mode": rec.get("mode"), "redo_of": rec.get("redo_of") or "",
                "parent_result_id": rec.get("parent_result_id") or "",
                "sec": rec.get("sec"), "actual": info, "task_status": rec.get("status"),
                "has_params": exact is not None or bool(shot or pose or style),
                "review": review,
            })
    by_id = {it["result_id"]: it for it in items}
    for it in items:
        if not it["parent_result_id"] and it["redo_of"]:
            parents = [p for p in items if p["id"] == it["redo_of"] and p["char_id"] == it["char_id"]]
            if len(parents) == 1:
                it["parent_result_id"] = parents[0]["result_id"]
        parent = by_id.get(it["parent_result_id"])
        it["parent_path"] = parent["path"] if parent else ""
        it["parent_id"] = parent["id"] if parent else ""
    # Resolve children once legacy parent ids have all been mapped.
    for it in items:
        it["children"] = [c["result_id"] for c in items if c["parent_result_id"] == it["result_id"]]
    items.sort(key=lambda it: (-it["mtime"], it["result_id"]))
    series_names = sorted({it["series"] for it in items})
    candidates = [it for it in items if not series_filter or it["series"] == series_filter]
    counts = {"all": len(candidates), **{s: 0 for s in REVIEW_STATUSES}}
    for it in candidates:
        st = it["review"].get("status", "unreviewed")
        if st in counts:
            counts[st] += 1
    page = [it for it in candidates if status_filter in ("", "all") or it["review"]["status"] == status_filter]
    return {"total": len(page), "items": page, "counts": counts, "series": series_names}


def find_result(result_id: str) -> dict:
    item = next((it for it in list_results()["items"] if it["result_id"] == result_id), None)
    if not item:
        raise ValueError("结果不存在或已移走，请刷新列表")
    return item


def original_dir(character_id: str) -> Path:
    bible_path = character_dir(character_id) / "bible.json"
    series = ""
    name = character_id
    if bible_path.is_file():
        bible = load_json(bible_path)
        desk = bible.get("desktop") if isinstance(bible.get("desktop"), dict) else {}
        series = str(desk.get("series") or "")
        name = str(desk.get("character") or bible.get("name") or character_id)
    if series:
        dest = library_root() / series / "角色" / name / "我的原创" / "studio"
    else:
        dest = ROOT / "out" / "studio" / character_id
    dest.mkdir(parents=True, exist_ok=True)
    return dest


def thumb_path(src: Path) -> Path:
    key = f"{src.resolve()}|{src.stat().st_mtime_ns}"
    h = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
    cache = ROOT / ".studio" / "thumbs"
    cache.mkdir(parents=True, exist_ok=True)
    return cache / f"{h}.jpg"


def ensure_thumb(src: Path) -> Path:
    dest = thumb_path(src)
    if dest.is_file() and dest.stat().st_mtime >= src.stat().st_mtime:
        return dest
    try:
        from PIL import Image
    except ImportError:
        return src
    im = Image.open(src).convert("RGB")
    im.thumbnail((360, 480))
    im.save(dest, "JPEG", quality=82)
    return dest


def job_id() -> str:
    return datetime.now().strftime("s%m%d-%H%M%S-") + uuid.uuid4().hex[:8]
