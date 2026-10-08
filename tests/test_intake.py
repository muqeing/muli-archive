import copy
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from blake3 import blake3
from muli_sorter.intake import EvidenceError, decode, read_bytes, relative, resolve_files, runtime_check, validate_record


def fixture(root, batch_id="BATCH_20260928_000001", uid="uid1"):
    folder = root / batch_id
    folder.mkdir()
    (folder / "SOURCE_DATA").mkdir()
    (folder / "SOURCE_DATA/A.ARW").write_bytes(b"synthetic")
    h = blake3(b"synthetic").hexdigest()
    now = datetime.now(timezone.utc).isoformat()
    m = {"schema_version": "1.1", "example_data": False, "manifest_id": f"{uid}-r1", "revision": 1,
         "batch": {"batch_id": batch_id, "batch_uid": uid, "state": "COMPLETED", "result": "COPY_VERIFIED", "completed_at": now, "source_id": "cam1"},
         "summary": {"pending_file_count": 0, "failed_file_count": 0, "selected_file_count": 1, "selected_bytes": 9, "verified_file_count": 1, "previously_ingested_count": 0},
         "scan_errors": [], "files": [{"file_id": "f1", "relative_path": "A.ARW", "filename": "A.ARW", "size_bytes": 9, "copy_status": "verified", "hash_match": True,
          "hash": {"algorithm": "blake3", "source": h, "destination": h, "readback_verified_at": now}, "existing_copy": None, "destination_relative_path": "SOURCE_DATA/A.ARW",
          "metadata": {"capture_time": {"normalized": "2026-09-28T10:00:00+08:00", "wall_time": "2026-09-28T10:00:00"}, "camera": {"make": "TEST", "model": "Synthetic"}}}]}
    write(root, m)
    return m


def write(root, m):
    folder = root / m["batch"]["batch_id"]
    raw = json.dumps(m).encode()
    md = b"# synthetic report\n" + raw
    (folder / "ingest_manifest.json").write_bytes(raw)
    (folder / "ingest_manifest.md").write_bytes(md)
    (folder / "ingest_complete.json").write_text(json.dumps({"algorithm": "blake3", "batch_uid": m["batch"]["batch_uid"], "manifest_id": m["manifest_id"], "revision": m["revision"], "example_data": m["example_data"], "json_blake3": blake3(raw).hexdigest(), "md_blake3": blake3(md).hexdigest()}))


class IntakeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.m = fixture(self.root)
        self.bid = self.m["batch"]["batch_id"]

    def test_success_and_media_size(self):
        m = validate_record(self.root, self.bid)
        self.assertEqual(len(resolve_files(self.root, m, {self.bid: m})), 1)
        (self.root / self.bid / "SOURCE_DATA/A.ARW").write_bytes(b"changed size")
        with self.assertRaises(EvidenceError):
            resolve_files(self.root, m, {self.bid: m})

    def test_success_does_not_modify_source(self):
        paths = list((self.root / self.bid).rglob("*"))
        before = {str(p): (p.stat().st_size, p.stat().st_mtime_ns) for p in paths}
        m = validate_record(self.root, self.bid)
        resolve_files(self.root, m, {self.bid: m})
        self.assertEqual(before, {str(p): (p.stat().st_size, p.stat().st_mtime_ns) for p in paths})

    def test_finalized_failed_is_not_success(self):
        self.m["finalized"] = True
        self.m["batch"]["state"] = "INTERRUPTED"
        write(self.root, self.m)
        with self.assertRaises(EvidenceError):
            validate_record(self.root, self.bid)

    def test_tampered_json_or_markdown_blocked(self):
        for name in ("ingest_manifest.json", "ingest_manifest.md"):
            with self.subTest(name=name):
                write(self.root, self.m)
                with (self.root / self.bid / name).open("ab") as f:
                    f.write(b" ")
                with self.assertRaises(EvidenceError):
                    validate_record(self.root, self.bid)

    def test_summary_and_hash_inconsistencies_blocked(self):
        for mutate in (lambda m: m["summary"].update(selected_bytes=10), lambda m: m["files"][0]["hash"].update(destination="0" * 64), lambda m: m["summary"].update(pending_file_count=1)):
            m = copy.deepcopy(self.m)
            mutate(m)
            write(self.root, m)
            with self.assertRaises(EvidenceError):
                validate_record(self.root, self.bid)

    def test_unsafe_paths_and_duplicate_json(self):
        for p in ("../x", "/tmp/x", "a/../b", "a//b", "./a", "a\\b"):
            with self.subTest(path=p), self.assertRaises(EvidenceError):
                relative(p)
        with self.assertRaises(EvidenceError):
            decode(b'{"x":1,"x":2}')

    def test_symlink_component_is_not_followed(self):
        (self.root / "link").symlink_to(self.root / self.bid, target_is_directory=True)
        with self.assertRaises(OSError):
            read_bytes(self.root, "link/ingest_manifest.json")

    def test_runtime_commit_and_freshness(self):
        snapshot = {"generated_at": datetime.now(timezone.utc).isoformat(), "batches": [{**self.m["batch"], "revision": 1}]}
        runtime_check(snapshot, self.m)
        snapshot["batches"][0]["state"] = "FINALIZING"
        with self.assertRaises(EvidenceError):
            runtime_check(snapshot, self.m)
        snapshot["batches"][0]["state"] = "COMPLETED"
        snapshot["generated_at"] = (datetime.now(timezone.utc) - timedelta(minutes=3)).isoformat()
        with self.assertRaises(EvidenceError):
            runtime_check(snapshot, self.m)

    def test_resolve_previous_metadata_without_copying(self):
        other = fixture(self.root, "BATCH_20260928_000002", "uid2")
        f = other["files"][0]
        f.update(copy_status="skipped_existing", metadata=None, destination_relative_path=None, existing_copy={"batch_uid": "uid1", "batch_id": self.bid, "manifest_id": "uid1-r1", "staging_relative_path": f"{self.bid}/SOURCE_DATA/A.ARW"})
        f["hash"].update(destination=None, existing_destination=f["hash"]["source"])
        other["summary"].update(verified_file_count=0, previously_ingested_count=1)
        write(self.root, other)
        other = validate_record(self.root, other["batch"]["batch_id"])
        result = resolve_files(self.root, other, {self.bid: self.m})
        self.assertEqual(result[0]["metadata"], self.m["files"][0]["metadata"])
        with self.assertRaises(EvidenceError):
            resolve_files(self.root, other, {})


if __name__ == "__main__":
    unittest.main()
