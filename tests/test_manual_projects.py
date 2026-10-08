from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from muli_sorter.manual_folders import FolderError, apply_folders, preview_folders
from muli_sorter.manual_projects import project_path
from muli_sorter.matching import project_index
from muli_sorter.review import ReviewError, build_review_model, compile_plan, validate_decisions
from muli_sorter.archive_preflight import inspect_plan
from test_review import draft, sample_report


def manual_plan(model):
    plan = draft(model)
    segment = plan["segments"][0]
    pid = "manual-" + "a" * 32
    plan.update(schema_version="0.3", manual_projects=[{
        "project_id": pid, "name": "合成无订单活动", "shoot_date": "2026-07-09", "unit_ids": segment["unit_ids"][:]}])
    segment.update(project_id=pid, decision="confirmed")
    return plan


class ManualProjectTests(unittest.TestCase):
    def setUp(self):
        self.model = build_review_model(sample_report())
        self.plan = manual_plan(self.model)

    def test_manual_assignment_preserves_model_and_no_order(self):
        original = deepcopy(self.model)
        compiled = compile_plan(self.model, self.plan)
        project = compiled["assignments"][0]["project"]
        self.assertEqual(project["path"], "2026/7月/20260709_自建_合成无订单活动")
        self.assertIsNone(project["order_id"])
        self.assertFalse(project["exists"])
        self.assertFalse(compiled["media_write_authorized"])
        self.assertFalse(compiled["executable"])
        self.assertEqual(self.model, original)

    def test_no_candidates_can_create_but_unrelated_segment_cannot_select(self):
        report = sample_report()
        report["projects"] = []
        model = build_review_model(report)
        plan = manual_plan(model)
        validate_decisions(model, plan)
        plan["segments"][1]["project_id"] = plan["manual_projects"][0]["project_id"]
        with self.assertRaisesRegex(ReviewError, "素材范围"):
            validate_decisions(model, plan)

    def test_cross_date_ack_remains_required(self):
        self.plan["manual_projects"][0]["shoot_date"] = "2026-07-10"
        with self.assertRaisesRegex(ReviewError, "显式确认"):
            validate_decisions(self.model, self.plan)
        self.plan["segments"][0]["acknowledge_date_mismatch"] = True
        validate_decisions(self.model, self.plan)

    def test_same_date_split_segment_can_share_manual_project(self):
        first = self.plan["segments"][0]
        one, two = first["unit_ids"]
        self.plan["manual_projects"][0]["unit_ids"] = [one]
        first["unit_ids"] = [one]
        self.plan["segments"].append({**first, "segment_id": "split-same-date", "unit_ids": [two]})
        validate_decisions(self.model, self.plan)

    def test_injection_invalid_date_and_scope_rejected(self):
        bads = [("path", "../../outside"), ("name", "../outside"), ("name", "bad\\name"),
                ("name", "bad\u202ename"), ("name", "test."), ("name", " 空白 "), ("name", "字" * 80),
                ("shoot_date", "2026-02-30"), ("shoot_date", "26-7-9"), ("shoot_date", "2100-01-01"),
                ("unit_ids", ["unknown"]), ("project_id", "p1")]
        for field, value in bads:
            with self.subTest(field=field, value=value):
                plan = deepcopy(self.plan)
                plan["manual_projects"][0][field] = value
                with self.assertRaises(ReviewError):
                    validate_decisions(self.model, plan)

    def test_case_unicode_duplicate_paths_rejected(self):
        self.plan["manual_projects"][0]["name"] = "Café"
        second = {**self.plan["manual_projects"][0], "project_id": "manual-" + "b" * 32, "name": "CAFÉ"}
        self.plan["manual_projects"].append(second)
        with self.assertRaisesRegex(ReviewError, "同名"):
            validate_decisions(self.model, self.plan)

    def test_matching_manual_title_digits_are_not_order_id(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            target = root / project_path("活动_12345", "2026-07-09")
            target.mkdir(parents=True)
            indexed = project_index(root)
            self.assertEqual(len(indexed), 1)
            self.assertIsNone(indexed[0]["order_id"])

    def test_preflight_reports_pending_creation_instead_of_crashing(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            staging, projects = root / "staging", root / "projects"
            staging.mkdir()
            projects.mkdir()
            with patch("muli_sorter.archive_preflight.verify_sources", return_value={}):
                report = inspect_plan(self.model, self.plan, staging, projects, lambda: {})
            self.assertEqual(report["status"], "blocked")
            self.assertIn("manual_project_creation_pending", json.dumps(report))
            self.assertEqual(list(projects.iterdir()), [])


class ManualFolderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name).resolve()
        self.root, self.receipts = base / "projects", base / "receipts"
        self.root.mkdir()
        self.receipts.mkdir()
        self.model = build_review_model(sample_report())
        self.plan = manual_plan(self.model)

    def tearDown(self):
        self.temp.cleanup()

    def preview(self):
        return preview_folders(self.model, self.plan, self.root, self.receipts)

    def test_readonly_preview_approved_create_and_idempotent_readback(self):
        preview = self.preview()
        self.assertEqual(list(self.root.iterdir()), [])
        self.assertEqual(list(self.receipts.iterdir()), [])
        result = apply_folders(self.model, self.plan, self.root, self.receipts, preview["request_id"])
        self.assertEqual(result["status"], "created_verified")
        target = self.root / preview["targets"][0]["path"]
        self.assertEqual(list(target.iterdir()), [])
        self.assertFalse(result["media_write_authorized"])
        before = target.stat().st_mtime_ns
        repeated = apply_folders(self.model, self.plan, self.root, self.receipts, preview["request_id"])
        self.assertEqual(result["created"], repeated["created"])
        self.assertEqual(before, target.stat().st_mtime_ns)
        self.assertEqual(self.preview()["targets"][0]["state"], "created_verified")

    def test_missing_or_stale_approval_does_not_write(self):
        request_id = self.preview()["request_id"]
        self.plan["manual_projects"][0]["name"] = "改名后需重新核对"
        for approval in (None, request_id):
            with self.assertRaises(FolderError):
                apply_folders(self.model, self.plan, self.root, self.receipts, approval)
        self.assertEqual(list(self.root.iterdir()), [])
        self.assertEqual(list(self.receipts.iterdir()), [])

    def test_existing_folder_without_receipt_and_symlink_blocked(self):
        preview = self.preview()
        target = self.root / preview["targets"][0]["path"]
        target.mkdir(parents=True)
        self.assertEqual(self.preview()["status"], "blocked")
        with self.assertRaises(FolderError):
            apply_folders(self.model, self.plan, self.root, self.receipts, preview["request_id"])
        target.rmdir()
        target.symlink_to(self.receipts, target_is_directory=True)
        self.assertEqual(self.preview()["status"], "blocked")

    def test_parent_case_conflict_and_symlink_blocked(self):
        (self.root / "2026").symlink_to(self.receipts, target_is_directory=True)
        self.assertEqual(self.preview()["status"], "blocked")
        (self.root / "2026").unlink()
        target = self.root / project_path("合成无订单活动", "2026-07-09")
        target.parent.mkdir(parents=True)
        self.plan["manual_projects"][0]["name"] = "Case"
        target.with_name("20260709_自建_case").mkdir()
        self.assertEqual(self.preview()["status"], "blocked")

    def test_partial_creation_receipt_and_resume(self):
        segment = self.plan["segments"][1]
        pid = "manual-" + "b" * 32
        self.plan["manual_projects"].append({"project_id": pid, "name": "第二组合成", "shoot_date": "2026-07-10", "unit_ids": segment["unit_ids"][:]})
        segment.update(project_id=pid, decision="confirmed")
        request_id = self.preview()["request_id"]
        def fail(_):
            raise RuntimeError("合成中断")
        interrupted = apply_folders(self.model, self.plan, self.root, self.receipts, request_id, checkpoint=fail)
        self.assertEqual(interrupted["status"], "partial_requires_review")
        self.assertEqual(len(interrupted["created"]), 1)
        result = apply_folders(self.model, self.plan, self.root, self.receipts, request_id)
        self.assertEqual(result["status"], "created_verified")
        self.assertEqual(len(result["created"]), 2)

    def test_all_targets_checked_before_first_mkdir(self):
        segment = self.plan["segments"][1]
        pid = "manual-" + "b" * 32
        self.plan["manual_projects"].append({"project_id": pid, "name": "第二组", "shoot_date": "2026-07-10", "unit_ids": segment["unit_ids"][:]})
        segment.update(project_id=pid, decision="confirmed")
        before = self.preview()
        (self.root / before["targets"][1]["path"]).mkdir(parents=True)
        with self.assertRaises(FolderError):
            apply_folders(self.model, self.plan, self.root, self.receipts, before["request_id"])
        self.assertFalse((self.root / before["targets"][0]["path"]).exists())


if __name__ == "__main__":
    unittest.main()
