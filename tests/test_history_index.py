"""Durable archive-history index: reuse, invalidation, rejection and pruning."""
import json
import tempfile
import unittest
from pathlib import Path

from muli_sorter import archive_history as history_module
from muli_sorter.archive_history import ArchiveHistory

LABEL = "木梨家庭摄影工作室/摄影工作室/拍摄项目"
PROJECT_PATH = "2026/7月/20260709_09999_合成"
JOB_ID = "b" * 64
RECEIPT_ID = "c" * 64
UNIT_ID = "unit-" + "d" * 20


def tmp_root():
    return Path(tempfile.mkdtemp(prefix="history-index-")).resolve()


def unit_files():
    return [
        {"source_path": "BATCH_20260709_000001/DCIM/A1.JPG", "name": "DCIM/A1.JPG",
         "size_bytes": 101, "blake3": "1" * 64},
        {"source_path": "BATCH_20260709_000001/DCIM/A1.XMP", "name": "DCIM/A1.XMP",
         "size_bytes": 102, "blake3": "2" * 64},
    ]


def receipt_rows(files):
    return [{**row, "published": True, "target_signature": [1, 2, row["size_bytes"], 3, 4],
             "target_path": PROJECT_PATH + "/" + row["name"]} for row in files]


def write_fixture(root, *, mode="copy", label=LABEL):
    state = root / "state"
    (state / "requests").mkdir(parents=True)
    (state / "units").mkdir(parents=True)
    projects = root / "projects"
    projects.mkdir(parents=True)
    identity = {"source_identity": [1, 2, 3, 4, 5], "target_identity": [6, 7, 8, 9, 10],
                "staging": str(root / "staging"), "projects": str(projects)}
    files = unit_files()
    rows = receipt_rows(files)
    receipt = {"schema_version": "archive/0.4", "real_media_write_authorized": True,
               "storage": "independent_copy" if mode == "copy" else "same_volume_move",
               "status": "completed", "unit_id": UNIT_ID, "job_id": RECEIPT_ID,
               "example_data": False, "completed_at": "2026-07-09T10:00:00+00:00",
               "file_count": len(rows), "files": rows,
               "project": {"name": "合成项目", "path": PROJECT_PATH}}
    job = {"job_id": JOB_ID, "request_digest": "e" * 64, "status": "completed",
           "example_data": False, "completed_at": "2026-07-09T10:00:00+00:00",
           "updated_at": "2026-07-09T10:00:00+00:00", "cleanup_started": True,
           "archive_options": {"mode": mode},
           "outcomes": [{"unit_id": UNIT_ID, "status": "completed",
                         "receipt": "receipt-" + RECEIPT_ID + ".json", "files": len(rows)}]}
    (state / "requests" / ("job-" + JOB_ID + ".json")).write_text(json.dumps(job))
    (state / "units" / ("receipt-" + RECEIPT_ID + ".json")).write_text(json.dumps(receipt))
    if mode == "move":
        journal = {"identity": {"job_id": JOB_ID, "request_digest": job["request_digest"],
                                "roots": identity, "files": rows},
                   "files": [{"source_path": row["source_path"], "state": "removed"} for row in rows]}
        (state / "requests" / ("move-" + JOB_ID + ".json")).write_text(json.dumps(journal))
    model = {"report_id": "sha256:" + "f" * 64, "units": [{"unit_id": UNIT_ID, "files": files}]}
    return {"root": root, "state": state, "identity": identity, "model": model, "label": label,
            "index": state / history_module.HISTORY_INDEX_DIR, "job_file": state / "requests" / ("job-" + JOB_ID + ".json")}


def counting_reads(history):
    counter = {"n": 0}
    original = history.read

    def wrapped(path):
        counter["n"] += 1
        return original(path)

    history.read = wrapped
    return counter


class ContentCopyTests(unittest.TestCase):
    def test_content_copies_never_overlap_retained_units(self):
        def origin(uid):
            return {"unit_id": uid, "job_id": "job", "mode": "copy", "completed_at": "t",
                    "project": {"name": "p", "path": "p"}, "file_count": 1, "files": []}
        archive = {"archived_units": [], "retained_unit_ids": ["unit-a"]}
        copies = {"unit-a": origin("unit-a"), "unit-b": origin("unit-b")}
        result = ArchiveHistory._with_content_copies(archive, copies)
        self.assertEqual({row["unit_id"] for row in result["archived_units"]}, {"unit-b"})
        self.assertEqual(result["content_copy_units"], 1)

    def test_for_report_matches_snapshot_including_content_copies(self):
        fixture = write_fixture(tmp_root())
        original = unit_files()
        copied = [dict(row, source_path=row["source_path"].replace(
            "BATCH_20260709_000001", "BATCH_20260710_000009")) for row in original]
        copy_id = "unit-" + "9" * 20
        model = {"report_id": "sha256:" + "c" * 64,
                 "units": [{"unit_id": UNIT_ID, "files": original},
                           {"unit_id": copy_id, "files": copied}]}
        history = ArchiveHistory(fixture["state"], fixture["identity"], LABEL, True)
        first = history.snapshot(model)
        again = history.for_report(model["report_id"])
        self.assertEqual([row["unit_id"] for row in again["archived_units"]],
                         [row["unit_id"] for row in first["archived_units"]])
        self.assertEqual(again.get("content_copy_units"), 1)
        self.assertEqual(again.get("content_copy_units"),
                         first.get("content_copy_units"))

    def test_recopied_content_is_reported_as_already_archived(self):
        fixture = write_fixture(tmp_root())
        original = unit_files()
        copied = [dict(row, source_path=row["source_path"].replace("BATCH_20260709_000001",
                                                                  "BATCH_20260710_000002"))
                  for row in original]
        copy_unit_id = "unit-" + "e" * 20
        model = {"report_id": "sha256:" + "a" * 64,
                 "units": [{"unit_id": UNIT_ID, "files": original},
                           {"unit_id": copy_unit_id, "files": copied}]}
        history = ArchiveHistory(fixture["state"], fixture["identity"], LABEL, True)
        result = history.snapshot(model)
        self.assertEqual({row["unit_id"] for row in result["archived_units"]},
                         {UNIT_ID, copy_unit_id})
        copy_row = next(row for row in result["archived_units"] if row["unit_id"] == copy_unit_id)
        self.assertTrue(copy_row["in_current_model"])
        self.assertEqual(copy_row["content_copy_of"], UNIT_ID)
        self.assertEqual(result["content_copy_units"], 1)

        from muli_sorter.review_projection import pending_view
        materials = {"units": {UNIT_ID: {"category": "shoot"},
                               copy_unit_id: {"category": "shoot"}}}
        view, _ = pending_view(model, result, materials)
        self.assertEqual([unit["unit_id"] for unit in view["units"]], [])
        self.assertEqual(len(view["presentation"]["hidden_units"]), 2)

    def test_different_content_in_a_new_batch_is_still_pending(self):
        fixture = write_fixture(tmp_root())
        other = [dict(row, blake3="9" * 64, source_path=row["source_path"].replace(
            "BATCH_20260709_000001", "BATCH_20260710_000003")) for row in unit_files()]
        other_id = "unit-" + "f" * 20
        model = {"report_id": "sha256:" + "b" * 64,
                 "units": [{"unit_id": UNIT_ID, "files": unit_files()},
                           {"unit_id": other_id, "files": other}]}
        result = ArchiveHistory(fixture["state"], fixture["identity"], LABEL, True).snapshot(model)
        self.assertEqual(result.get("content_copy_units"), None)
        from muli_sorter.review_projection import pending_view
        materials = {"units": {UNIT_ID: {"category": "shoot"}, other_id: {"category": "shoot"}}}
        view, _ = pending_view(model, result, materials)
        self.assertEqual([unit["unit_id"] for unit in view["units"]], [other_id])


class HistoryIndexTests(unittest.TestCase):
    def test_restart_reuses_the_index_without_reopening_receipts(self):
        fixture = write_fixture(tmp_root())
        first = ArchiveHistory(fixture["state"], fixture["identity"], LABEL, True)
        one = first.snapshot(fixture["model"])
        self.assertEqual([row["unit_id"] for row in one["archived_units"]], [UNIT_ID])
        self.assertTrue(one["archived_units"][0]["in_current_model"])
        index_files = sorted(fixture["index"].glob("*.json"))
        self.assertEqual([path.name for path in index_files], [JOB_ID + ".json"])

        second = ArchiveHistory(fixture["state"], fixture["identity"], LABEL, True)
        reads = counting_reads(second)
        two = second.snapshot(fixture["model"])
        self.assertEqual(reads["n"], 0, "a restarted console must not reopen the job or its receipts")
        self.assertEqual(two["archived_units"], one["archived_units"])

    def test_index_survives_a_move_job_and_keeps_its_journal_dependency(self):
        fixture = write_fixture(tmp_root(), mode="move")
        one = ArchiveHistory(fixture["state"], fixture["identity"], LABEL, True).snapshot(fixture["model"])
        self.assertEqual(one["archived_units"][0]["mode"], "move")
        second = ArchiveHistory(fixture["state"], fixture["identity"], LABEL, True)
        reads = counting_reads(second)
        two = second.snapshot(fixture["model"])
        self.assertEqual(reads["n"], 0)
        self.assertEqual(two["archived_units"], one["archived_units"])

        journal = fixture["state"] / "requests" / ("move-" + JOB_ID + ".json")
        document = json.loads(journal.read_text())
        document["files"].append({"source_path": "BATCH_20260709_000001/DCIM/A2.JPG", "state": "removed"})
        journal.write_text(json.dumps(document))
        third = ArchiveHistory(fixture["state"], fixture["identity"], LABEL, True)
        reads = counting_reads(third)
        third.snapshot(fixture["model"])
        self.assertGreater(reads["n"], 0, "a changed cleanup journal must invalidate the index")

    def test_changed_receipt_invalidates_and_rewrites_the_index(self):
        fixture = write_fixture(tmp_root())
        ArchiveHistory(fixture["state"], fixture["identity"], LABEL, True).snapshot(fixture["model"])
        receipt = fixture["state"] / "units" / ("receipt-" + RECEIPT_ID + ".json")
        document = json.loads(receipt.read_text())
        document["files"][0]["target_path"] = document["files"][0]["target_path"] + ".renamed"
        receipt.write_text(json.dumps(document))

        second = ArchiveHistory(fixture["state"], fixture["identity"], LABEL, True)
        reads = counting_reads(second)
        result = second.snapshot(fixture["model"])
        self.assertGreater(reads["n"], 0)
        self.assertTrue(result["archived_units"][0]["files"][0]["target_path"].endswith(".renamed"))
        self.assertEqual(json.loads((fixture["index"] / (JOB_ID + ".json")).read_text())["version"],
                         history_module.HISTORY_INDEX_VERSION)

    def test_corrupt_index_falls_back_and_is_repaired(self):
        fixture = write_fixture(tmp_root())
        expected = ArchiveHistory(fixture["state"], fixture["identity"], LABEL, True).snapshot(fixture["model"])
        index_file = fixture["index"] / (JOB_ID + ".json")
        index_file.write_text("{ this is not json")
        second = ArchiveHistory(fixture["state"], fixture["identity"], LABEL, True)
        reads = counting_reads(second)
        result = second.snapshot(fixture["model"])
        self.assertGreater(reads["n"], 0)
        self.assertEqual(result["archived_units"], expected["archived_units"])
        self.assertEqual(json.loads(index_file.read_text())["job_id"], JOB_ID)

    def test_index_pointing_outside_the_state_root_is_rejected(self):
        fixture = write_fixture(tmp_root())
        ArchiveHistory(fixture["state"], fixture["identity"], LABEL, True).snapshot(fixture["model"])
        index_file = fixture["index"] / (JOB_ID + ".json")
        document = json.loads(index_file.read_text())
        document["dependencies"].append(["/etc/hostname", [1, 2, 3, 4]])
        index_file.write_text(json.dumps(document))
        second = ArchiveHistory(fixture["state"], fixture["identity"], LABEL, True)
        reads = counting_reads(second)
        second.snapshot(fixture["model"])
        self.assertGreater(reads["n"], 0)

    def test_index_from_another_label_is_not_reused(self):
        fixture = write_fixture(tmp_root())
        ArchiveHistory(fixture["state"], fixture["identity"], LABEL, True).snapshot(fixture["model"])
        other = ArchiveHistory(fixture["state"], fixture["identity"], "另一个拍摄项目", True)
        reads = counting_reads(other)
        result = other.snapshot(fixture["model"])
        self.assertGreater(reads["n"], 0)
        self.assertTrue(result["archived_units"][0]["project"]["path"].startswith("另一个拍摄项目/"))

    def test_index_is_pruned_when_a_job_disappears(self):
        fixture = write_fixture(tmp_root())
        ArchiveHistory(fixture["state"], fixture["identity"], LABEL, True).snapshot(fixture["model"])
        index_file = fixture["index"] / (JOB_ID + ".json")
        self.assertTrue(index_file.exists())
        keep = fixture["index"] / "notes.txt"
        keep.write_text("keep me")
        fixture["job_file"].unlink()
        result = ArchiveHistory(fixture["state"], fixture["identity"], LABEL, True).snapshot(fixture["model"])
        self.assertEqual(result["archived_units"], [])
        self.assertFalse(index_file.exists())
        self.assertTrue(keep.exists(), "unrelated files in the index directory are left alone")


if __name__ == "__main__":
    unittest.main()
