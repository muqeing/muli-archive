import json
import hashlib
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from muli_sorter.archive_io import ArchiveError
from muli_sorter.archive_reservations import ReservationIndex


HASH_A = "a" * 64
HASH_B = "b" * 64


class ReservationIndexTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.identity = {"service": "archive", "state": "test"}

    def tearDown(self):
        self.temp.cleanup()

    def write_legacy(self, value):
        path = self.root / "reservations.json"
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return path

    def test_migrates_once_and_scopes_deterministic_reads(self):
        legacy = self.write_legacy({
            "z-source": {"target_path": "2026/项目/IMG.Z", "blake3": HASH_B},
            "a-source": {"target_path": "2026/项目/IMG.A", "blake3": HASH_A},
            "other": {"target_path": "other/place/X", "blake3": HASH_A},
        })
        original = legacy.read_bytes()
        index = ReservationIndex(self.root, self.identity)
        self.assertEqual(index.get("a-source"), {"target_path": "2026/项目/IMG.A", "blake3": HASH_A})
        self.assertEqual(list(index.subset(["z-source", "missing", "a-source"])), ["a-source", "z-source"])
        self.assertEqual(list(index.destinations(["2026/项目"])), ["a-source", "z-source"])
        self.assertEqual(index.destinations(["other/place"]), {"other": {"target_path": "other/place/X", "blake3": HASH_A}})
        with patch("muli_sorter.archive_reservations.read_bytes", side_effect=AssertionError("legacy reread")):
            index.reserve({"new": {"target_path": "new/place/X", "blake3": HASH_B}})
            self.assertIsNotNone(index.get("new"))
            self.assertEqual(index.subset(["new"])["new"]["blake3"], HASH_B)
            self.assertEqual(index.destinations(["new/place"])["new"]["target_path"], "new/place/X")
        index.close()
        self.assertEqual(legacy.read_bytes(), original)

        reopened = ReservationIndex(self.root, self.identity)
        self.assertEqual(reopened.get("new")["target_path"], "new/place/X")
        reopened.close()

    def test_exact_same_target_and_digest_can_have_multiple_sources(self):
        index = ReservationIndex(self.root, self.identity)
        target = {"target_path": "项目/独立归档/A.RAW", "blake3": HASH_A}
        index.reserve({"one": target, "two": target})
        self.assertEqual(list(index.destinations(["项目/独立归档"])), ["one", "two"])
        self.assertEqual(index.owners("项目/独立归档/a.raw"), {
            "one": target, "two": target,
        })
        index.close()

    def test_subset_chunks_large_source_scope_and_reopens(self):
        index = ReservationIndex(self.root, self.identity)
        rows = {
            f"source-{number:04d}": {
                "target_path": f"parent-{number % 5}/file-{number:04d}.RAW",
                "blake3": f"{number + 1:064x}",
            }
            for number in range(1205)
        }
        index.reserve(rows)
        self.assertEqual(index.subset(list(rows) + ["missing"]), rows)
        index.close()

        reopened = ReservationIndex(self.root, self.identity)
        self.assertEqual(reopened.subset(list(rows)), rows)
        reopened.close()

    def test_rollback_export_is_complete_without_changing_legacy_or_authority(self):
        legacy=self.write_legacy({'old':{'target_path':'old/A','blake3':HASH_A}})
        before=legacy.read_bytes()
        index=ReservationIndex(self.root,self.identity)
        index.reserve({'new':{'target_path':'new/A','blake3':HASH_B}})
        result=index.export_rollback('reservations-rollback-test.json')
        exported=self.root/result['file']
        self.assertEqual(result['count'],2)
        self.assertEqual(json.loads(exported.read_text()),index.subset(['old','new']))
        self.assertEqual(legacy.read_bytes(),before)
        self.assertEqual(hashlib.sha256(exported.read_bytes()).hexdigest(),result['sha256'])
        with self.assertRaises(FileExistsError):index.export_rollback(result['file'])
        self.assertIsNotNone(index.get('new'))
        index.close()

    def test_case_unicode_target_conflict_is_atomic(self):
        index = ReservationIndex(self.root, self.identity)
        index.reserve({"first": {"target_path": "项目/É.RAW", "blake3": HASH_A}})
        with self.assertRaises(ArchiveError):
            index.reserve({
                "new-safe": {"target_path": "new/X.RAW", "blake3": HASH_B},
                "new-conflict": {"target_path": "项目/e\u0301.raw", "blake3": HASH_A},
            })
        self.assertIsNone(index.get("new-safe"))
        self.assertIsNone(index.get("new-conflict"))
        index.close()

    def test_existing_source_can_only_be_replayed_identically(self):
        index = ReservationIndex(self.root, self.identity)
        index.reserve({"source": {"target_path": "p/A", "blake3": HASH_A}})
        index.reserve({"source": {"target_path": "p/A", "blake3": HASH_A}})
        with self.assertRaises(ArchiveError):
            index.reserve({"source": {"target_path": "p/B", "blake3": HASH_A}})
        index.close()

    def test_identity_mismatch_and_corrupt_database_fail_closed(self):
        index = ReservationIndex(self.root, self.identity)
        index.close()
        with self.assertRaises(ArchiveError):
            ReservationIndex(self.root, {"service": "different"})

        db = self.root / "reservations.sqlite3"
        db.unlink()
        db.write_bytes(b"not sqlite")
        with self.assertRaises(ArchiveError):
            ReservationIndex(self.root, self.identity)

    def test_symlinked_inputs_are_rejected(self):
        legacy = self.root / "legacy.json"
        legacy.write_text("{}", encoding="utf-8")
        os.symlink(legacy, self.root / "reservations.json")
        with self.assertRaises(ArchiveError):
            ReservationIndex(self.root, self.identity)

        (self.root / "reservations.json").unlink()
        index = ReservationIndex(self.root, self.identity)
        index.close()
        db = self.root / "reservations.sqlite3"
        db.unlink()
        os.symlink(legacy, db)
        with self.assertRaises(ArchiveError):
            ReservationIndex(self.root, self.identity)

    def test_replaced_legacy_is_rejected_without_fallback(self):
        legacy = self.write_legacy({"source": {"target_path": "p/A", "blake3": HASH_A}})
        index = ReservationIndex(self.root, self.identity)
        replacement = self.root / "replacement.json"
        replacement.write_bytes(legacy.read_bytes())
        os.replace(replacement, legacy)
        with self.assertRaises(ArchiveError):
            index.get("source")
        index.close()

    def test_incomplete_migration_is_completed_transactionally(self):
        self.write_legacy({"source": {"target_path": "p/A", "blake3": HASH_A}})
        db = self.root / "reservations.sqlite3"
        connection = sqlite3.connect(db)
        identity_json = json.dumps(self.identity, ensure_ascii=False, sort_keys=True,
                                   separators=(",", ":"))
        with connection:
            connection.execute("CREATE TABLE meta (name TEXT PRIMARY KEY, value TEXT NOT NULL)")
            connection.execute("""CREATE TABLE reservations (
                source_path TEXT PRIMARY KEY, target_path TEXT NOT NULL,
                target_fold TEXT NOT NULL, target_parent TEXT NOT NULL,
                target_parent_fold TEXT NOT NULL, blake3 TEXT NOT NULL
            )""")
            connection.execute("CREATE INDEX reservations_target_fold ON reservations(target_fold)")
            connection.execute("CREATE INDEX reservations_target_parent_fold ON reservations(target_parent_fold, source_path)")
            connection.executemany("INSERT INTO meta(name,value) VALUES (?,?)", [
                ("schema", "archive-reservations/1"),
                ("identity", identity_json),
                ("identity_digest", hashlib.sha256(identity_json.encode()).hexdigest()),
                ("migration_complete", "0"),
            ])
        connection.close()
        index = ReservationIndex(self.root, self.identity)
        self.assertEqual(index.get("source")["target_path"], "p/A")
        index.close()
        with sqlite3.connect(db) as connection:
            self.assertEqual(connection.execute(
                "SELECT value FROM meta WHERE name='migration_complete'").fetchone(), ("1",))


if __name__ == "__main__":
    unittest.main()
