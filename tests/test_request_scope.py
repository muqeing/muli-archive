from copy import deepcopy
import json
import unittest

from muli_sorter.archive_request_scope import scoped_request_inputs
from muli_sorter.review import digest


def _file(path, name, marker, size=1):
    return {"source_path": path, "name": name, "blake3": marker * 64,
            "size_bytes": size}


def _unit(uid, kind="photo", files=None):
    return {"unit_id": uid, "capture_date": "2026-01-01", "kind": kind,
            "files": files or [_file(uid + ".jpg", uid + ".jpg", uid[0])],
            "provenance": [{"manifest_id": "manifest-1", "batch_id": "batch-1",
                            "group_id": "group-1"}]}


def _base_model(units, projects=None):
    model = {"schema_version": "0.2", "snapshot_at": "2026-01-01T00:00:00+00:00",
             "example_data": True, "projects": projects or [{
                 "project_id": "p1", "name": "P", "path": "2026/1月/P",
                 "dates": ["2026-01-01"]}], "units": units,
             "initial_segments": [], "excluded_batches": []}
    model["report_id"] = "sha256:" + digest(model)
    return model


def _decisions(model, segments, *, manual_projects=None):
    result = {"schema_version": "0.3" if manual_projects is not None else "0.2",
              "mode": "classification_confirmation_only",
              "media_write_authorized": False, "report_id": model["report_id"],
              "created_at": model["snapshot_at"], "segments": segments}
    if manual_projects is not None:
        result["manual_projects"] = manual_projects
    return result


class RequestScopeTests(unittest.TestCase):
    def _prepared(self, unit, project=None):
        return {"selected": [{"unit": deepcopy(unit), "project": deepcopy(project or {}),
                               "rows": []}],
                "compiled": {"assignments": [{"unit_ids": [unit["unit_id"]]}]},
                "summary": {"units": 1, "files": len(unit["files"])}}

    def test_unselected_large_model_fields_are_not_carried(self):
        selected, unused = _unit("u1"), _unit("u2")
        model = _base_model([selected, unused])
        model["unused_units"] = [{"payload": "x" * 200000}]
        model["batches"] = [{"payload": "y" * 200000}]
        model["report_id"] = "sha256:" + digest(
            {key: value for key, value in model.items() if key != "report_id"})
        decisions = _decisions(model, [
            {"segment_id": "selected", "label": "selected", "unit_ids": ["u1"],
             "project_id": "p1", "decision": "confirmed", "acknowledge_date_mismatch": False},
            {"segment_id": "unused", "label": "unused", "unit_ids": ["u2"],
             "project_id": None, "decision": "deferred", "acknowledge_date_mismatch": False},
        ])
        original_model, original_decisions = deepcopy(model), deepcopy(decisions)
        scoped_model, scoped_decisions, binding = scoped_request_inputs(
            model, decisions, self._prepared(selected, model["projects"][0]))

        self.assertEqual([u["unit_id"] for u in scoped_model["units"]], ["u1"])
        self.assertNotIn("unused_units", scoped_model)
        self.assertNotIn("batches", scoped_model)
        self.assertLess(len(json.dumps(scoped_model)), 10000)
        self.assertEqual(scoped_decisions["report_id"], scoped_model["report_id"])
        self.assertEqual(binding["report_id"], model["report_id"])
        self.assertEqual(model, original_model)
        self.assertEqual(decisions, original_decisions)

    def test_all_confirmed_units_and_file_identity_are_retained(self):
        first, second = _unit("u1"), _unit("u2")
        model = _base_model([first, second])
        decisions = _decisions(model, [{
            "segment_id": "selected", "label": "selected", "unit_ids": ["u1", "u2"],
            "project_id": "p1", "decision": "confirmed", "acknowledge_date_mismatch": False,
        }])
        project = model["projects"][0]
        prepared = {"selected": [{"unit": deepcopy(first), "project": deepcopy(project), "rows": []},
                                  {"unit": deepcopy(second), "project": deepcopy(project), "rows": []}],
                    "compiled": {"assignments": [{"unit_ids": ["u1", "u2"]}]},
                    "summary": {"units": 2}}

        scoped_model, scoped_decisions, binding = scoped_request_inputs(model, decisions, prepared)
        self.assertEqual([u["unit_id"] for u in scoped_model["units"]], ["u1", "u2"])
        self.assertEqual(scoped_decisions["segments"][0]["unit_ids"], ["u1", "u2"])
        self.assertEqual(binding["selected_unit_ids"], ["u1", "u2"])
        self.assertEqual(scoped_model["units"], [first, second])

    def test_companion_and_manual_anchor_dependencies_are_deferred(self):
        parent = _unit("video", "video", [_file(
            "DCIM/DJI_001/video.mp4", "DCIM/DJI_001/DJI_20260710100000_0001_D.MP4", "a")])
        companion = _unit("companion", "auxiliary", [_file(
            "MISC/THM/thumb.thm", "MISC/THM/DJI_001/DJI_20260710100000_0001_D.THM", "b")])
        anchor = _unit("anchor")
        model = _base_model([parent, companion, anchor])
        manual_id = "manual-" + "a" * 32
        manual = [{"project_id": manual_id, "name": "Manual", "shoot_date": "2026-01-01",
                   "unit_ids": ["companion", "anchor"]}]
        decisions = _decisions(model, [
            {"segment_id": "parent", "label": "parent", "unit_ids": ["video"],
             "project_id": None, "decision": "deferred", "acknowledge_date_mismatch": False},
            {"segment_id": "selected", "label": "selected", "unit_ids": ["companion"],
             "project_id": manual_id, "decision": "confirmed", "acknowledge_date_mismatch": False},
            {"segment_id": "anchor", "label": "anchor", "unit_ids": ["anchor"],
             "project_id": None, "decision": "deferred", "acknowledge_date_mismatch": False},
        ], manual_projects=manual)
        selected = deepcopy(companion)
        selected["_companion_parent"] = deepcopy(parent)
        prepared = self._prepared(selected, {"project_id": manual_id,
                                              "name": "Manual",
                                              "path": "2026/1月/20260101_自建_Manual"})

        scoped_model, scoped_decisions, _ = scoped_request_inputs(model, decisions, prepared)
        self.assertEqual({u["unit_id"] for u in scoped_model["units"]},
                         {"video", "companion", "anchor"})
        by_uid = {uid: segment for segment in scoped_decisions["segments"]
                  for uid in segment["unit_ids"]}
        self.assertEqual(by_uid["video"]["decision"], "deferred")
        self.assertEqual(by_uid["anchor"]["decision"], "deferred")
        self.assertEqual(scoped_decisions["manual_projects"][0]["unit_ids"],
                         ["companion", "anchor"])


if __name__ == "__main__":
    unittest.main()
