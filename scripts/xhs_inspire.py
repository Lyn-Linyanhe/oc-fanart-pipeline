#!/usr/bin/env python3
"""Xiaohongshu keyword → composition job cards.

Does not log in. Expects xiaohongshu-mcp already running (other session).
Downloads covers to inbox/xhs/ for human review. Never uses those images as
character refs; pose_ref stays empty unless you later drop a de-faced pose.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import ROOT  # noqa: E402

DEFAULT_MCP = "http://127.0.0.1:18060"
MCP_BASE = DEFAULT_MCP
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
VIDEO_HINT = re.compile(r"(视频|live|v\.xiaohongshu|sns-video)", re.I)

POSE_RULES = [
    (re.compile(r"回眸|回头|over.?shoulder", re.I), "looking back over shoulder"),
    (re.compile(r"坐姿|坐在|椅|窗边坐"), "sitting, relaxed, one knee angled"),
    (re.compile(r"走|步行|撑伞"), "walking forward"),
    (re.compile(r"蹲|跪"), "crouching"),
    (re.compile(r"躺|卧"), "lying down"),
    (re.compile(r"奔跑|跑"), "running"),
    (re.compile(r"侧颜|侧脸"), "profile view"),
    (re.compile(r"背影"), "from behind, back view"),
    (re.compile(r"俯拍|俯视"), "high angle looking down"),
    (re.compile(r"仰拍|仰视"), "low angle looking up"),
]
SHOT_RULES = [
    (re.compile(r"全身|full.?body", re.I), "full body"),
    (re.compile(r"特写|面部特写"), "close-up face"),
    (re.compile(r"半身|cowboy"), "cowboy shot"),
    (re.compile(r"中景"), "medium shot"),
]
MOOD_RULES = [
    (re.compile(r"雨|雨夜|wet"), "rainy night, wet ground reflections"),
    (re.compile(r"校园|教室|窗边"), "classroom, window light"),
    (re.compile(r"夜|霓虹"), "night, neon lights"),
    (re.compile(r"黄昏|夕阳|晚霞"), "golden hour, warm rim light"),
    (re.compile(r"雪"), "snow, cold atmosphere"),
    (re.compile(r"海边|沙滩"), "seaside, wind"),
    (re.compile(r"森林|林间"), "forest, dappled light"),
]


def http_json(method: str, url: str, payload: dict | None = None, timeout: int = 30) -> dict:
    data = None
    headers = {"User-Agent": UA, "Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:400]
        raise SystemExit(f"MCP HTTP {e.code} {url}: {body}") from e
    except urllib.error.URLError as e:
        raise SystemExit(
            f"连不上小红书 MCP（{url}）：{e}\n"
            "请在「空会话」里启动 xiaohongshu-mcp 并扫码登录后再跑。"
        ) from e
    if not raw.strip():
        return {}
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        raise SystemExit(f"MCP 返回不是 JSON: {raw[:200]}")
    return obj if isinstance(obj, dict) else {"data": obj}


def unwrap(obj: dict) -> object:
    if not isinstance(obj, dict):
        return obj
    for k in ("data", "result", "feeds", "items", "notes"):
        if k in obj and obj[k] is not None:
            return obj[k]
    return obj


def mcp_health() -> None:
    for path in ("/health", "/api/v1/health"):
        try:
            http_json("GET", MCP_BASE + path, timeout=5)
            return
        except SystemExit:
            continue
    # last try: login status is enough proof the process is up
    http_json("GET", MCP_BASE + "/api/v1/login/status", timeout=5)


def mcp_logged_in() -> bool:
    for path in ("/api/v1/login/status", "/login/status"):
        try:
            j = http_json("GET", MCP_BASE + path, timeout=8)
        except SystemExit:
            continue
        blob = json.dumps(j, ensure_ascii=False).lower()
        if any(s in blob for s in ("\"logged_in\": true", "\"is_logged_in\": true", "已登录", "\"login\": true")):
            return True
        if j.get("logged_in") or j.get("is_logged_in") or j.get("success") and "未登录" not in blob:
            data = unwrap(j)
            if data is True:
                return True
            if isinstance(data, dict) and (data.get("logged_in") or data.get("is_logged_in")):
                return True
    return False


def search_feeds(keyword: str, n: int) -> list[dict]:
    last_err = None
    attempts = [
        ("POST", MCP_BASE + "/api/v1/feeds/search", {"keyword": keyword, "filters": {"note_type": "图文"}}),
        ("GET", MCP_BASE + "/api/v1/feeds/search?" + urllib.parse.urlencode({"keyword": keyword}), None),
        ("POST", MCP_BASE + "/feeds/search", {"keyword": keyword}),
        ("GET", MCP_BASE + "/feeds/search?" + urllib.parse.urlencode({"keyword": keyword}), None),
    ]
    body = None
    for method, url, payload in attempts:
        try:
            body = http_json(method, url, payload, timeout=45)
            break
        except SystemExit as e:
            last_err = e
            continue
    if body is None:
        raise SystemExit(str(last_err) if last_err else "search 失败")
    items = unwrap(body)
    if isinstance(items, dict):
        for k in ("feeds", "items", "notes", "list", "data"):
            if isinstance(items.get(k), list):
                items = items[k]
                break
    if not isinstance(items, list):
        raise SystemExit(f"搜索结果格式不认识: {json.dumps(body, ensure_ascii=False)[:400]}")
    return items[: max(1, n)]


def walk_strings(obj, pred) -> list[str]:
    found: list[str] = []

    def rec(x):
        if isinstance(x, dict):
            for v in x.values():
                rec(v)
        elif isinstance(x, list):
            for v in x:
                rec(v)
        elif isinstance(x, str) and pred(x):
            found.append(x)

    rec(obj)
    return found


def feed_id_token(item: dict) -> tuple[str, str]:
    fid = str(
        item.get("id")
        or item.get("feed_id")
        or item.get("note_id")
        or item.get("noteId")
        or (item.get("noteCard") or {}).get("noteId")
        or ""
    )
    token = str(
        item.get("xsec_token")
        or item.get("xsecToken")
        or item.get("xsecToken")
        or ""
    )
    if not token:
        urls = walk_strings(item, lambda s: "xsec_token=" in s)
        if urls:
            q = urllib.parse.parse_qs(urllib.parse.urlparse(urls[0]).query)
            token = (q.get("xsec_token") or [""])[0]
    return fid, token


def title_of(item: dict) -> str:
    card = item.get("noteCard") if isinstance(item.get("noteCard"), dict) else {}
    for k in ("displayTitle", "title", "desc", "description", "name"):
        v = item.get(k) or card.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    texts = walk_strings(item, lambda s: 4 <= len(s) <= 80 and not s.startswith("http"))
    return texts[0] if texts else ""


def desc_of(item: dict) -> str:
    card = item.get("noteCard") if isinstance(item.get("noteCard"), dict) else {}
    for k in ("desc", "description", "content"):
        v = item.get(k) or card.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def cover_urls(item: dict) -> list[str]:
    urls = walk_strings(
        item,
        lambda s: s.startswith("http")
        and any(x in s.lower() for x in (".jpg", ".jpeg", ".png", ".webp", "sns-webpic", "xhscdn", "ci.xiaohongshu")),
    )
    out = []
    seen = set()
    for u in urls:
        if VIDEO_HINT.search(u):
            continue
        if u in seen:
            continue
        seen.add(u)
        out.append(u)
    return out


def pick_rule(text: str, rules, default: str) -> str:
    for rx, val in rules:
        if rx.search(text):
            return val
    return default


def composition_ok(item: dict, title: str, desc: str) -> tuple[bool, str]:
    blob = f"{title}\n{desc}"
    if VIDEO_HINT.search(json.dumps(item, ensure_ascii=False)):
        # still allow if it has a still cover
        if not cover_urls(item):
            return False, "video_no_cover"
    if not title and not desc:
        return False, "empty_text"
    # skip ads / product dumps
    if re.search(r"(店铺|购买|链接|优惠券|爆款课)", blob):
        return False, "ad"
    return True, "ok"


def download(url: str, dest: Path) -> bool:
    dest.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(
        url,
        headers={"User-Agent": UA, "Referer": "https://www.xiaohongshu.com/"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = resp.read()
    except (urllib.error.URLError, urllib.error.HTTPError):
        return False
    if len(data) < 4000:
        return False
    dest.write_bytes(data)
    return True


def to_job(i: int, item: dict, dest: Path | None, character: str) -> dict:
    title = title_of(item)
    desc = desc_of(item)
    blob = f"{title} {desc}"
    fid, token = feed_id_token(item)
    return {
        "id": f"xhs{i:03d}",
        "character": character,
        "shot": pick_rule(blob, SHOT_RULES, "cowboy shot"),
        "pose": pick_rule(blob, POSE_RULES, "standing, natural pose"),
        "mood": pick_rule(blob, MOOD_RULES, "cinematic lighting"),
        "pose_ref": None,
        "inspiration_only": True,
        "source": {
            "site": "xiaohongshu",
            "keyword_note": title[:120],
            "feed_id": fid,
            "cover": str(dest.relative_to(ROOT)).replace("\\", "/") if dest else None,
            "use": "composition inspiration; do not copy face/outfit",
        },
    }


def write_jobs(path: Path, jobs: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"jobs": jobs}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser(description="小红书关键词 → 构图任务卡（不登录，MCP 需已就绪）")
    ap.add_argument("--keyword", required=True, help="例如：动漫 构图 回眸 雨夜")
    ap.add_argument("--n", type=int, default=3, help="最多采用条数")
    ap.add_argument("--jobs", default=str(ROOT / "jobs" / "xhs.json"))
    ap.add_argument("--character", default="demo")
    ap.add_argument("--mcp", default=DEFAULT_MCP)
    args = ap.parse_args()
    global MCP_BASE
    MCP_BASE = args.mcp.rstrip("/")

    print(f"mcp={MCP_BASE} keyword={args.keyword!r} n={args.n}")
    mcp_health()
    if not mcp_logged_in():
        raise SystemExit(
            "小红书 MCP 在线但未登录。请到空会话扫码登录后重跑，本脚本不会弹出二维码。"
        )

    inbox = ROOT / "inbox" / "xhs"
    inbox.mkdir(parents=True, exist_ok=True)
    feeds = search_feeds(args.keyword, max(args.n * 3, args.n))
    jobs = []
    kept = 0
    for item in feeds:
        if not isinstance(item, dict):
            continue
        title = title_of(item)
        desc = desc_of(item)
        ok, reason = composition_ok(item, title, desc)
        if not ok:
            print(f"  skip {reason}: {title[:40]}")
            continue
        fid, _token = feed_id_token(item)
        cover = None
        urls = cover_urls(item)
        if urls:
            dest = inbox / f"{fid or f'n{kept+1:03d}'}.jpg"
            if download(urls[0], dest):
                cover = dest
            else:
                print(f"  cover download failed: {title[:40]}")
        kept += 1
        jobs.append(to_job(kept, item, cover, args.character))
        print(f"  keep {jobs[-1]['id']} shot={jobs[-1]['shot']} pose={jobs[-1]['pose']}")
        if len(jobs) >= args.n:
            break
        time.sleep(0.4)

    if not jobs:
        raise SystemExit("没有可用构图。换关键词，或确认 MCP 已登录且能搜到图文笔记。")

    out = Path(args.jobs)
    write_jobs(out, jobs)
    print(f"wrote {len(jobs)} jobs → {out}")
    print("身份仍来自 characters/<id>/bible.json + refs/；这些封面只是构图灵感，不会当脸。")
    print(f"下一步: python scripts/generate.py --jobs {out} --limit 1")


if __name__ == "__main__":
    main()
