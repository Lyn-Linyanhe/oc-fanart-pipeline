#!/usr/bin/env python3
"""Cheap automatic reject: tiny files / decode fail / near-empty frames."""
from __future__ import annotations

import argparse
import json
import struct
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def png_size(data: bytes) -> tuple[int, int] | None:
    if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    w, h = struct.unpack(">II", data[16:24])
    return w, h


def jpeg_size(data: bytes) -> tuple[int, int] | None:
    if data[:2] != b"\xff\xd8":
        return None
    i = 2
    while i + 9 < len(data):
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in (0xC0, 0xC1, 0xC2):
            h, w = struct.unpack(">HH", data[i + 5 : i + 9])
            return w, h
        if marker == 0xD9:
            break
        ln = struct.unpack(">H", data[i + 2 : i + 4])[0]
        i += 2 + ln
    return None


def png_avg_luma_sample(data: bytes) -> float | None:
    """Very rough: inflate first IDAT and average a few bytes. Optional."""
    try:
        i = 8
        idats = []
        while i + 12 <= len(data):
            ln = struct.unpack(">I", data[i : i + 4])[0]
            typ = data[i + 4 : i + 8]
            chunk = data[i + 8 : i + 8 + ln]
            i += 12 + ln
            if typ == b"IDAT":
                idats.append(chunk)
            if typ == b"IEND":
                break
        raw = zlib.decompress(b"".join(idats))
        sample = raw[1:4000:17]
        if not sample:
            return None
        return sum(sample) / len(sample)
    except Exception:
        return None


def judge(path: Path, min_bytes: int) -> dict:
    rec = {"file": path.name, "keep": False}
    data = path.read_bytes()
    rec["bytes"] = len(data)
    if len(data) < min_bytes:
        rec["reason"] = "too_small"
        return rec
    size = png_size(data) or jpeg_size(data)
    if not size:
        rec["reason"] = "not_image"
        return rec
    rec["width"], rec["height"] = size
    if min(size) < 256:
        rec["reason"] = "tiny_canvas"
        return rec
    luma = png_avg_luma_sample(data) if path.suffix.lower() == ".png" else None
    if luma is not None and luma < 8:
        rec["reason"] = "almost_black"
        return rec
    rec["keep"] = True
    rec["reason"] = "ok"
    return rec


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=str(ROOT / "out"))
    ap.add_argument("--min-bytes", type=int, default=20000)
    args = ap.parse_args()
    folder = Path(args.dir)
    rows = []
    for p in sorted(folder.glob("*.png")) + sorted(folder.glob("*.jpg")):
        rows.append(judge(p, args.min_bytes))
    keep = [r for r in rows if r["keep"]]
    drop = [r for r in rows if not r["keep"]]
    report = folder / "sift_report.json"
    report.write_text(json.dumps({"keep": keep, "drop": drop}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"keep {len(keep)} / drop {len(drop)}  → {report}")
    for r in drop:
        print(f"  DROP {r['file']}: {r['reason']}")


if __name__ == "__main__":
    main()
