from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest

from muli_sorter.archive import CATEGORIES, run_synthetic
from muli_sorter.archive_fixture import prepare, save_manifest
from muli_sorter.archive_source import verify_sources
from muli_sorter.intake import resolve_files, validate_record
from muli_sorter.matching import group_files, project_index
from muli_sorter.review import build_review_model, digest


def fixture():
    temp = tempfile.TemporaryDirectory()
    root = Path(temp.name).resolve() / "fixture"
    model, decisions = prepare(root)
    return temp, root, model, decisions


def confirmed_assignment(root, model, decisions, index=0):
    units = {unit["unit_id"]: unit for unit in model["units"]}
    segment = [segment for segment in decisions["segments"] if segment["decision"] == "confirmed"][index]
    unit = units[segment["unit_ids"][0]]
    project = next(project for project in model["projects"] if project["project_id"] == segment["project_id"])
    file = unit["files"][0]
    target = root / "projects" / project["path"] / CATEGORIES[unit["kind"]] / "独立归档" / unit["unit_id"] / file["name"]
    source = root / "staging" / file["source_path"]
    return unit, project, file, source, target


def outcome(report, unit_id):
    return next(row for row in report["outcomes"] if row["unit_id"] == unit_id)


def reset_model_report_id(model, decisions):
    model = deepcopy(model)
    model["report_id"] = "sha256:" + digest({key: value for key, value in model.items() if key != "report_id"})
    decisions = deepcopy(decisions)
    decisions["report_id"] = model["report_id"]
    return model, decisions


def make_skipped_batch(root, first):
    second = deepcopy(first)
    second["manifest_id"] = "synthetic-fixture-2-r1"
    second["batch"] = {**first["batch"], "batch_id": "BATCH_20260928_000002", "batch_uid": "synthetic-fixture-2"}
    for file in second["files"]:
        file.update(
            copy_status="skipped_existing",
            metadata=None,
            destination_relative_path=None,
            existing_copy={
                "batch_uid": first["batch"]["batch_uid"],
                "batch_id": first["batch"]["batch_id"],
                "manifest_id": first["manifest_id"],
                "staging_relative_path": first["batch"]["batch_id"] + "/" + file["destination_relative_path"],
            },
        )
        file["hash"] = {
            **file["hash"],
            "destination": None,
            "existing_destination": file["hash"]["source"],
        }
    second["summary"] = {
        **second["summary"],
        "verified_file_count": 0,
        "previously_ingested_count": len(second["files"]),
    }
    batch = root / "staging" / second["batch"]["batch_id"]
    (batch / "SOURCE_DATA").mkdir(parents=True)
    save_manifest(root, second)
    snapshot = json.loads((root / "runtime-state.json").read_text())
    snapshot["batches"].append({**second["batch"], "revision": second["revision"]})
    (root / "runtime-state.json").write_text(json.dumps(snapshot))
    return second


def model_for_batch(root, manifest):
    staging = root / "staging"
    validated = validate_record(staging, manifest["batch"]["batch_id"], allow_examples=True)
    completed = {
        manifest["batch"]["batch_id"]: validated,
        "BATCH_20260928_000001": validate_record(staging, "BATCH_20260928_000001", allow_examples=True),
    }
    files = resolve_files(staging, validated, completed)
    projects = project_index(root / "projects")
    groups = group_files(files, projects, [], validated["manifest_id"])
    report = {
        "mode": "read_only_preview",
        "example_data": True,
        "generated_at": validated["batch"]["completed_at"],
        "projects": projects,
        "batches": [{"batch_id": manifest["batch"]["batch_id"], "status": "verified", "manifest_id": validated["manifest_id"], "groups": groups}],
    }
    model = build_review_model(report)
    decisions = {
        "schema_version": "0.2",
        "mode": "classification_confirmation_only",
        "media_write_authorized": False,
        "report_id": model["report_id"],
        "created_at": datetime.now(timezone.utc).isoformat(),
        "segments": [],
    }
    units = {unit["unit_id"]: unit for unit in model["units"]}
    for segment in model["initial_segments"]:
        unit = units[segment["unit_ids"][0]]
        if unit["candidate_project_ids"]:
            decisions["segments"].append({**segment, "decision": "confirmed", "project_id": unit["candidate_project_ids"][0]})
        else:
            decisions["segments"].append({**segment, "decision": "pending", "project_id": None})
    return model, decisions


class ArchiveSafetyTests(unittest.TestCase):
    def test_unregistered_same_name_and_content_is_rejected(self):
        temp, root, model, decisions = fixture()
        self.addCleanup(temp.cleanup)
        unit, _, file, source, target = confirmed_assignment(root, model, decisions)
        target.parent.mkdir(parents=True)
        target.write_bytes(source.read_bytes())
        self.assertNotEqual(target.stat().st_ino, source.stat().st_ino)
        report = run_synthetic(root, model, decisions)
        self.assertEqual(outcome(report, unit["unit_id"])["status"], "incomplete")
        self.assertEqual(target.read_bytes(), source.read_bytes())

    def test_symlink_target_is_rejected_without_following(self):
        temp, root, model, decisions = fixture()
        self.addCleanup(temp.cleanup)
        unit, _, _, source, target = confirmed_assignment(root, model, decisions)
        outside = root.parent / "outside"
        outside.write_bytes(source.read_bytes())
        target.parent.mkdir(parents=True)
        target.symlink_to(outside)
        report = run_synthetic(root, model, decisions)
        self.assertEqual(outcome(report, unit["unit_id"])["status"], "incomplete")
        self.assertTrue(target.is_symlink())
        self.assertEqual(outside.read_bytes(), source.read_bytes())

    def test_same_size_source_tamper_is_blocked_before_publish(self):
        temp, root, model, decisions = fixture()
        self.addCleanup(temp.cleanup)
        unit, _, _, source, target = confirmed_assignment(root, model, decisions)
        original = source.read_bytes()
        source.write_bytes(bytes([original[0] ^ 0xFF]) + original[1:])
        self.assertEqual(len(source.read_bytes()), len(original))
        report = run_synthetic(root, model, decisions)
        self.assertEqual(outcome(report, unit["unit_id"])["status"], "incomplete")
        self.assertFalse(target.exists())

    def test_repeated_archive_does_not_overwrite_tampered_target(self):
        temp, root, model, decisions = fixture()
        self.addCleanup(temp.cleanup)
        unit, _, _, source, target = confirmed_assignment(root, model, decisions)
        first = run_synthetic(root, model, decisions)
        self.assertEqual(outcome(first, unit["unit_id"])["status"], "completed")
        original_source = source.read_bytes()
        tampered = b"Z" * target.stat().st_size
        target.write_bytes(tampered)
        second = run_synthetic(root, model, decisions)
        self.assertEqual(outcome(second, unit["unit_id"])["status"], "incomplete")
        self.assertEqual(target.read_bytes(), tampered)
        self.assertEqual(source.read_bytes(), original_source)

    def test_cross_batch_reference_rechecks_old_owner_bytes(self):
        temp, root, model, _ = fixture()
        self.addCleanup(temp.cleanup)
        first = validate_record(root / "staging", "BATCH_20260928_000001", allow_examples=True)
        second = make_skipped_batch(root, first)
        model2, decisions2 = model_for_batch(root, second)
        owner = root / "staging" / "BATCH_20260928_000001" / "SOURCE_DATA/DCIM/A.JPG"
        original = owner.read_bytes()
        owner.write_bytes(bytes([original[0] ^ 0xFF]) + original[1:])
        self.assertEqual(len(owner.read_bytes()), len(original))
        report = run_synthetic(root, model2, decisions2)
        self.assertTrue(any(row["status"] == "incomplete" for row in report["outcomes"]))
        self.assertEqual(owner.read_bytes(), bytes([original[0] ^ 0xFF]) + original[1:])

    def test_proxy_only_missing_timezone_and_stale_runtime_are_blocked(self):
        temp, root, model, decisions = fixture()
        self.addCleanup(temp.cleanup)
        unit, _, _, _, target = confirmed_assignment(root, model, decisions)

        proxy_model = deepcopy(model)
        next(item for item in proxy_model["units"] if item["unit_id"] == unit["unit_id"])["kind"] = "proxy_only"
        proxy_model, proxy_decisions = reset_model_report_id(proxy_model, decisions)
        proxy_report = run_synthetic(root, proxy_model, proxy_decisions)
        self.assertEqual(outcome(proxy_report, unit["unit_id"])["status"], "incomplete")
        self.assertFalse(target.exists())

        temp2, root2, model2, decisions2 = fixture()
        self.addCleanup(temp2.cleanup)
        manifest = validate_record(root2 / "staging", "BATCH_20260928_000001", allow_examples=True)
        for file in manifest["files"]:
            if file["relative_path"] == "DCIM/A.JPG":
                file["metadata"]["capture_time"] = {"wall_time": "2026-07-09T10:00:00", "confidence": "wall_time"}
        save_manifest(root2, manifest)
        missing_timezone = run_synthetic(root2, model2, decisions2)
        self.assertEqual(outcome(missing_timezone, model2["units"][0]["unit_id"])["status"], "incomplete")

        stale = json.loads((root2 / "runtime-state.json").read_text())
        stale["generated_at"] = (datetime.now(timezone.utc) - timedelta(minutes=3)).isoformat()
        stale_run = run_synthetic(root2, model2, decisions2, runtime_provider=lambda: stale)
        confirmed_ids = {
            unit_id
            for segment in decisions2["segments"]
            if segment["decision"] == "confirmed"
            for unit_id in segment["unit_ids"]
        }
        self.assertTrue(confirmed_ids)
        self.assertTrue(all(outcome(stale_run, unit_id)["status"] == "incomplete" for unit_id in confirmed_ids))
        self.assertEqual(len(confirmed_ids), 2)
        self.assertEqual(stale_run["summary"]["completed_files"], 0)
        self.assertEqual(stale_run["summary"]["pending_units"], 1)


if __name__ == "__main__":
    unittest.main()
