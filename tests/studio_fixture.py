"""Isolated local fixtures. Never load credentials or call an image provider."""
from __future__ import annotations

import base64
import io
import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageDraw

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
import common
import generate
import studio_app as app
import studio_lib as lib


class StudioFixture:
    def __init__(self):
        self.temp = tempfile.TemporaryDirectory(prefix="fanart-test-")
        self.root = Path(self.temp.name)
        self.project = self.root / "project"
        self.library = self.root / "library"
        self.bible = {"name": "测试角色", "lock": "silver hair, green eyes", "style": "watercolor",
                      "forbid": "watermark", "desktop": {"series": "测试作品", "character": "测试角色"}}
        char = self.project / "characters" / "test"
        (char / "refs").mkdir(parents=True)
        (char / "bible.json").write_text(json.dumps(self.bible), encoding="utf-8")
        self.identity = char / "refs" / "01.png"
        self.image(self.identity, "#778b92", "IDENTITY")
        self.inspire = self.library / "灵感作品" / "他山之石" / "inspire.webp"
        self.image(self.inspire, "#ae9198", "INSPIRATION")
        self.out = self.library / "测试作品" / "角色" / "测试角色" / "我的原创" / "studio"
        self.parent = self.out / "v001.png"
        self.child = self.out / "v002.png"
        self.image(self.parent, "#657384", "VERSION ONE")
        self.image(self.child, "#789c91", "VERSION TWO")
        self.image(self.out / "left_v001.webp", "#ae9198", "INSPIRATION")
        self.image(self.out / "left_v002.webp", "#ae9198", "INSPIRATION")
        self.image(self.out.parent / "refs" / "not-output.png", "#778b92", "NOT OUTPUT")
        self.rid = "测试作品|测试角色|studio|v001"
        self.child_rid = "测试作品|测试角色|studio|v002"
        record = {"id": "v002", "ok": True, "inspire": str(self.inspire), "parent_result_id": self.rid,
                  "final_prompt": "Original prompt\nKeep the expression.", "mode": "redo", "send_inspire": True,
                  "size": "1024x1536", "quality": "high", "model": "gpt-image-2.5-sunburst"}
        (self.out / "run_log.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")
        self.config = json.loads((REPO / "config.json").read_text(encoding="utf-8"))
        self.config["library"] = str(self.library)
        self.settings = {**self.config, "api_key": "fixture-only-not-a-real-key", "base_url": "https://fixture.invalid/v1",
                         "profile_id": "offline-test", "profile_name": "离线测试 · 不调用模型", "key_source": "fixture"}
        self.patches = [patch.object(common, "ROOT", self.project), patch.object(lib, "ROOT", self.project),
                        patch.object(app, "ROOT", self.project), patch.object(lib, "cfg", lambda: self.config),
                        patch.object(app, "cfg", lambda: self.config), patch.object(app, "load_settings", lambda: self.settings)]
        self.calls = []

    @staticmethod
    def image(path, color, text):
        path.parent.mkdir(parents=True, exist_ok=True)
        im = Image.new("RGB", (640, 800), color)
        draw = ImageDraw.Draw(im)
        draw.ellipse((180, 130, 460, 430), fill="#dedad0")
        draw.polygon([(190, 390), (450, 390), (530, 720), (110, 720)], fill="#343f55")
        draw.text((35, 40), text, fill="white", font_size=28)
        im.save(path)

    def __enter__(self):
        for p in self.patches:
            p.start()
        app.JOBS.clear()
        app.WORKER_STARTED = False
        return self

    def __exit__(self, *exc):
        for p in reversed(self.patches):
            p.stop()
        app.JOBS.clear()
        self.temp.cleanup()

    def request(self, **overrides):
        return {"character": "test", "inspire": str(self.inspire), "send_inspire": True,
                "mode": "redo", "final_prompt": "  Exact prompt\n没有隐藏后缀。  ",
                "model": "gpt-image-2.5-flare", "size": "auto", "quality": "high",
                "parent_result_id": self.rid, "client_request_id": "test-submit", **overrides}

    def mock_generate(self, settings, bible, job):
        self.calls.append({k: job[k] for k in ("id", "final_prompt", "mode", "size", "quality", "reference_inputs")})
        (self.root / "captured-requests.json").write_text(json.dumps(self.calls, ensure_ascii=False, indent=2), encoding="utf-8")
        return self.child.read_bytes()


if __name__ == "__main__":
    import argparse
    from http.server import ThreadingHTTPServer
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8790)
    args = parser.parse_args()
    with StudioFixture() as fixture, patch.object(app, "generate_one", fixture.mock_generate):
        print(f"OFFLINE TEST http://127.0.0.1:{args.port}/  fixture={fixture.root}", flush=True)
        ThreadingHTTPServer(("127.0.0.1", args.port), app.Handler).serve_forever()
