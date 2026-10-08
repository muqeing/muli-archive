import json
import hashlib
import os
from pathlib import Path
import tempfile
import unittest

from muli_sorter.review_display_store import DisplayStore, FILENAME


class ReviewDisplayStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve() / "display-cache"
        self.root.mkdir(mode=0o700)
        self.binding = {
            "renderer": "review/2026-10-03",
            "production": True,
            "roots": {"staging": [10, 20], "projects": [30, 40]},
        }
        self.clock = lambda: 1_000.0
        self.store = DisplayStore(self.root, self.binding, max_age=100, wall_clock=self.clock)

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def bundle(report_id="report-1", html=b"<html>ready</html>"):
        return {
            "html_bytes": html,
            "report_id": report_id,
            "archive_state": {"report_id": report_id, "history_total": 1},
            "history_generation": "generation-1",
        }

    def save(self, key=("model", "one"), verified_at=950):
        self.store.save(self.bundle(), key, verified_at)

    def test_normal_snapshot_recovers_across_instances(self):
        self.save()
        other = DisplayStore(self.root, {"production": True, "roots": {"projects": [30, 40], "staging": [10, 20]}, "renderer": "review/2026-10-03"}, max_age=100, wall_clock=self.clock)
        loaded = other.load(("model", "one"))
        self.assertEqual(loaded["bundle"]["html_bytes"], b"<html>ready</html>")
        self.assertEqual(loaded["bundle"]["report_id"], "report-1")
        self.assertEqual(loaded["key"], ["model", "one"])
        self.assertEqual(loaded["verified_at"], 950.0)
        self.assertIsNotNone(other.load())

    def test_key_and_binding_changes_fail_closed(self):
        self.save()
        self.assertIsNone(self.store.load(("model", "two")))
        self.assertIsNone(DisplayStore(self.root, {**self.binding, "renderer": "review/other"}, max_age=100, wall_clock=self.clock).load())
        self.assertIsNone(DisplayStore(self.root, {**self.binding, "production": False}, max_age=100, wall_clock=self.clock).load())

    def test_corruption_and_internal_report_mismatch_are_rejected(self):
        self.save()
        path = self.root / FILENAME
        raw = json.loads(path.read_text())
        raw["bundle"]["archive_state"]["report_id"] = "other"
        path.write_text(json.dumps(raw))
        self.assertIsNone(self.store.load())

        self.save()
        raw = json.loads(path.read_text())
        raw["schema"] = "review-display-store/unknown"
        path.write_text(json.dumps(raw))
        self.assertIsNone(self.store.load())

        self.save()
        raw = json.loads(path.read_text())
        raw["bundle"]["archive_state"]["report_id"] = "other"
        unsigned = {key: raw[key] for key in raw if key != "sha256"}
        raw["sha256"] = hashlib.sha256(json.dumps(
            unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()).hexdigest()
        path.write_text(json.dumps(raw))
        self.assertIsNone(self.store.load())

        self.save()
        path.write_bytes(path.read_bytes()[:-1] + b"x")
        self.assertIsNone(self.store.load())

    def test_expired_and_future_snapshots_are_rejected(self):
        self.save(verified_at=899)
        self.assertIsNone(self.store.load())
        self.save(verified_at=1_001)
        self.assertIsNone(self.store.load())

    def test_symlink_and_oversized_snapshot_are_rejected(self):
        outside = Path(self.tmp.name) / "outside.json"
        outside.write_text("outside")
        (self.root / FILENAME).symlink_to(outside)
        self.assertIsNone(self.store.load())
        with self.assertRaises(ValueError):
            self.save()
        self.assertEqual(outside.read_text(), "outside")

        (self.root / FILENAME).unlink()
        (self.root / FILENAME).write_bytes(b"x" * 200)
        small = DisplayStore(self.root, self.binding, max_bytes=100, max_age=100, wall_clock=self.clock)
        self.assertIsNone(small.load())

    def test_hard_link_is_rejected_and_failed_save_keeps_old_snapshot(self):
        self.save()
        path = self.root / FILENAME
        link = self.root / "second-link"
        os.link(path, link)
        self.assertIsNone(self.store.load())
        with self.assertRaises(ValueError):
            self.save(key=("replacement",))
        self.assertTrue(path.exists())
        self.assertEqual(path.read_bytes(), link.read_bytes())

    def test_crash_left_temporary_file_does_not_replace_valid_snapshot(self):
        self.save()
        original = (self.root / FILENAME).read_bytes()
        (self.root / ".display-snapshot-crash").write_bytes(b"partial")
        self.assertEqual(self.store.load()["bundle"]["html_bytes"], b"<html>ready</html>")
        self.assertEqual((self.root / FILENAME).read_bytes(), original)

    def test_invalid_bundle_is_rejected_before_replacing_old_snapshot(self):
        self.save()
        original = (self.root / FILENAME).read_bytes()
        invalid = self.bundle()
        invalid["archive_state"] = {"report_id": "different"}
        with self.assertRaises(ValueError):
            self.store.save(invalid, ("new",), 950)
        self.assertEqual((self.root / FILENAME).read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
