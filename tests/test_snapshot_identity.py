"""The snapshot id must name the material, not the moment it was published.

A republish that changed nothing used to hand every open confirmation page a new
id, so the page's saved plan was rejected as "计划来自其他快照" even though the
material was identical. These tests pin the property that made that happen.
"""
import unittest

from muli_sorter.review import (build_review_model, digest, identity_digest,
                                legacy_identity_digest, validate_decisions, validate_model)
from test_review import sample_report


def report_at(stamp):
    value = sample_report()
    value["generated_at"] = stamp
    return value


class SnapshotIdentityTests(unittest.TestCase):
    def test_republish_with_same_material_keeps_the_snapshot_id(self):
        first = build_review_model(report_at("2026-10-08T06:14:24.719788+00:00"))
        again = build_review_model(report_at("2026-10-08T06:21:45.923578+00:00"))
        self.assertNotEqual(first["snapshot_at"], again["snapshot_at"])
        self.assertEqual(first["report_id"], again["report_id"])

    def test_changed_material_gets_a_new_snapshot_id(self):
        base = build_review_model(report_at("2026-10-08T06:14:24+00:00"))
        changed = build_review_model(report_at("2026-10-08T06:14:24+00:00"))
        changed["units"][0]["candidate_project_ids"] = ["another-project"]
        changed["units"][0]["bytes"] = changed["units"][0]["bytes"] + 1
        changed["report_id"] = identity_digest(changed)
        self.assertNotEqual(base["report_id"], changed["report_id"])

    def test_plan_from_a_content_identical_republish_is_still_accepted(self):
        first = build_review_model(report_at("2026-10-08T06:14:24+00:00"))
        again = build_review_model(report_at("2026-10-08T06:21:45+00:00"))
        units = {u["unit_id"]: u for u in again["units"]}
        plan = {"schema_version": "0.2", "mode": "classification_confirmation_only",
                "media_write_authorized": False, "report_id": first["report_id"],
                "created_at": "2026-10-08T06:14:30+00:00",
                "segments": [{**s, "decision": "confirmed",
                              "project_id": units[s["unit_ids"][0]]["candidate_project_ids"][0]}
                             for s in again["initial_segments"]]}
        validate_decisions(again, plan)

    def test_older_pages_keep_working_during_a_rolling_update(self):
        model = build_review_model(report_at("2026-10-08T06:14:24+00:00"))
        model["report_id"] = legacy_identity_digest(model)
        validate_model(model)

    def test_a_plan_from_different_material_is_still_rejected(self):
        from muli_sorter.review import ReviewError
        first = build_review_model(report_at("2026-10-08T06:14:24+00:00"))
        again = build_review_model(report_at("2026-10-08T06:21:45+00:00"))
        again["units"][0]["bytes"] = again["units"][0]["bytes"] + 1
        again["report_id"] = identity_digest(again)
        plan = {"schema_version": "0.2", "mode": "classification_confirmation_only",
                "media_write_authorized": False, "report_id": first["report_id"],
                "created_at": "2026-10-08T06:14:30+00:00",
                "segments": [dict(s) for s in first["initial_segments"]]}
        with self.assertRaises(ReviewError):
            validate_decisions(again, plan)


if __name__ == "__main__":
    unittest.main()
