import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest

from muli_sorter.history_catalog import HistoryCatalog, HistoryCatalogError


HASH_A = "a" * 64
HASH_B = "b" * 64
SCOPE = "c" * 64
SCOPE_NEW = "d" * 64


class HistoryCatalogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.path = self.root / "history.sqlite3"
        self.identity = {"service": "archive", "state": "test"}

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def item(job_id, unit_id, completed_at, digest=HASH_A, size=10):
        return {
            "unit_id": unit_id,
            "job_id": job_id,
            "mode": "copy",
            "completed_at": completed_at,
            "project": {"name": "Project", "path": "项目/独立归档"},
            "file_count": 1,
            "files": [{"name": "A.RAW", "target_path": "项目/A.RAW",
                        "size_bytes": size, "blake3": digest}],
        }

    def test_latest_unit_wins_and_metadata_is_separate(self):
        catalog = HistoryCatalog(self.path, self.identity)
        catalog.replace_job("job-old", [(self.item("job-old", "unit-1", "2026-01-01T00:00:00Z"), SCOPE)])
        catalog.replace_job("job-new", [(self.item("job-new", "unit-1", "2026-02-01T00:00:00Z", HASH_B), SCOPE)])
        page = catalog.page()
        self.assertEqual(page["total"], 1)
        self.assertEqual(page["items"][0]["job_id"], "job-new")
        self.assertEqual(catalog.generation, 2)
        self.assertEqual(catalog.get_meta("source_version"), None)
        catalog.set_meta("source_version", {"revision": 3})
        self.assertEqual(catalog.get_meta("source_version"), {"revision": 3})
        self.assertEqual(catalog.generation, 2)
        catalog.close()

    def test_latest_removal_falls_back_and_count_never_duplicates(self):
        catalog = HistoryCatalog(self.path, self.identity)
        old = self.item('old', 'unit', '2026-01-01T00:00:00Z')
        new = self.item('new', 'unit', '2026-02-01T00:00:00Z')
        catalog.replace_job('old', [(old, SCOPE)])
        catalog.replace_job('new', [(new, SCOPE_NEW)])
        self.assertEqual(catalog.page()['total'], 1)
        catalog.remove_job('new')
        self.assertEqual(catalog.page()['items'], [old])
        self.assertEqual(catalog.page()['total'], 1)
        catalog.close()
        catalog = HistoryCatalog(self.path, self.identity)
        self.assertEqual(catalog.page()['items'], [old])
        catalog.remove_job('old')
        self.assertEqual(catalog.page()['total'], 0)
        catalog.close()

    def test_precise_queries_and_page_sizes(self):
        catalog = HistoryCatalog(self.path, self.identity)
        items = [(self.item("job-main", f"unit-{i:04d}", f"2026-01-01T00:{i:02d}:00Z",
                             HASH_A if i % 2 == 0 else HASH_B, i), SCOPE)
                  for i in range(1000)]
        catalog.replace_job("job-main", items)
        catalog.replace_job("job-history", [(self.item("job-history", "unit-0000", "2025-01-01T00:00:00Z"), SCOPE)])

        self.assertEqual(catalog.job_ids(unit_ids=["unit-0000"]), {"job-main", "job-history"})
        self.assertEqual(catalog.job_ids(fingerprints=[(HASH_A, 0)]), {"job-main"})
        self.assertEqual(catalog.job_ids(fingerprints=[(HASH_B, 1)]), {"job-main"})
        self.assertEqual(catalog.job_ids(), set())
        self.assertEqual(catalog.unit_ids(job_ids=["job-main"]), {f"unit-{i:04d}" for i in range(1000)})
        self.assertEqual(catalog.unit_ids(), set())
        self.assertEqual(catalog.matching_scopes(fingerprints=[(HASH_B, 1)]), {"unit-0001": SCOPE})
        self.assertEqual(catalog.matching_jobs(fingerprints=[(HASH_B, 1)]), {"job-main"})
        self.assertEqual(catalog.matching_scopes(), {})
        self.assertEqual(catalog.matching_jobs(), set())

        for limit in (1, 10, 100, 1000):
            page = catalog.page(offset=0, limit=limit)
            self.assertEqual(page["total"], 1000)
            self.assertEqual(len(page["items"]), limit)
            self.assertEqual(page["next_offset"], limit if limit < 1000 else None)
        page = catalog.page(offset=900, limit=100)
        self.assertEqual(len(page["items"]), 100)
        self.assertIsNone(page["next_offset"])
        catalog.close()

    def test_matching_scopes_unions_filters_and_uses_latest_matching_version(self):
        catalog = HistoryCatalog(self.path, self.identity)
        fingerprint = (HASH_A, 77)
        catalog.replace_job("job-old", [(self.item("job-old", "unit-version", "2026-01-01T00:00:00Z",
                                                     fingerprint[0], fingerprint[1]), SCOPE)])
        catalog.replace_job("job-new", [(self.item("job-new", "unit-version", "2026-02-01T00:00:00Z",
                                                     fingerprint[0], fingerprint[1]), SCOPE_NEW)])
        catalog.replace_job("job-wide", [
            (self.item("job-wide", "unit-other", "2026-03-01T00:00:00Z", HASH_B, 88), SCOPE),
        ])
        self.assertEqual(catalog.matching_scopes(unit_ids=["unit-version"]),
                         {"unit-version": SCOPE_NEW})
        self.assertEqual(catalog.matching_scopes(fingerprints=[fingerprint]),
                         {"unit-version": SCOPE_NEW})
        self.assertEqual(catalog.matching_jobs(fingerprints=[fingerprint]), {"job-new"})
        self.assertEqual(catalog.matching_scopes(unit_ids=["unit-version"],
                                                 fingerprints=[(HASH_B, 88)]),
                         {"unit-version": SCOPE_NEW, "unit-other": SCOPE})
        catalog.close()

    def test_each_fingerprint_selects_one_latest_origin(self):
        catalog = HistoryCatalog(self.path, self.identity)
        fingerprint = (HASH_A, 999)
        for index in range(1000):
            job_id = f"job-{index:04d}"
            unit_id = f"unit-{index:04d}"
            catalog.replace_job(job_id, [(
                self.item(job_id, unit_id, f"2026-01-01T00:{index:04d}:00Z",
                          fingerprint[0], fingerprint[1]), SCOPE)])
        self.assertEqual(catalog.matching_scopes(fingerprints=[fingerprint]),
                         {"unit-0999": SCOPE})
        self.assertEqual(catalog.matching_jobs(fingerprints=[fingerprint]), {"job-0999"})
        catalog.close()

    def test_external_projection_without_blake3_stays_unit_queryable(self):
        catalog = HistoryCatalog(self.path, self.identity)
        external = self.item("external-job", "external-unit", "2026-04-01T00:00:00Z")
        del external["files"][0]["blake3"]
        catalog.replace_job("external-job", [(external, SCOPE)])
        self.assertEqual(catalog.unit_ids(["external-job"]), {"external-unit"})
        self.assertEqual(catalog.matching_scopes(unit_ids=["external-unit"]),
                         {"external-unit": SCOPE})
        self.assertEqual(catalog.matching_scopes(fingerprints=[(HASH_A, 10)]), {})
        catalog.close()

    def test_replace_job_failure_rolls_back_and_remove_is_durable(self):
        catalog = HistoryCatalog(self.path, self.identity)
        original = self.item("job", "unit", "2026-01-01T00:00:00Z")
        catalog.replace_job("job", [(original, SCOPE)])
        before = catalog.generation
        with catalog._db:
            catalog._db.execute("""CREATE TRIGGER fail_fingerprint
                BEFORE INSERT ON fingerprints
                BEGIN SELECT RAISE(ABORT, 'test failure'); END""")
        with self.assertRaises(HistoryCatalogError):
            catalog.replace_job("job", [(self.item("job", "unit", "2026-02-01T00:00:00Z", HASH_B), SCOPE)])
        self.assertEqual(catalog.page()["items"][0], original)
        self.assertEqual(catalog.generation, before)
        catalog._db.execute("DROP TRIGGER fail_fingerprint")
        catalog.remove_job("job")
        self.assertEqual(catalog.page()["total"], 0)
        self.assertEqual(catalog.generation, before + 1)
        catalog.close()

    def test_identity_corruption_and_replacement_fail_closed(self):
        catalog = HistoryCatalog(self.path, self.identity)
        catalog.close()
        with self.assertRaises(HistoryCatalogError):
            HistoryCatalog(self.path, {"service": "other"})

        replacement = self.root / "replacement.sqlite3"
        replacement.write_bytes(b"not sqlite")
        os.replace(replacement, self.path)
        with self.assertRaises(HistoryCatalogError):
            HistoryCatalog(self.path, self.identity)

        self.path.unlink()
        target = self.root / "target.sqlite3"
        sqlite3.connect(target).close()
        os.symlink(target, self.path)
        with self.assertRaises(HistoryCatalogError):
            HistoryCatalog(self.path, self.identity)

    def test_transaction_failure_does_not_publish_new_rows(self):
        catalog = HistoryCatalog(self.path, self.identity)
        with self.assertRaises(HistoryCatalogError):
            catalog.replace_job("job", [(self.item("other", "unit", "2026-01-01T00:00:00Z"), SCOPE)])
        self.assertEqual(catalog.page()["total"], 0)
        catalog.close()


if __name__ == "__main__":
    unittest.main()
