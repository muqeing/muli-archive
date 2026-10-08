from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from muli_sorter.archive_fixture import prepare
from muli_sorter.archive_preflight import inspect_plan


def _fixture():
    temp = tempfile.TemporaryDirectory()
    root = Path(temp.name).resolve() / "fixture"
    model, decisions = prepare(root)
    runtime = lambda: json.loads((root / "runtime-state.json").read_text())
    return temp, root, model, decisions, runtime


class ArchivePreflightTests(unittest.TestCase):
    def test_all_pending_is_blocked_and_keeps_counts(self):
        temp, root, model, decisions, runtime = _fixture()
        self.addCleanup(temp.cleanup)
        for segment in decisions["segments"]:
            segment.update(decision="pending", project_id=None)

        result = inspect_plan(model, decisions, root / "staging", root / "projects", runtime)

        self.assertEqual(result["status"], "blocked")
        self.assertFalse(result["executable"])
        self.assertFalse(result["media_write_authorized"])
        self.assertEqual(result["summary"]["pending_units"], 3)
        self.assertIn("needs_assignment_confirmation", " ".join(result["blocking_reasons"]))

    def test_confirmed_plan_has_one_target_per_model_file(self):
        temp, root, model, decisions, runtime = _fixture()
        self.addCleanup(temp.cleanup)
        result = inspect_plan(model, decisions, root / "staging", root / "projects", runtime)

        self.assertEqual(result["status"], "ready_for_write_review")
        self.assertEqual(result["summary"]["confirmed_files"], 4)
        self.assertEqual(result["summary"]["targets"], 4)
        self.assertEqual(sum(len(unit["targets"]) for unit in result["units"]), 4)
        self.assertTrue(all(not row["full_content_hash_verified"] for unit in result["units"] for row in unit["sources"]))
        self.assertTrue(result["required_before_execution"])

    def test_missing_project_requires_unique_matching_creation_intent(self):
        temp, root, model, decisions, runtime = _fixture()
        self.addCleanup(temp.cleanup)
        project = next(p for p in model["projects"] if p["project_id"] == decisions["segments"][0]["project_id"])
        (root / "projects" / project["path"]).rmdir()

        blocked = inspect_plan(model, decisions, root / "staging", root / "projects", runtime)
        self.assertEqual(blocked["status"], "blocked")
        self.assertTrue(any("missing_project_folder_intent" in reason for reason in blocked["blocking_reasons"]))

        shoot_date = project["dates"][0]
        intent = {"intentions": [{
            "action": "create_required", "path": project["path"], "order_id": project["order_id"],
            "shoot_date": shoot_date,
            "folder_request": {"service":"photo-project-folder-service", "payload": {
                "order_id": project["order_id"], "shoot_date": shoot_date,
                "customer_name":"合成客户", "package_code":"TEST-01"}},
        }]}
        allowed = inspect_plan(model, decisions, root / "staging", root / "projects", runtime,
                               folder_intents=intent)
        self.assertEqual(allowed["status"], "ready_for_write_review")
        self.assertIn(project["path"], allowed["directories_to_create"])
        self.assertNotIn("2026", allowed["directories_to_create"])
        self.assertNotIn("2026/7月", allowed["directories_to_create"])

        for field in ('order_id', 'path', 'shoot_date', 'folder_request'):
            malformed=deepcopy(intent)
            malformed['intentions'][0].pop(field)
            rejected=inspect_plan(model,decisions,root/'staging',root/'projects',runtime,folder_intents=malformed)
            self.assertEqual(rejected['status'],'blocked',field)
        for field in ('customer_name', 'package_code', 'order_id', 'shoot_date'):
            malformed=deepcopy(intent)
            malformed['intentions'][0]['folder_request']['payload'].pop(field)
            rejected=inspect_plan(model,decisions,root/'staging',root/'projects',runtime,folder_intents=malformed)
            self.assertEqual(rejected['status'],'blocked',field)

        conflict = deepcopy(intent)
        conflict["intentions"].append({"action": "create_required", "path": project["path"], "order_id": "00001"})
        conflicted = inspect_plan(model, decisions, root / "staging", root / "projects", runtime,
                                  folder_intents=conflict)
        self.assertEqual(conflicted["status"], "blocked")
        self.assertTrue(any("folder_order_intent_conflict" in reason for reason in conflicted["blocking_reasons"]))

    def test_symlink_parent_and_existing_target_are_blocked(self):
        temp, root, model, decisions, runtime = _fixture()
        self.addCleanup(temp.cleanup)
        project = next(p for p in model["projects"] if p["project_id"] == decisions["segments"][0]["project_id"])
        category = root / "projects" / project["path"] / "4选片用JPG原片"
        outside = root / "outside"
        outside.mkdir()
        category.symlink_to(outside, target_is_directory=True)
        symlink_result = inspect_plan(model, decisions, root / "staging", root / "projects", runtime)
        self.assertEqual(symlink_result["status"], "blocked")
        self.assertTrue(any("target_parent_symlink" in reason for reason in symlink_result["blocking_reasons"]))

        category.unlink()
        source = root / "staging" / model["units"][0]["files"][0]["source_path"]
        target = category / Path(model["units"][0]["files"][0]["name"]).name
        target.parent.mkdir(parents=True)
        folded_target = target.with_name(target.name.swapcase())
        folded_target.write_bytes(source.read_bytes())
        folded_result = inspect_plan(model, decisions, root / "staging", root / "projects", runtime)
        self.assertEqual(folded_result["status"], "blocked")
        self.assertTrue(any("target_name_conflict" in reason for reason in folded_result["blocking_reasons"]))
        folded_target.unlink()
        target.write_bytes(source.read_bytes())
        target_result = inspect_plan(model, decisions, root / "staging", root / "projects", runtime)
        self.assertEqual(target_result["status"], "blocked")
        self.assertTrue(any("target_exists" in reason for reason in target_result["blocking_reasons"]))

    def test_invalid_source_receipt_blocks_without_writing(self):
        temp, root, model, decisions, runtime = _fixture()
        self.addCleanup(temp.cleanup)
        source = root / "staging" / model["units"][0]["files"][0]["source_path"]
        before = {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}
        source.write_bytes(source.read_bytes() + b"changed")
        result = inspect_plan(model, decisions, root / "staging", root / "projects", runtime)

        self.assertEqual(result["status"], "blocked")
        self.assertTrue(any("source_verification_failed" in reason for reason in result["blocking_reasons"]))
        after = {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}
        self.assertEqual(after, {**before, source: before[source] + b"changed"})

    def test_preflight_does_not_change_files(self):
        temp, root, model, decisions, runtime = _fixture()
        self.addCleanup(temp.cleanup)
        before = {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}
        result = inspect_plan(model, decisions, root / "staging", root / "projects", runtime)
        after = {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}

        self.assertEqual(result["status"], "ready_for_write_review")
        self.assertEqual(before, after)
        self.assertEqual(result["summary"]["bytes"], result["capacity"]["required_bytes"])


if __name__ == "__main__":
    unittest.main()
