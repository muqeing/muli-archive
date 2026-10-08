import unittest

from muli_sorter.render import render_report


class RenderReportTests(unittest.TestCase):
    def _report(self, **overrides):
        report = {
            "generated_at": "2026-09-28T10:00:00Z",
            "mode": "read_only_preview",
            "summary": {
                "batches": 1,
                "verified_batches": 1,
                "blocked_batches": 0,
                "groups": 1,
                "ready_groups": 0,
                "review_groups": 1,
            },
            "batches": [
                {
                    "batch_id": "batch-001",
                    "status": "verified",
                    "reasons": ["项目日期有两个候选"],
                    "file_count": 2,
                    "groups": [
                        {
                            "group_id": "group-001",
                            "capture_date": "2026-09-27",
                            "device": "相机 A",
                            "kind": "photo",
                            "file_count": 2,
                            "bytes": 1536,
                            "status": "review",
                            "reasons": ["缺少人工确认"],
                            "candidates": [
                                {
                                    "project_id": "P-001",
                                    "order_id": "SO-001",
                                    "name": "家庭肖像",
                                    "path": "/vol1/projects/P-001",
                                    "evidence": ["拍摄日期相符"],
                                }
                            ],
                            "file_names": ["IMG_0001.RAW", "IMG_0001.JPG"],
                        }
                    ],
                }
            ],
            "limitations": ["只读展示，不执行归档"],
        }
        report.update(overrides)
        return report

    def test_renders_read_only_status_and_report_fields(self):
        html = render_report(self._report())
        self.assertIn("只读预览 · 未归档", html)
        self.assertIn("batch-001", html)
        self.assertIn("group-001", html)
        self.assertIn("2026-09-28 18:00:00（北京时间）", html)
        self.assertIn("快照时间（非实时）", html)
        self.assertIn("2026-09-27 · 照片", html)
        self.assertIn("追踪 ID：", html)
        self.assertIn("销售编号：SO-001", html)
        self.assertNotIn("项目编号：P-001", html)
        self.assertIn("待确认理由", html)
        self.assertIn("家庭肖像", html)
        self.assertIn("文件数", html)
        self.assertIn("1.5 KB", html)
        self.assertIn('type="search"', html)
        self.assertIn("全部状态", html)
        self.assertIn("等待拷贝完成", html)
        self.assertIn("const filteringBatches", html)

    def test_kind_labels_and_internal_project_id_are_not_mislabelled(self):
        report = self._report(
            batches=[
                {
                    "batch_id": "batch-kinds",
                    "status": "waiting",
                    "groups": [
                        {"group_id": "g-photo", "capture_date": "2026-01-01", "kind": "photo"},
                        {"group_id": "g-video", "capture_date": "2026-01-01", "kind": "video"},
                        {"group_id": "g-audio", "capture_date": "2026-01-01", "kind": "audio"},
                        {"group_id": "g-proxy", "capture_date": "2026-01-01", "kind": "proxy_only"},
                        {"group_id": "g-aux", "capture_date": "2026-01-01", "kind": "auxiliary"},
                    ],
                }
            ]
        )
        html = render_report(report)
        for label in ("照片", "主视频", "录音", "代理视频", "辅助文件"):
            self.assertIn(label, html)
        self.assertIn("等待拷贝完成", html)
        self.assertNotIn("等待确认", html)
        self.assertNotIn("项目编号", html)

    def test_user_values_are_html_escaped(self):
        payload = '</script><script>alert("x")</script>&<img src=x onerror=alert(1)>'
        report = self._report(
            generated_at=payload,
            limitations=[payload],
            batches=[
                {
                    "batch_id": payload,
                    "status": "blocked",
                    "reasons": [payload],
                    "groups": [
                        {
                            "group_id": payload,
                            "device": payload,
                            "candidates": [{"name": payload, "path": payload}],
                            "file_names": [payload],
                        }
                    ],
                }
            ],
        )
        html = render_report(report)
        self.assertNotIn(payload, html)
        self.assertIn("&lt;/script&gt;", html)
        self.assertIn("&lt;img src=x onerror=alert(1)&gt;", html)
        self.assertNotIn('<script>alert("x")</script>', html)

    def test_no_external_resources_are_referenced(self):
        html = render_report(self._report())
        self.assertNotIn("<script src=", html.lower())
        self.assertNotIn("<link ", html.lower())
        self.assertNotIn("http://", html.lower())
        self.assertNotIn("https://", html.lower())

    def test_missing_fields_get_safe_defaults(self):
        html = render_report({})
        self.assertIn("只读预览 · 未归档", html)
        self.assertIn("当前没有可预览的批次", html)
        self.assertIn("暂无补充限制", html)
        self.assertIn("只读预览 · 未归档", html)


if __name__ == "__main__":
    unittest.main()
