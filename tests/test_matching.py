import tempfile
import unittest
from pathlib import Path
from muli_sorter.matching import group_files, project_index


def file(name, time="2026-09-28T10:00:00+08:00", trusted=True):
    return {"file_id": name, "relative_path": "DCIM/" + name, "resolved_path": "batch/SOURCE_DATA/DCIM/" + name, "resolved_source_id": "camera1", "size_bytes": 100,
            "metadata": {"capture_time": {"normalized": time, "confidence": "timezone_aware" if trusted else "wall_time"}, "camera": {"make": "TEST"}}}


PROJECTS = [{"project_id": "p1", "name": "项目一", "path": "2026/9月/project1", "dates": ["2026-09-28"]}, {"project_id": "p2", "name": "项目二", "path": "2026/9月/project2", "dates": ["2026-09-28"]}]
BINDING = {"manifest_id": "m1", "source_id": "camera1", "project_id": "p1", "start": "2026-09-28T09:00:00+08:00", "end": "2026-09-28T12:00:00+08:00", "confirmed": True, "confirmation_reference": "synthetic-human-confirmation"}


class MatchingTests(unittest.TestCase):
    def test_date_only_never_auto_assigns(self):
        for projects in ([PROJECTS[0]], PROJECTS):
            g = group_files([file("A.ARW")], projects, [], "m1")[0]
            self.assertEqual(g["status"], "review")
            self.assertEqual(len(g["candidates"]), len(projects))

    def test_explicit_device_and_window_binding(self):
        g = group_files([file("A.ARW"), file("A.JPG"), file("A.XMP")], PROJECTS, [BINDING], "m1")[0]
        self.assertEqual(g["status"], "ready")
        self.assertEqual(g["file_count"], 3)
        self.assertEqual(g["candidates"][0]["project_id"], "p1")

    def test_conflicting_bindings_block(self):
        g = group_files([file("A.ARW")], PROJECTS, [BINDING, {**BINDING, "project_id": "p2"}], "m1")[0]
        self.assertEqual(g["status"], "review")

    def test_confirmation_never_carries_to_next_batch_or_revision(self):
        for manifest_id in ("m2", "m1-r2"):
            g = group_files([file("A.ARW")], PROJECTS, [BINDING], manifest_id)[0]
            self.assertEqual(g["status"], "review")

    def test_missing_timezone_never_inferred(self):
        for time, trust in [("2026-09-28T10:00:00", False), ("2026-09-28T10:00:00+08:00", False), (None, False)]:
            g = group_files([file("A.ARW", time, trust)], PROJECTS, [BINDING], "m1")[0]
            self.assertEqual(g["status"], "review")

    def test_proxy_without_original_not_ready(self):
        g = group_files([file("DJI_1.LRF"), file("DJI_1.THM")], PROJECTS, [BINDING], "m1")[0]
        self.assertEqual(g["kind"], "proxy_only")
        self.assertEqual(g["status"], "review")

    def test_video_sidecars_and_auxiliary_separated(self):
        groups = group_files([file("A.MP4"), file("A.LRF"), file("A.THM"), file("A.db")], PROJECTS, [BINDING], "m1")
        self.assertEqual(sorted(g["file_count"] for g in groups), [1, 3])
        self.assertEqual([g["status"] for g in groups if g["kind"] == "auxiliary"], ["auxiliary"])

    def test_changed_binding_splits_continuous_files(self):
        bindings = [{**BINDING, "end": "2026-09-28T10:30:00+08:00"}, {**BINDING, "project_id": "p2", "start": "2026-09-28T10:30:00+08:00"}]
        groups = group_files([file("A.ARW"), file("B.ARW", "2026-09-28T10:31:00+08:00")], PROJECTS, bindings, "m1")
        self.assertEqual(len(groups), 2)
        self.assertEqual({g["candidates"][0]["project_id"] for g in groups}, {"p1", "p2"})

    def test_delayed_copy_matches_multiple_historical_days(self):
        projects = [
            {"project_id": "old1", "name": "七月项目甲", "path": "2026/7月/old1", "dates": ["2026-07-09"]},
            {"project_id": "old2", "name": "七月项目乙", "path": "2026/7月/old2", "dates": ["2026-07-10"]},
            PROJECTS[0],
        ]
        files = [file("A.ARW", "2026-07-09T10:00:00+08:00"), file("B.ARW", "2026-07-10T10:00:00+08:00"), file("C.ARW")]
        groups = group_files(files, projects, [], "copy-on-september-28")
        self.assertEqual(len(groups), 3)
        self.assertEqual({g["capture_date"] for g in groups}, {"2026-07-09", "2026-07-10", "2026-09-28"})
        self.assertEqual({g["candidates"][0]["project_id"] for g in groups}, {"old1", "old2", "p1"})
        self.assertTrue(all(g["status"] == "review" for g in groups))

    def test_same_device_does_not_extend_a_confirmed_shoot_to_other_days(self):
        groups = group_files([file("A.ARW"), file("B.ARW", "2026-09-29T10:00:00+08:00")], PROJECTS, [BINDING], "m1")
        self.assertEqual({g["capture_date"]: g["status"] for g in groups}, {"2026-09-28": "ready", "2026-09-29": "review"})

    def test_index_reads_only_structured_project_dirs(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            good = root / "2026/9月/20260928_00888_合成项目"
            good.mkdir(parents=True)
            (good / "1相机原素材/20260928_00999_不是项目").mkdir(parents=True)
            (root / "2026/9月/20260931_00889_错误日期").mkdir()
            (root / "2026/9月/20260928_00890_软链接").symlink_to(good, target_is_directory=True)
            index = project_index(root)
            self.assertEqual(len(index), 1)
            self.assertEqual(index[0]["order_id"], "00888")


if __name__ == "__main__":
    unittest.main()
