import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from test_intake import fixture
from muli_sorter.cli import main, output_directory
from muli_sorter.preview import build_preview


class PreviewTests(unittest.TestCase):
    def test_realistic_readonly_run_and_missing_receipt(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            staging, projects = root / "staging", root / "projects"
            staging.mkdir()
            (projects / "2026/9月/20260928_00888_合成项目").mkdir(parents=True)
            m = fixture(staging)
            interrupted = staging / "BATCH_20260928_000002"
            interrupted.mkdir()
            (interrupted / "SOURCE_DATA").symlink_to(root / "outside")
            snapshot = {"generated_at": datetime.now(timezone.utc).isoformat(), "batches": [{**m["batch"], "revision": 1}]}
            before = {str(p): (p.stat().st_size, p.stat().st_mtime_ns) for p in staging.rglob("*") if p.is_file()}
            report = build_preview(staging, projects, snapshot)
            self.assertEqual(report["summary"]["verified_batches"], 1)
            self.assertEqual(report["summary"]["blocked_batches"], 1)
            self.assertEqual(report["summary"]["ready_groups"], 0)
            self.assertEqual(report["summary"]["review_groups"], 1)
            self.assertEqual(report["batches"][1]["status"], "waiting")
            state = root / "state.json"
            state.write_text(json.dumps(snapshot))
            main(["--staging", str(staging), "--projects", str(projects), "--runtime-snapshot", str(state), "--output", str(root / "out")])
            self.assertTrue((root / "out/分类预览.html").is_file())
            self.assertTrue((root / "out/拍摄段确认.html").is_file())
            model = json.loads((root / "out/确认模型.json").read_text())
            self.assertEqual(len(model["units"]), 1)
            self.assertEqual(len(model["excluded_batches"]), 1)
            self.assertEqual(model["initial_segments"][0]["decision"], "pending")
            self.assertEqual(before, {str(p): (p.stat().st_size, p.stat().st_mtime_ns) for p in staging.rglob("*") if p.is_file()})

    def test_output_cannot_be_input_or_child_or_parent(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t) / "source"
            p.mkdir()
            for out in (p, p / "output", p.parent):
                with self.assertRaises(ValueError):
                    output_directory(out, [p])
            alias = Path(t) / "alias"
            alias.symlink_to(p, target_is_directory=True)
            with self.assertRaises(ValueError):
                output_directory(alias / "output", [p])

    def test_no_runtime_snapshot_blocks_classification(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            staging, projects = root / "staging", root / "projects"
            staging.mkdir()
            projects.mkdir()
            fixture(staging)
            report = build_preview(staging, projects)
            self.assertEqual(report["summary"]["verified_batches"], 0)
            self.assertEqual(report["summary"]["groups"], 0)


if __name__ == "__main__":
    unittest.main()
