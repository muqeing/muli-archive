import json
import re
import unittest

from muli_sorter.review_render import render_review


class ReviewRenderTests(unittest.TestCase):
    def model(self):
        return {
            "schema_version": "0.2",
            "report_id": "sha256:demo",
            "snapshot_at": "2026-09-28T02:00:00+08:00",
            "example_data": True,
            "projects": [
                {"project_id": "P-1", "name": "家庭肖像", "path": "/archive/P-1", "order_id": "SO-1", "dates": ["2026-09-27"]},
                {"project_id": "P-2", "name": "历史项目", "path": "/archive/P-2", "order_id": "SO-2", "dates": []},
            ],
            "units": [
                {"unit_id": "U-1", "capture_time": "2026-09-27T10:00:00+08:00", "capture_date": "2026-09-27", "device": "相机 A", "kind": "photo", "file_count": 2, "bytes": 123, "file_names": ["IMG_1.RAW", "IMG_1.JPG"], "warnings": [], "candidate_project_ids": ["P-1"], "provenance": []},
                {"unit_id": "U-2", "capture_time": None, "capture_date": None, "device": "相机 A", "kind": "video", "file_count": 1, "bytes": 456, "file_names": ["clip.mov"], "warnings": ["日期未知"], "candidate_project_ids": [], "provenance": []},
            ],
            "initial_segments": [
                {"segment_id": "S-1", "label": "上午拍摄", "unit_ids": ["U-1", "U-2"], "project_id": None, "decision": "pending", "acknowledge_date_mismatch": False}
            ],
            "excluded_batches": [{"batch_id": "B-0", "status": "blocked", "reasons": ["缺少回执"]}],
        }

    def test_renders_self_contained_confirmation_controls(self):
        output = render_review(self.model())
        for marker in ("id=\"export-plan\"", "id=\"save-draft\"", "id=\"merge-segments\"", "data-segment-id", "data-unit-id"):
            self.assertIn(marker, output)
        self.assertIn("按拍摄段确认项目", output)
        self.assertIn("example_data=true", output)
        self.assertIn("classification_confirmation_only", output)
        self.assertIn("media_write_authorized", output)
        self.assertNotIn("<script src=", output.lower())
        self.assertNotIn("http://", output.lower())
        self.assertNotIn("https://", output.lower())

    def test_model_is_json_and_script_termination_is_escaped(self):
        model = self.model()
        payload = "</script><script>alert(1)</script>"
        model["projects"][0]["name"] = payload
        output = render_review(model)
        self.assertNotIn(payload, output)
        self.assertNotIn("</script><script>", output)
        match = re.search(r'<script id="review-model" type="application/json">(.*?)</script>', output, re.S)
        self.assertIsNotNone(match)
        embedded = json.loads(match.group(1))
        self.assertEqual(embedded["report_id"], "sha256:demo")
        self.assertEqual(embedded["projects"][0]["name"], payload)

    def test_empty_model_still_has_safe_shell(self):
        output = render_review({})
        self.assertIn("当前状态", output)
        self.assertIn("id=\"timeline\"", output)
        self.assertIn('<script id="review-model" type="application/json">{}</script>', output)

    def test_video_previews_are_embedded_separately_and_paths_are_allowlisted(self):
        previews = {
            "U-2": {
                "state": "ready",
                "source_label": "主视频",
                "source_name": "clip.mp4",
                "message": "已生成",
                "frames": [
                    {"src": "../../video-previews/" + "a" * 64 + ".jpg", "time_seconds": 12.3},
                    {"src": "video-previews/" + "b" * 64 + ".jpg", "time_seconds": 45},
                    {"src": "https://example.invalid/frame.jpg", "time_seconds": 60},
                ],
            },
            "U-photo": {"state": ["invalid"]},
        }
        output = render_review(self.model(), previews=previews)
        match = re.search(r'<script id="review-previews" type="application/json">(.*?)</script>', output, re.S)
        self.assertIsNotNone(match)
        embedded = json.loads(match.group(1))
        self.assertEqual(embedded["U-2"]["source_name"], "clip.mp4")
        self.assertEqual(len(embedded["U-2"]["frames"]), 2)
        self.assertEqual(embedded["U-2"]["frames"][0]["time_seconds"], 12.3)
        self.assertEqual(embedded["U-photo"]["state"], "pending")
        model_match = re.search(r'<script id="review-model" type="application/json">(.*?)</script>', output, re.S)
        self.assertEqual(json.loads(model_match.group(1))["report_id"], "sha256:demo")

    def test_preview_script_is_present_without_preview_data(self):
        output = render_review(self.model())
        self.assertIn('<script id="review-previews" type="application/json">{}</script>', output)

    def test_material_state_is_embedded_without_changing_model_payload(self):
        model = self.model()
        material_state = {
            "version": "material-triage/1",
            "report_id": model["report_id"],
            "units": {
                "U-1": {"category": "shoot", "reason_code": "primary", "reason": ""},
                "U-2": {"category": "exception", "reason_code": "source_missing", "reason": "来源文件不在记录的位置"},
            },
        }
        output = render_review(model, material_state=material_state)
        settings = re.search(r'<script id="review-settings" type="application/json">(.*?)</script>', output, re.S)
        self.assertIsNotNone(settings)
        embedded = json.loads(settings.group(1))
        self.assertEqual(embedded["material_state"], material_state)
        self.assertIn('id="material-triage"', output)
        self.assertIn("重新检查来源", output)
        self.assertIn("/api/material-state?report_id=", output)
        self.assertEqual(model["initial_segments"][0]["unit_ids"], ["U-1", "U-2"])


if __name__ == "__main__":
    unittest.main()
