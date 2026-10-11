from __future__ import annotations

import base64
import json
import sys
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from studio_fixture import StudioFixture, app, generate, lib


class StudioTests(unittest.TestCase):
    def setUp(self):
        self.fixture = StudioFixture().__enter__()
        self.worker_patch = patch.object(app, "start_worker")
        self.worker = self.worker_patch.start()

    def tearDown(self):
        self.worker_patch.stop()
        self.fixture.__exit__()

    def enqueue(self, **kwargs):
        app.enqueue(self.fixture.request(**kwargs))
        return app.JOBS[-1]

    def test_prompt_is_verbatim_and_negative_can_be_removed(self):
        f = self.fixture
        job = self.enqueue()
        with patch.object(generate, "post_multipart", return_value={"data": [{"b64_json": base64.b64encode(b"image").decode()}]}) as post:
            self.assertEqual(generate.generate_one(f.settings, f.bible, job), b"image")
        _, fields, files, _ = post.call_args.args
        self.assertEqual(fields["prompt"], f.request()["final_prompt"])
        self.assertEqual((fields["model"], fields["size"], fields["quality"]), ("gpt-image-2.5-flare", "auto", "high"))
        self.assertEqual([p.name for _, p in files], ["01_identity.png", "02_inspiration.webp"])
        self.assertEqual(generate.resolve_prompt(f.bible, {"prompt_override": "only this", "negative_override": ""}), "only this")

    def test_multipart_keeps_webp_type_and_order(self):
        f = self.fixture
        response = unittest.mock.MagicMock()
        response.__enter__.return_value.read.return_value = b'{"data": []}'
        with patch.object(urllib.request, "urlopen", return_value=response) as send:
            generate.post_multipart("https://fixture.invalid/v1/images/edits", {"prompt": "exact"},
                                    [("image", f.identity), ("image", f.inspire)], f.settings)
        data = send.call_args.args[0].data
        self.assertIn(b"Content-Type: image/webp", data)
        self.assertLess(data.index(b'filename="01.png"'), data.index(b'filename="inspire.webp"'))

    def test_duplicate_submission_snapshots_and_version(self):
        f = self.fixture
        job = self.enqueue()
        duplicate = app.enqueue(f.request())
        self.assertEqual(duplicate["id"], job["id"])
        self.assertEqual(len(app.JOBS), 1)
        frozen = Path(job["reference_inputs"][1]["path"]).read_bytes()
        f.inspire.write_bytes(b"changed after enqueue")
        self.assertEqual(Path(job["reference_inputs"][1]["path"]).read_bytes(), frozen)
        with patch.object(app, "generate_one", f.mock_generate):
            app.run_job(job)
        self.assertEqual(job["status"], "ok")
        result = lib.find_result("测试作品|测试角色|studio|" + job["id"])
        self.assertEqual(result["final_prompt"], f.request()["final_prompt"])
        self.assertEqual(result["parent_result_id"], f.rid)
        self.assertEqual(result["actual"], {"width": 640, "height": 800, "format": "PNG"})
        self.assertTrue(f.parent.exists())
        self.assertEqual(len(f.calls), 1)

    def test_failure_calls_once_and_redacts_key(self):
        job = self.enqueue()
        with patch.object(app, "generate_one", side_effect=RuntimeError("fail " + self.fixture.settings["api_key"])) as call:
            app.run_job(job)
        self.assertEqual(call.call_count, 1)
        self.assertEqual(job["status"], "fail")
        self.assertNotIn(self.fixture.settings["api_key"], job["error"])
        self.assertNotIn("api_key", app.public_job(job))

    def test_restore_does_not_resubmit(self):
        self.enqueue()
        app.restore_jobs()
        self.assertEqual(app.JOBS[0]["status"], "interrupted")
        self.worker.assert_called_once()

    def test_refinement_order_and_templates(self):
        f = self.fixture
        body = f.request(mode="refine")
        plan = app.prepare(body)
        self.assertEqual([x["role"] for x in plan["references"]], ["base", "identity", "inspiration"])
        self.assertIn("FIRST image", plan["final_prompt"])
        self.assertEqual(plan["references"][0]["path"], str(f.parent.resolve()))
        self.assertNotIn("SECOND reference image is ONLY", app.prepare(f.request(send_inspire=False))["final_prompt"])

    def test_review_undo_drafts_and_presets(self):
        rid = self.fixture.rid
        lib.save_review(rid, "manual_review", "keep expression")
        lib.save_review(rid, "quarantine", "fix hand")
        restored = lib.undo_review(rid)
        self.assertEqual((restored["status"], restored["note"]), ("manual_review", "keep expression"))
        lib.save_draft(rid, {"prompt": "draft"})
        self.assertEqual(lib.local_store("drafts.json")[rid]["prompt"], "draft")
        lib.save_preset("portrait", {"prompt": "template"})
        self.assertEqual(lib.local_store("presets.json")["portrait"]["prompt"], "template")
        lib.save_draft(rid, None)
        self.assertNotIn(rid, lib.local_store("drafts.json"))

    def test_counts_history_and_refs(self):
        f = self.fixture
        lib.save_review(f.rid, "pass", "")
        all_results = lib.list_results()
        self.assertEqual(all_results["total"], 2)
        filtered = lib.list_results(status_filter="pass")
        self.assertEqual(filtered["total"], 1)
        self.assertEqual(filtered["counts"], all_results["counts"])
        old = lib.find_result(f.rid)
        self.assertIsNone(old["final_prompt"])
        self.assertIsNone(old["send_inspire"])
        self.assertIsNone(old["size"])
        self.assertEqual(old["children"], [f.child_rid])

    def test_unsent_inspiration_still_displayed(self):
        job = self.enqueue(send_inspire=False)
        self.assertEqual([r["role"] for r in job["reference_inputs"]], ["identity"])
        with patch.object(app, "generate_one", self.fixture.mock_generate):
            app.run_job(job)
        self.assertTrue(Path(job["left"]).is_file())
        self.assertFalse(lib.find_result("测试作品|测试角色|studio|" + job["id"])["send_inspire"])

    def test_known_split_wide_is_blocked(self):
        f = self.fixture
        vdir = f.inspire.parent / "_vision"
        vdir.mkdir()
        record = {"folder": str(f.inspire.parent), "images": [{"file": f.inspire.name}], "split_wide": {"is_split_wide": True}}
        (vdir / "split.json").write_text(json.dumps(record), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "半张"):
            app.enqueue(f.request())
        self.worker.assert_not_called()

    def test_missing_and_outside_images_rejected(self):
        f = self.fixture
        outside = f.root / "outside.png"
        f.image(outside, "#ffffff", "OUTSIDE")
        for path in [outside, f.library / "missing.png"]:
            with self.assertRaises(ValueError):
                app.enqueue(f.request(inspire=str(path)))
        self.worker.assert_not_called()

    def test_cannot_enqueue_when_persistence_fails(self):
        with patch.object(app, "persist_jobs", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(ValueError, "没有提交"):
                app.enqueue(self.fixture.request())
        self.assertEqual(app.JOBS, [])
        self.worker.assert_not_called()

    def test_workbench_requires_exact_prompt(self):
        for value in (None, "", 12):
            with self.assertRaisesRegex(ValueError, "完整发送提示词"):
                app.enqueue(self.fixture.request(mode="refine", final_prompt=value))
        self.worker.assert_not_called()

    def test_changed_intent_does_not_reuse_historical_prompt(self):
        f = self.fixture
        prepared = app.prepare(f.request(parent_result_id=f.child_rid, pose="NEW POSE"))
        self.assertIn("NEW POSE", prepared["final_prompt"])
        self.assertEqual(prepared["prompt_source"], "reconstructed")

    def test_interrupted_output_preserves_request_snapshot(self):
        f = self.fixture
        job = self.enqueue()
        f.image(f.out / (job["id"] + ".png"), "#666666", "SAVED BEFORE INTERRUPTION")
        app.restore_jobs()
        result = lib.find_result("测试作品|测试角色|studio|" + job["id"])
        self.assertEqual(result["final_prompt"], f.request()["final_prompt"])
        self.assertEqual(result["parent_result_id"], f.rid)
        self.assertEqual(result["task_status"], "interrupted")

    def test_source_survives_more_than_one_generation(self):
        f = self.fixture
        first = self.enqueue()
        with patch.object(app, "generate_one", f.mock_generate):
            app.run_job(first)
        first_result = lib.find_result("测试作品|测试角色|studio|" + first["id"])
        second = self.enqueue(parent_result_id=first_result["result_id"], inspire=first_result["inspire"], client_request_id="second")
        self.assertEqual(second["inspire"], str(f.inspire))
        vdir = f.inspire.parent / "_vision"
        vdir.mkdir()
        other = f.inspire.parent / "other.webp"
        f.image(other, "#999999", "HALF")
        (vdir / "new.json").write_text(json.dumps({"folder": str(other.parent), "images": [{"file": other.name}], "split_wide": {"is_split_wide": True}}), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "半张"):
            self.enqueue(parent_result_id=f.child_rid, inspire=str(other), client_request_id="third")

    def test_http_contract_and_origin_guard(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            for route in ["/", "/static/studio.js", "/static/studio.css", "/api/state", "/api/results", "/api/editor"]:
                with opener.open(base + route) as r:
                    self.assertEqual(r.status, 200)
            body = json.dumps(self.fixture.request()).encode()
            req = urllib.request.Request(base + "/api/prepare", data=body, headers={"Content-Type": "application/json"})
            with opener.open(req) as r:
                self.assertTrue(json.load(r)["references"])
            req = urllib.request.Request(base + "/api/prepare", data=body, headers={"Origin": "https://outside.invalid"})
            with self.assertRaises(urllib.error.HTTPError) as caught:
                opener.open(req)
            self.assertEqual(caught.exception.code, 403)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == "__main__":
    unittest.main()
