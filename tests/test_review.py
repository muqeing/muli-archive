from copy import deepcopy
from datetime import datetime, timezone
import unittest
from muli_sorter.review import ReviewError, build_review_model, compile_plan, validate_decisions


def sample_report():
    units = []
    for n, day in enumerate(("2026-07-09", "2026-07-09", "2026-07-10")):
        units.append({"files": [{"source_path": f"BATCH_20260928_000001/SOURCE_DATA/A{n}.ARW", "name": f"A{n}.ARW", "blake3": str(n) * 64, "size_bytes": 100}],
                      "capture_time": day + f"T{10+n}:00:00+08:00", "capture_date": day, "timezone_trusted": True,
                      "device": "合成相机", "kind": "photo", "warnings": [], "candidate_project_ids": ["p1" if n < 2 else "p2"]})
    units[0]["files"].append({"source_path": "BATCH_20260928_000001/SOURCE_DATA/A0.JPG", "name": "A0.JPG", "blake3": "f" * 64, "size_bytes": 40})
    return {"mode": "read_only_preview", "generated_at": "2026-09-28T00:00:00+00:00", "example_data": True,
            "projects": [{"project_id": "p1", "name": "合成项目甲", "path": "2026/7月/20260709_00001_合成甲", "dates": ["2026-07-09"]}, {"project_id": "p2", "name": "合成项目乙", "path": "2026/7月/20260710_00002_合成乙", "dates": ["2026-07-10"]}],
            "batches": [{"batch_id": "BATCH_20260928_000001", "status": "verified", "manifest_id": "uid-r1", "groups": [
                {"group_id": "g1", "file_count": 3, "units": units[:2]}, {"group_id": "g2", "file_count": 1, "units": units[2:]}]}]}


def draft(model):
    return {"schema_version": "0.2", "mode": "classification_confirmation_only", "media_write_authorized": False,
            "report_id": model["report_id"], "created_at": datetime.now(timezone.utc).isoformat(), "segments": deepcopy(model["initial_segments"])}


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.report = sample_report()
        self.model = build_review_model(self.report)
        self.plan = draft(self.model)

    def test_pairs_are_indivisible_and_no_initial_confirmation(self):
        self.assertEqual(len(self.model["units"]), 3)
        self.assertEqual(self.model["units"][0]["file_count"], 2)
        self.assertTrue(all(s["decision"] == "pending" and s["project_id"] is None for s in self.plan["segments"]))
        validate_decisions(self.model, self.plan)

    def test_repeated_batch_and_overlapping_pair_deduplicated(self):
        duplicate = deepcopy(self.report["batches"][0])
        duplicate.update(batch_id="BATCH_20260928_000002", manifest_id="uid2-r1")
        duplicate["groups"][0]["units"][0]["files"] = duplicate["groups"][0]["units"][0]["files"][:1]
        self.report["batches"].append(duplicate)
        model = build_review_model(self.report)
        self.assertEqual(len(model["units"]), 3)
        self.assertEqual(sum(u["file_count"] for u in model["units"]), 4)
        self.assertEqual(len(model["units"][0]["provenance"]), 2)

    def test_incomplete_batch_excluded(self):
        self.report["batches"].append({"batch_id": "BATCH_20260928_000003", "status": "waiting", "reasons": ["尚未完成"], "groups": []})
        model = build_review_model(self.report)
        self.assertEqual(len(model["units"]), 3)
        self.assertEqual(len(model["excluded_batches"]), 1)

    def test_confirmed_plan_is_not_executable(self):
        s = self.plan["segments"][0]
        s.update(decision="confirmed", project_id="p1")
        result = compile_plan(self.model, self.plan)
        self.assertFalse(result["executable"])
        self.assertFalse(result["media_write_authorized"])
        self.assertEqual(result["summary"]["confirmed_units"], 2)
        self.assertEqual(len(result["assignments"][0]["files"]), 3)

    def test_split_merge_cover_each_unit_exactly_once(self):
        first, second = self.plan["segments"]
        one, two = first["unit_ids"]
        self.plan["segments"] = [{**first, "segment_id": "split-1", "unit_ids": [one]}, {**first, "segment_id": "split-2", "unit_ids": [two]}, second]
        validate_decisions(self.model, self.plan)
        self.plan["segments"] = [{**first, "segment_id": "merged", "unit_ids": [one, two] + second["unit_ids"]}]
        validate_decisions(self.model, self.plan)

    def test_omissions_duplicates_and_unknown_units_rejected(self):
        for mutation in (lambda p: p["segments"].pop(), lambda p: p["segments"][1]["unit_ids"].append(p["segments"][0]["unit_ids"][0]), lambda p: p["segments"][0]["unit_ids"].append("invented")):
            p = deepcopy(self.plan)
            mutation(p)
            with self.assertRaises(ReviewError):
                validate_decisions(self.model, p)

    def test_cross_snapshot_or_privilege_escalation_rejected(self):
        for field, value in (("report_id", "wrong"), ("media_write_authorized", True), ("mode", "execute")):
            p = deepcopy(self.plan)
            p[field] = value
            with self.assertRaises(ReviewError):
                validate_decisions(self.model, p)

    def test_paths_cannot_be_injected_through_decisions(self):
        self.plan["segments"][0]["target_path"] = "../../outside"
        with self.assertRaises(ReviewError):
            validate_decisions(self.model, self.plan)

    def test_date_mismatch_requires_explicit_acknowledgement(self):
        self.plan["segments"][0].update(project_id="p2", decision="confirmed")
        with self.assertRaises(ReviewError):
            validate_decisions(self.model, self.plan)
        self.plan["segments"][0]["acknowledge_date_mismatch"] = True
        validate_decisions(self.model, self.plan)

    def test_changed_model_rejected(self):
        self.model["units"][0]["files"][0]["source_path"] = "other/file"
        with self.assertRaises(ReviewError):
            validate_decisions(self.model, self.plan)


if __name__ == "__main__":
    unittest.main()
