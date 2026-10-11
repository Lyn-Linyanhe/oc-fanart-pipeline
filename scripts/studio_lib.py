#!/usr/bin/env python3
"""Desktop 二创 library helpers for the studio."""
from __future__ import annotations

import hashlib
import json
import re
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


def load_reviews() -> dict:
    p = review_path()
    if not p.is_file():
        return {}
    try:
        data = load_json(p)
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_review(result_id: str, status: str, note: str = "") -> dict:
    import time
    reviews = load_reviews()
    reviews[result_id] = {
        "status": status,
        "note": note,
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    review_path().write_text(
        json.dumps(reviews, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return reviews[result_id]


def _log_index(out_dir: Path) -> dict[str, dict]:
    """Map result id -> latest run_log record for one output folder."""
    log = out_dir / "run_log.jsonl"
    out: dict[str, dict] = {}
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


def list_results(series_filter: str = "", status_filter: str = "all") -> dict:
    """Scan generated results across the desktop library.

    A result is an image produced by the pipeline (not a `left_` inspiration copy).
    Review state is stored in `_results/review.json` keyed by a stable id.
    """
    reviews = load_reviews()
    items: list[dict] = []
    for out_dir in _result_dirs():
        logs = _log_index(out_dir)
        folder_label = out_dir.name
        series = out_dir.parts[out_dir.parts.index("二创") + 1] if "二创" in out_dir.parts else ""
        for img in iter_images(out_dir):
            if img.name.startswith("left_"):
                continue
            rid = img.stem
            rec = logs.get(rid) or {}
            inspire = str(rec.get("inspire") or "")
            if not inspire:
                # Fall back to the sibling left_<id>.<ext> copy.
                cands = sorted(out_dir.glob(f"left_{rid}.*"))
                if cands:
                    inspire = str(cands[0])
            result_id = f"{series}|{out_dir.parents[1].name}|{folder_label}|{rid}"
            review = reviews.get(result_id) or {}
            character = out_dir.parents[1].name if len(out_dir.parents) > 1 else ""
            char_id = _char_id_for(series, character)
            shot = str(rec.get("shot") or "")
            pose = str(rec.get("pose") or "")
            style = str(rec.get("style") or "")
            send_ref = bool(rec.get("send_inspire")) or bool(inspire)
            prompt_preview, negative_preview = _prompt_for(char_id, shot, pose, style, send_ref)
            item = {
                "result_id": result_id,
                "id": rid,
                "series": series,
                "character": character,
                "char_id": char_id,
                "folder": folder_label,
                "path": str(img),
                "bytes": img.stat().st_size,
                "mtime": img.stat().st_mtime,
                "inspire": inspire,
                "shot": shot,
                "pose": pose,
                "style": style,
                "send_inspire": send_ref,
                "prompt_preview": prompt_preview,
                "negative_preview": negative_preview,
                "sec": rec.get("sec"),
                "has_params": bool(shot or pose or style or inspire),
                "review": review or {"status": "unreviewed", "note": "", "updated_at": ""},
            }
            if series_filter and item["series"] != series_filter:
                continue
            st = item["review"].get("status") or "unreviewed"
            if status_filter not in ("", "all") and st != status_filter:
                continue
            items.append(item)
    items.sort(key=lambda x: (x["series"], x["folder"], x["mtime"], x["id"]))
    counts = {"all": 0, "unreviewed": 0, "pass": 0, "manual_review": 0, "quarantine": 0}
    for it in items:
        counts["all"] += 1
        st = it["review"].get("status") or "unreviewed"
        if st in counts:
            counts[st] += 1
    return {"total": len(items), "items": items, "counts": counts}


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
    import time
    return time.strftime("s%m%d-%H%M%S")
