import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from blake3 import blake3

from test_intake import fixture
from test_archive_jobs import Fixture as ArchiveFixture
from muli_sorter.archive_jobs import ArchiveJobs
from muli_sorter.archive_io import ArchiveError
from muli_sorter.archive_source import verify_sources
from muli_sorter.intake import EvidenceError, resolve_files, validate_record
from muli_sorter.matching import group_files, project_index
from muli_sorter.queue_evidence import signal_parts
from muli_sorter.review import build_review_model
from muli_sorter.time_correction_receipt import validate_correction


def seconds(value):
    epoch = datetime(1904, 1, 1, tzinfo=timezone.utc)
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int((dt.astimezone(timezone.utc) - epoch).total_seconds())


def receipt_for_manifest(root, batch_id, manifest, names, *, corrected_hashes=None, correction_id="clock-1"):
    first = next(f for f in manifest["files"] if f["relative_path"] == names[0])
    capture = (first.get("metadata") or {}).get("capture_time", {})
    old = capture.get("wall_time") or capture.get("normalized")
    anchor_local = ("2026-09-30T08:00:00+08:00" if old == "2026-01-01T00:00:00"
                    else "2026-07-10T12:00:00+08:00")
    raw = (Path(root) / batch_id / "ingest_manifest.json").read_bytes()
    now = "2026-10-01T00:00:00+00:00"
    rows = []
    for name in names:
        original = next(f for f in manifest["files"] if f["relative_path"] == name)
        capture = (original.get("metadata") or {}).get("capture_time", {})
        original_time = capture.get("wall_time") or capture.get("normalized")
        corrected = datetime.fromisoformat(anchor_local) + (datetime.fromisoformat(original_time) - datetime.fromisoformat(old))
        corrected_time = corrected.isoformat()
        before, after = seconds(original_time), seconds(corrected_time)
        pair = lambda value: (value.to_bytes(4, "big") * 2).hex()
        patches = [{"atom": atom, "offset": offset, "before_hex": pair(before),
                    "after_hex": pair(after)} for atom, offset in (
                        ("moov/mvhd", 10), ("moov/trak/tkhd", 30),
                        ("moov/trak/mdia/mdhd", 50))]
        rows.append({"file_id": original["file_id"], "relative_path": name,
                     "size_bytes": original["size_bytes"],
                     "original_blake3": original["hash"]["source"],
                     "corrected_blake3": (corrected_hashes or {}).get(name, "b" * 64),
                     "original_capture_time": original_time,
                     "corrected_capture_time": corrected_time, "patches": patches,
                     "readback_verified_at": now})
    return {"schema": "media-clock-correction/1", "status": "completed",
            "batch_id": batch_id, "manifest_id": manifest["manifest_id"],
            "original_manifest_blake3": blake3(raw).hexdigest(),
            "correction_id": correction_id, "anchor_recorded_time": old,
            "anchor_local_time": anchor_local, "uncertainty_seconds": 3600,
            "no_reset_confirmed": True, "completed_at": now, "files": rows}


class TimeCorrectionReceiptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.manifest = fixture(self.root)
        self.batch_id = self.manifest["batch"]["batch_id"]
        folder = self.root / self.batch_id
        source = folder / "SOURCE_DATA/DJI_0001.MP4"
        source.write_bytes(b"x" * 1000)
        f = self.manifest["files"][0]
        old_hash = blake3(source.read_bytes()).hexdigest()
        f.update(relative_path="DJI_0001.MP4", filename="DJI_0001.MP4", size_bytes=1000,
                 destination_relative_path="SOURCE_DATA/DJI_0001.MP4")
        f["hash"].update(source=old_hash, destination=old_hash)
        f["metadata"] = {"capture_time": {"wall_time": "2026-01-01T00:00:00",
                                            "normalized": "2026-01-01T00:00:00"}}
        self.manifest["summary"].update(selected_bytes=1000)
        self._write_manifest()

    def _write_manifest(self):
        folder = self.root / self.batch_id
        raw = json.dumps(self.manifest, separators=(",", ":")).encode()
        md = b"# report\n" + raw
        (folder / "ingest_manifest.json").write_bytes(raw)
        (folder / "ingest_manifest.md").write_bytes(md)
        (folder / "ingest_complete.json").write_text(json.dumps({
            "algorithm": "blake3", "batch_uid": self.manifest["batch"]["batch_uid"],
            "manifest_id": self.manifest["manifest_id"], "revision": 1,
            "example_data": self.manifest["example_data"],
            "json_blake3": blake3(raw).hexdigest(), "md_blake3": blake3(md).hexdigest()}))

    def receipt(self, *, status="completed", names=None):
        names = names or ["DJI_0001.MP4"]
        first = next(f for f in self.manifest["files"] if f["relative_path"] == names[0])
        old = (first.get("metadata") or {}).get("capture_time", {}).get("wall_time") or \
              (first.get("metadata") or {}).get("capture_time", {}).get("normalized")
        anchor_local = "2026-09-30T08:00:00+08:00" if old == "2026-01-01T00:00:00" else "2026-07-10T12:00:00+08:00"
        now = "2026-10-01T00:00:00+00:00"
        raw = (self.root / self.batch_id / "ingest_manifest.json").read_bytes()
        rows = []
        for name in names:
            original = next(f for f in self.manifest["files"] if f["relative_path"] == name)
            original_time = (original.get("metadata") or {}).get("capture_time", {}).get("wall_time") or \
                             (original.get("metadata") or {}).get("capture_time", {}).get("normalized")
            from datetime import datetime
            corrected = datetime.fromisoformat(anchor_local) + (datetime.fromisoformat(original_time) - datetime.fromisoformat(old))
            corrected_time = corrected.isoformat()
            old_seconds, new_seconds = seconds(original_time), seconds(corrected_time)
            pair = lambda value: (value.to_bytes(4, "big") * 2).hex()
            patches = [{"atom": atom, "offset": offset, "before_hex": pair(old_seconds),
                        "after_hex": pair(new_seconds)} for atom, offset in (
                            ("moov/mvhd", 10), ("moov/trak/tkhd", 30),
                            ("moov/trak/mdia/mdhd", 50))]
            rows.append({"file_id": original["file_id"], "relative_path": name,
                         "size_bytes": original["size_bytes"],
                         "original_blake3": original["hash"]["source"],
                         "corrected_blake3": "b" * 64, "original_capture_time": original_time,
                         "corrected_capture_time": corrected_time, "patches": patches,
                         "readback_verified_at": now})
        return {"schema": "media-clock-correction/1", "status": status,
                "batch_id": self.batch_id, "manifest_id": self.manifest["manifest_id"],
                "original_manifest_blake3": blake3(raw).hexdigest(),
                "correction_id": "clock-1", "anchor_recorded_time": old,
                "anchor_local_time": anchor_local, "uncertainty_seconds": 3600,
                "no_reset_confirmed": True, "completed_at": None if status == "prepared" else now,
                "files": [] if status == "prepared" else rows}

    def install(self, receipt):
        (self.root / self.batch_id / "time_correction_receipt.json").write_text(json.dumps(receipt))

    def test_completed_receipt_overlays_without_mutating_original(self):
        receipt = self.receipt()
        self.install(receipt)
        manifest = validate_record(self.root, self.batch_id, allow_examples=True)
        result = resolve_files(self.root, manifest, {self.batch_id: manifest}, check_media=False)
        self.assertEqual(result[0]["hash"]["source"], "b" * 64)
        self.assertEqual(result[0]["metadata"]["capture_time"]["normalized"], "2026-09-30T08:00:00+08:00")
        self.assertEqual(result[0]["metadata"]["capture_time"]["confidence"], "operator_estimated")
        self.assertEqual(manifest["files"][0]["hash"]["source"], receipt["files"][0]["original_blake3"])
        self.assertEqual(validate_correction(self.manifest,
                                              (self.root / self.batch_id / "ingest_manifest.json").read_bytes(), receipt)["correction_id"], "clock-1")

    def test_prepared_receipt_is_rejected(self):
        self.install(self.receipt(status="prepared"))
        with self.assertRaises(EvidenceError):
            validate_record(self.root, self.batch_id, allow_examples=True)

    def test_changed_receipt_changes_queue_signal(self):
        snapshot = {"batches": [{**self.manifest["batch"], "revision": self.manifest["revision"]}]}
        before = signal_parts(self.root, [self.batch_id], snapshot)
        self.install(self.receipt())
        after = signal_parts(self.root, [self.batch_id], snapshot)
        self.assertNotEqual(before, after)

    def test_tampered_patch_or_base_digest_is_rejected(self):
        raw = (self.root / self.batch_id / "ingest_manifest.json").read_bytes()
        mutations = {
            "fileid": lambda r: r["files"][0].update(file_id="missing"),
            "path": lambda r: r["files"][0].update(relative_path="DJI_0001.LRF"),
            "old hash": lambda r: r["files"][0].update(original_blake3="0" * 64),
            "offset": lambda r: r["files"][0]["patches"][0].update(offset=1000),
            "overlap": lambda r: r["files"][0]["patches"][1].update(offset=10),
            "duplicate": lambda r: r["files"].append(dict(r["files"][0])),
            "naive completion": lambda r: r.update(completed_at="2026-10-01T00:00:00"),
            "base hash": lambda r: r.update(original_manifest_blake3="0" * 64),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                receipt = self.receipt()
                mutate(receipt)
                with self.assertRaises(ValueError):
                    validate_correction(self.manifest, raw, receipt)

    def test_symlink_receipt_is_rejected(self):
        receipt = self.root / self.batch_id / "time_correction_receipt.json"
        target = self.root / "receipt-target.json"
        target.write_text(json.dumps(self.receipt()))
        receipt.symlink_to(target)
        with self.assertRaises(OSError):
            validate_record(self.root, self.batch_id, allow_examples=True)


class ArchiveCorrectionReceiptTests(ArchiveFixture, unittest.TestCase):
    """Exercise corrected hashes through the real synthetic archive job path."""

    def install_corrected_archive(self, names=("DCIM/B.MP4", "DCIM/B.LRF")):
        batch = self.root / "staging/BATCH_20260928_000001"
        manifest = json.loads((batch / "ingest_manifest.json").read_text())
        changed_hashes = {}
        for name in names:
            source = batch / "SOURCE_DATA" / name
            content = source.read_bytes()
            changed = bytes([content[0] ^ 1]) + content[1:]
            source.write_bytes(changed)
            changed_hashes[name] = blake3(changed).hexdigest()
        receipt = receipt_for_manifest(self.root / "staging", "BATCH_20260928_000001",
                                       manifest, list(names), corrected_hashes=changed_hashes)
        (batch / "time_correction_receipt.json").write_text(json.dumps(receipt))
        bid = manifest["batch"]["batch_id"]
        validated = validate_record(self.root / "staging", bid, allow_examples=True)
        projects = project_index(self.root / "projects")
        groups = group_files(resolve_files(self.root / "staging", validated, {bid: validated}),
                             projects, [], manifest["manifest_id"])
        self.model = build_review_model({"mode": "read_only_preview", "example_data": True,
                                         "generated_at": datetime.now(timezone.utc).isoformat(),
                                         "projects": projects,
                                         "batches": [{"batch_id": bid, "status": "verified",
                                                      "manifest_id": manifest["manifest_id"],
                                                      "groups": groups}]})
        self.decisions = {"schema_version": "0.2", "mode": "classification_confirmation_only",
                          "media_write_authorized": False, "report_id": self.model["report_id"],
                          "created_at": datetime.now(timezone.utc).isoformat(), "segments": []}
        units = {u["unit_id"]: u for u in self.model["units"]}
        for segment in self.model["initial_segments"]:
            unit = units[segment["unit_ids"][0]]
            confirmed = unit["capture_date"] in ("2026-07-09", "2026-07-10") and bool(unit["candidate_project_ids"])
            self.decisions["segments"].append({**segment,
                "decision": "confirmed" if confirmed else "pending",
                "project_id": unit["candidate_project_ids"][0] if confirmed else None})
        return receipt

    def test_corrected_hashes_copy_through_preflight_submit_and_run(self):
        self.install_corrected_archive()
        sources = {p: p.read_bytes() for p in (self.root / "staging").rglob("SOURCE_DATA/*") if p.is_file()}
        preview = self.service.preflight(self.decisions)
        self.assertEqual(preview["status"], "ready", preview)
        job = self.service.submit(preview["preview_id"], self.decisions, True)
        done = self.service.run_job(job["job_id"])
        self.assertEqual(done["status"], "completed", done)
        self.assertEqual(done["summary"]["completed_files"], 4)
        self.assertEqual(sources, {p: p.read_bytes() for p in sources})

    def test_old_confirmation_is_rejected_after_correction_changes_identity(self):
        preview = self.service.preflight(self.decisions)
        self.assertEqual(preview["status"], "ready", preview)
        old_model = json.loads(json.dumps(self.model))
        old_decisions = json.loads(json.dumps(self.decisions))
        self.install_corrected_archive()
        self.model = old_model
        job=self.service.submit(preview["preview_id"], old_decisions, True)
        done=self.service.run_job(job['job_id'])
        self.assertEqual(done['status'],'failed',done)
        self.assertEqual(done['summary']['completed_files'],0)

    def test_cache_does_not_reuse_prepared_or_changed_receipt(self):
        receipt = self.install_corrected_archive()
        unit = next(u for u in self.model["units"] if u["capture_date"] == "2026-07-10")
        cache = {}
        first = verify_sources(self.root / "staging", unit, self.runtime,
                                cache=cache, check_media=False, reviewed_metadata=True)
        changed = json.loads((self.root / "staging/BATCH_20260928_000001/time_correction_receipt.json").read_text())
        changed["correction_id"] = "clock-2"
        (self.root / "staging/BATCH_20260928_000001/time_correction_receipt.json").write_text(json.dumps(changed))
        second = verify_sources(self.root / "staging", unit, self.runtime,
                                cache=cache, check_media=False, reviewed_metadata=True)
        self.assertNotEqual(first, second)
        changed["status"] = "prepared"; changed["completed_at"] = None; changed["files"] = []
        (self.root / "staging/BATCH_20260928_000001/time_correction_receipt.json").write_text(json.dumps(changed))
        with self.assertRaises(ValueError):
            verify_sources(self.root / "staging", unit, self.runtime, cache=cache, check_media=False,
                           reviewed_metadata=True)

    def test_move_receipt_change_before_cleanup_preserves_sources(self):
        self.install_corrected_archive()
        self.decisions["archive_options"] = {"mode": "move", "existing": "error"}
        self.service.move_enabled = True
        for segment in self.decisions["segments"]:
            if not segment["label"].startswith("2026-07-10"):
                segment.update(decision="pending", project_id=None)
        preview = self.service.preflight(self.decisions)
        self.assertEqual(preview["status"], "ready", preview)
        receipt_path = self.root / "staging/BATCH_20260928_000001/time_correction_receipt.json"
        mutated = [False]
        def checkpoint(phase, row):
            if phase == "receipt" and not mutated[0]:
                changed = json.loads(receipt_path.read_text()); changed["correction_id"] = "clock-before-move"
                receipt_path.write_text(json.dumps(changed)); mutated[0] = True
        self.service.checkpoint = checkpoint
        job = self.service.submit(preview["preview_id"], self.decisions, True)
        done = self.service.run_job(job["job_id"])
        self.assertEqual(done["status"], "failed", done)
        self.assertTrue(mutated[0])
        self.assertTrue(any((self.root / "staging").joinpath(p).exists()
                            for p in ("BATCH_20260928_000001/SOURCE_DATA/DCIM/B.MP4",
                                      "BATCH_20260928_000001/SOURCE_DATA/DCIM/B.LRF")))

    def test_move_receipt_change_during_cleanup_preserves_sources(self):
        self.install_corrected_archive()
        self.decisions["archive_options"] = {"mode": "move", "existing": "error"}
        self.service.move_enabled = True
        mutated = [False]
        receipt_path = self.root / "staging/BATCH_20260928_000001/time_correction_receipt.json"
        def checkpoint(phase, row):
            if phase == "move_intent" and not mutated[0]:
                changed = json.loads(receipt_path.read_text()); changed["correction_id"] = "clock-during-move"
                receipt_path.write_text(json.dumps(changed)); mutated[0] = True
        self.service.checkpoint = checkpoint
        job = self.service.submit(self.service.preflight(self.decisions)["preview_id"], self.decisions, True)
        done = self.service.run_job(job["job_id"])
        self.assertEqual(done["status"], "failed", done)
        self.assertTrue(mutated[0])
        self.assertTrue((self.root / "staging/BATCH_20260928_000001/SOURCE_DATA/DCIM/B.MP4").exists())


if __name__ == "__main__":
    unittest.main()
