#!/usr/bin/env python3
"""Shared config / HTTP helpers for the fanart pipeline."""
from __future__ import annotations

import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config.json"
LOCAL_KEY_PATH = Path.home() / ".zcode" / "image-edit.local.json"


def load_json(path: Path) -> dict:
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def normalize_base(base: str) -> str:
    base = (base or "").strip().rstrip("/")
    if base and not base.endswith("/v1"):
        base += "/v1"
    return base


def _active_profile(local: dict) -> dict:
    """Pick the active profile as a (base, key, model) pair. Never mix sources."""
    profiles = local.get("profiles")
    if isinstance(profiles, list) and profiles:
        active = local.get("active")
        chosen = None
        if isinstance(active, str) and active.strip():
            chosen = next(
                (p for p in profiles if isinstance(p, dict) and str(p.get("id") or "") == active),
                None,
            )
        if not isinstance(chosen, dict):
            chosen = next((p for p in profiles if isinstance(p, dict)), None)
        if isinstance(chosen, dict):
            return {
                "id": str(chosen.get("id") or ""),
                "name": str(chosen.get("name") or chosen.get("id") or ""),
                "base_url": str(chosen.get("base_url") or ""),
                "api_key": str(chosen.get("api_key") or ""),
                "model": str(chosen.get("model") or ""),
            }
    return {
        "id": "",
        "name": "",
        "base_url": str(local.get("base_url") or ""),
        "api_key": str(local.get("api_key") or ""),
        "model": str(local.get("model") or ""),
    }


def load_settings() -> dict:
    cfg = load_json(CONFIG_PATH)
    env_key = os.environ.get("IMAGE_API_KEY", "").strip()
    env_base = os.environ.get("IMAGE_BASE_URL", "").strip()
    env_model = os.environ.get("IMAGE_MODEL", "").strip()
    env_size = os.environ.get("IMAGE_SIZE", "").strip()

    local: dict = {}
    if LOCAL_KEY_PATH.exists():
        try:
            local = load_json(LOCAL_KEY_PATH)
        except json.JSONDecodeError:
            local = {}
    if not isinstance(local, dict):
        local = {}

    prof = _active_profile(local)
    if env_key:
        # Env key is its own source: only pair with IMAGE_BASE_URL, never a leftover profile key.
        key = env_key
        base = env_base or ""
        model = env_model or prof.get("model") or ""
        source = "env"
        profile_id = ""
    else:
        key = str(prof.get("api_key") or "")
        prof_base = str(prof.get("base_url") or "")
        if env_base and prof_base and normalize_base(env_base) != normalize_base(prof_base):
            raise SystemExit(
                "IMAGE_BASE_URL 和本机 active 配置不是同一条线路。禁止拼套："
                "要么同时设 IMAGE_API_KEY + IMAGE_BASE_URL，要么两边都不设。"
            )
        base = env_base or prof_base
        model = env_model or str(prof.get("model") or "")
        source = "profile" if prof.get("id") else "local"
        profile_id = str(prof.get("id") or "")

    if not key:
        raise SystemExit(
            "没有 API Key。设 IMAGE_API_KEY，或确保存在 "
            f"{LOCAL_KEY_PATH} 且当前 active 配置有 key"
        )
    if env_key and not base:
        raise SystemExit("设了 IMAGE_API_KEY 时必须同时设 IMAGE_BASE_URL，禁止和环境/配置文件拼套。")

    base = normalize_base(base or cfg["base_url"])
    return {
        "base_url": base,
        "api_key": key,
        "model": model or cfg["model"],
        "size": env_size or cfg.get("size") or "1024x1024",
        "quality": cfg.get("quality") or "medium",
        "user_agent": cfg.get("user_agent")
        or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "min_bytes": int(cfg.get("min_bytes") or 20000),
        "timeout_sec": int(cfg.get("timeout_sec") or 180),
        "key_source": source,
        "profile_id": profile_id,
        "profile_name": str(prof.get("name") or ""),
        "has_key": True,
    }


def headers(settings: dict) -> dict:
    return {
        "Authorization": f"Bearer {settings['api_key']}",
        "User-Agent": settings["user_agent"],
    }


def character_dir(char_id: str) -> Path:
    return ROOT / "characters" / char_id


def load_bible(char_id: str) -> dict:
    path = character_dir(char_id) / "bible.json"
    if not path.exists():
        raise SystemExit(f"找不到角色圣经: {path}")
    return load_json(path)


def ref_images(char_id: str) -> list[Path]:
    refs = character_dir(char_id) / "refs"
    if not refs.exists():
        return []
    out = []
    for p in sorted(refs.iterdir()):
        if p.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}:
            out.append(p)
    return out


def pose_path(name: str | None) -> Path | None:
    if not name:
        return None
    p = Path(name)
    if not p.is_absolute():
        p = ROOT / name
    return p if p.exists() else None
