#!/usr/bin/env python3
"""One command after Xiaohongshu is already logged in (other session).

  python scripts/run.py --keyword "动漫 构图 回眸 雨夜" --n 3 --limit 1
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable


def run(cmd: list[str]) -> None:
    print("+", " ".join(cmd), flush=True)
    subprocess.check_call(cmd, cwd=str(ROOT))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keyword", required=True)
    ap.add_argument("--n", type=int, default=3)
    ap.add_argument("--limit", type=int, default=1, help="生图张数，默认 1 避免烧钱")
    ap.add_argument("--character", default="demo")
    ap.add_argument("--jobs", default=str(ROOT / "jobs" / "xhs.json"))
    ap.add_argument("--skip-xhs", action="store_true", help="MCP 未就绪时只生已有 jobs")
    args = ap.parse_args()

    jobs = args.jobs
    if not args.skip_xhs:
        try:
            run([
                PY, "scripts/xhs_inspire.py",
                "--keyword", args.keyword,
                "--n", str(args.n),
                "--character", args.character,
                "--jobs", jobs,
            ])
        except subprocess.CalledProcessError:
            print("小红书未就绪。空会话登录后再跑；或加 --skip-xhs 用现有任务卡。", file=sys.stderr)
            raise SystemExit(2)
    else:
        jobs = str(ROOT / "jobs" / "batch01.json")
        print(f"--skip-xhs，改用 {jobs}")

    run([PY, "scripts/generate.py", "--jobs", jobs, "--limit", str(args.limit)])
    run([PY, "scripts/sift.py"])


if __name__ == "__main__":
    main()
