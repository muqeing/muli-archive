"""Portable synthetic content-proof fixtures; all files live in a test tempdir."""
from __future__ import annotations
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
from blake3 import blake3
from muli_sorter.archive_fixture import prepare, save_manifest
from muli_sorter.intake import resolve_files, validate_record
from muli_sorter.matching import group_files, project_index
from muli_sorter.review import build_review_model

BATCH_ID = "BATCH_20260928_000001"
class BenchmarkError(RuntimeError):
    pass

def _copy_verified_record(base: dict, index: int, digest: str, now: str) -> dict:
    record = deepcopy(base)
    relative_path = f"DCIM/P_{index:06d}.JPG"
    record.update(
        file_id=f"synthetic-{index:06d}",
        relative_path=relative_path,
        filename=Path(relative_path).name,
        destination_relative_path=f"SOURCE_DATA/{relative_path}",
        hash={
            "algorithm": "blake3",
            "source": digest,
            "destination": digest,
            "readback_verified_at": now,
        },
        copy_status="verified",
        hash_match=True,
        existing_copy=None,
        error=None,
    )
    return record


def _size_verified_record(base: dict, index: int, digest: str, now: str) -> dict:
    record = _copy_verified_record(base, index, digest, now)
    record.update(
        copy_status="size_verified",
        hash_match=None,
        existing_copy=None,
        error=None,
    )
    record["hash"]["destination"] = None
    record["hash"]["existing_destination"] = None
    return record


def _write_postcopy_receipt(root: Path, manifest: dict, receipt_root: Path, now: str) -> None:
    """Write the same bounded receipt shape accepted by postcopy_receipt."""
    batch_dir = root / "staging" / BATCH_ID
    manifest_raw = (batch_dir / "ingest_manifest.json").read_bytes()
    ingest_raw = (batch_dir / "ingest_complete.json").read_bytes()
    rows = []
    for item in manifest["files"]:
        source = batch_dir / "SOURCE_DATA" / item["relative_path"]
        st = source.stat()
        rows.append(
            {
                "file_id": item["file_id"],
                "relative_path": item["relative_path"],
                "resolved_path": f"{BATCH_ID}/SOURCE_DATA/{item['relative_path']}",
                "size_bytes": item["size_bytes"],
                "blake3": item["hash"]["source"],
                "source_signature": {
                    "dev": st.st_dev,
                    "ino": st.st_ino,
                    "size": st.st_size,
                    "mtime_ns": st.st_mtime_ns,
                    "ctime_ns": st.st_ctime_ns,
                },
            }
        )
    receipt = {
        "schema": "postcopy-verification/1",
        "status": "completed",
        "verified_at": now,
        "batch_id": BATCH_ID,
        "batch_uid": manifest["batch"]["batch_uid"],
        "revision": manifest["revision"],
        "manifest_id": manifest["manifest_id"],
        "source_id": manifest["batch"]["source_id"],
        "original_manifest_blake3": blake3(manifest_raw).hexdigest(),
        "original_ingest_complete_blake3": blake3(ingest_raw).hexdigest(),
        "files": rows,
    }
    receipt_root.mkdir(mode=0o700)
    (receipt_root / f"{BATCH_ID}.json").write_text(
        json.dumps(receipt, ensure_ascii=False), encoding="utf-8"
    )


def _prepare_fixture(root: Path, unit_count: int, result: str) -> tuple[dict, Path | None]:
    if unit_count < 1:
        raise BenchmarkError("--units 必须是正整数")
    if result not in ("COPY_VERIFIED", "COPY_SIZE_VERIFIED"):
        raise BenchmarkError("不支持的批次结果")

    model, _ = prepare(root)
    batch_dir = root / "staging" / BATCH_ID
    source = batch_dir / "SOURCE_DATA" / "DCIM" / "A.JPG"
    image = source.read_bytes()
    image_digest = blake3(image).hexdigest()
    shutil.rmtree(batch_dir / "SOURCE_DATA")
    (batch_dir / "SOURCE_DATA" / "DCIM").mkdir(parents=True)

    now = datetime.now(timezone.utc).isoformat()
    base = json.loads((batch_dir / "ingest_manifest.json").read_text(encoding="utf-8"))["files"][0]
    records = []
    factory = _copy_verified_record if result == "COPY_VERIFIED" else _size_verified_record
    for index in range(unit_count):
        record = factory(base, index, image_digest, now)
        path = batch_dir / "SOURCE_DATA" / record["relative_path"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(image)
        records.append(record)

    manifest = json.loads((batch_dir / "ingest_manifest.json").read_text(encoding="utf-8"))
    manifest["batch"]["result"] = result
    manifest["batch"]["completed_at"] = now
    manifest["files"] = records
    summary = manifest["summary"]
    summary.update(
        pending_file_count=0,
        failed_file_count=0,
        selected_file_count=unit_count,
        selected_bytes=sum(item["size_bytes"] for item in records),
        verified_file_count=unit_count if result == "COPY_VERIFIED" else 0,
        previously_ingested_count=0,
    )
    if result == "COPY_SIZE_VERIFIED":
        summary.update(
            copied_file_count=unit_count,
            copied_bytes=summary["selected_bytes"],
            size_verified_file_count=unit_count,
            awaiting_hash_file_count=0,
        )
    save_manifest(root, manifest)

    receipt_root = None
    if result == "COPY_SIZE_VERIFIED":
        receipt_root = root / "postcopy-verifications"
        _write_postcopy_receipt(root, manifest, receipt_root, now)

    runtime_path = root / "runtime-state.json"
    runtime_snapshot = json.loads(runtime_path.read_text(encoding="utf-8"))
    runtime_snapshot["generated_at"] = now
    runtime_snapshot["batches"] = [{**manifest["batch"], "revision": manifest["revision"]}]
    runtime_path.write_text(
        json.dumps(runtime_snapshot, ensure_ascii=False), encoding="utf-8"
    )

    # Rebuild only the in-memory review model used by the benchmark.  This is
    # intentionally before the timed section.
    with _postcopy_env(receipt_root):
        validated = validate_record(root / "staging", BATCH_ID, allow_examples=True)
    resolved = resolve_files(root / "staging", validated, {BATCH_ID: validated}, check_media=False)
    groups = group_files(
        resolved,
        project_index(root / "projects"),
        [],
        manifest["manifest_id"],
    )
    report = {
        "mode": "read_only_preview",
        "example_data": True,
        "generated_at": now,
        "projects": project_index(root / "projects"),
        "batches": [
            {
                "batch_id": BATCH_ID,
                "status": "verified",
                "manifest_id": manifest["manifest_id"],
                "groups": groups,
            }
        ],
    }
    built_model = build_review_model(report)
    if len(built_model["units"]) != unit_count:
        raise BenchmarkError(
            f"合成单元数量不符: expected={unit_count}, actual={len(built_model['units'])}"
        )
    return built_model, receipt_root


@contextmanager
def _postcopy_env(receipt_root: Path | None):
    old = os.environ.get("MULI_POSTCOPY_RECEIPTS")
    if receipt_root is None:
        os.environ.pop("MULI_POSTCOPY_RECEIPTS", None)
    else:
        os.environ["MULI_POSTCOPY_RECEIPTS"] = str(receipt_root)
    try:
        yield
    finally:
        if old is None:
            os.environ.pop("MULI_POSTCOPY_RECEIPTS", None)
        else:
            os.environ["MULI_POSTCOPY_RECEIPTS"] = old

