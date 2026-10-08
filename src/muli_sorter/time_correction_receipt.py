"""Read-only validation for in-place media clock correction receipts.

The receipt describes a completed, same-size edit to a verified ingest source.
This module deliberately never opens media content.  It only reads the bounded
receipt and the already validated manifest, and lets callers stat media files.
"""
from __future__ import annotations

import json
import os
import re
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath

from blake3 import blake3


RECEIPT_NAME = "time_correction_receipt.json"
RECEIPT_SCHEMA = "media-clock-correction/1"
MAX_RECEIPT_BYTES = 64 * 1024 * 1024
MAX_FILES = 20_000
HEX = re.compile(r"[0-9a-f]{64}\Z")
BATCH = re.compile(r"BATCH_\d{8}_\d{6,}\Z")
ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
ATOM = {
    "moov/mvhd": "mvhd",
    "moov/trak/tkhd": "tkhd",
    "moov/trak/mdia/mdhd": "mdhd",
}
EPOCH = datetime(1904, 1, 1, tzinfo=timezone.utc)


class ReceiptError(ValueError):
    """Malformed, stale, or unsafe correction receipt."""


def _parts(value: str) -> tuple[str, ...]:
    if not isinstance(value, str) or not value or "\\" in value or "\0" in value:
        raise ReceiptError("非法校正回执路径")
    p = PurePosixPath(value)
    if p.is_absolute() or any(part in ("", ".", "..") for part in value.split("/")):
        raise ReceiptError("校正回执路径越界或不规范")
    return p.parts


def _open(root: Path, path: str) -> int:
    parts = _parts(path)
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = nxt
        return os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    finally:
        os.close(directory)


def receipt_signature(root: Path, batch_id: str):
    """Return a bounded identity tuple, or ``None`` when no receipt exists.

    The tuple is suitable for cache keys and detects both creation and changes.
    """
    if not BATCH.fullmatch(batch_id):
        raise ReceiptError("非法批次目录名")
    try:
        fd = _open(root, f"{batch_id}/{RECEIPT_NAME}")
    except FileNotFoundError:
        return None
    try:
        s = os.fstat(fd)
        if not stat.S_ISREG(s.st_mode) or s.st_size > MAX_RECEIPT_BYTES:
            raise ReceiptError("校正回执不是普通文件或超过大小限制")
        return (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
    finally:
        os.close(fd)


def read_receipt(root: Path, batch_id: str) -> dict | None:
    """Read an optional receipt with no symlink traversal."""
    signature = receipt_signature(root, batch_id)
    if signature is None:
        return None
    fd = _open(root, f"{batch_id}/{RECEIPT_NAME}")
    try:
        before = os.fstat(fd)
        chunks, total = [], 0
        while chunk := os.read(fd, min(1024 * 1024, MAX_RECEIPT_BYTES + 1 - total)):
            chunks.append(chunk)
            total += len(chunk)
            if total > MAX_RECEIPT_BYTES:
                raise ReceiptError("校正回执超过大小限制")
        after = os.fstat(fd)
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns
        ):
            raise ReceiptError("读取期间校正回执发生变化")
    finally:
        os.close(fd)
    try:
        value = json.loads(b"".join(chunks), object_pairs_hook=_unique)
    except (UnicodeDecodeError, json.JSONDecodeError, ReceiptError) as exc:
        raise ReceiptError("校正回执不是合法 JSON") from exc
    if not isinstance(value, dict):
        raise ReceiptError("校正回执顶层必须是对象")
    return value


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ReceiptError("校正回执存在重复字段")
        result[key] = value
    return result


def _timestamp(value, *, local=False, name="时间") -> datetime:
    if not isinstance(value, str):
        raise ReceiptError(f"{name}无效")
    try:
        result = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ReceiptError(f"{name}无效") from exc
    if local and result.utcoffset() != timedelta(hours=8):
        raise ReceiptError(f"{name}必须明确为北京时间")
    if not local and result.tzinfo is not None and result.utcoffset() is None:
        raise ReceiptError(f"{name}无效")
    return result


def _utc_seconds(value: str, *, local=False) -> int:
    dt = _timestamp(value, local=local)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    seconds = (dt.astimezone(timezone.utc) - EPOCH).total_seconds()
    if seconds < 0 or not seconds.is_integer():
        raise ReceiptError("校正时间必须是1904纪元后的整秒")
    return int(seconds)


def _canonical_local(value: str) -> str:
    dt = _timestamp(value, local=True)
    return dt.isoformat()


def _capture(manifest_file: dict) -> str:
    capture = (manifest_file.get("metadata") or {}).get("capture_time") or {}
    value = capture.get("wall_time") or capture.get("normalized")
    if not isinstance(value, str):
        raise ReceiptError("原始清单缺少可校正的拍摄时间")
    return value


def _offset_corrected(anchor_recorded: str, anchor_local: str, original: str) -> str:
    anchor = _timestamp(anchor_recorded)
    local = _timestamp(anchor_local, local=True)
    value = _timestamp(original)
    if anchor.tzinfo is None:
        anchor = anchor.replace(tzinfo=timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    corrected = local + (value.astimezone(timezone.utc) - anchor.astimezone(timezone.utc))
    return corrected.isoformat()


def _amendment_evidence(receipt: dict) -> dict:
    return {
        "schema": RECEIPT_SCHEMA,
        "status": receipt["status"],
        "correction_id": receipt["correction_id"],
        "original_manifest_blake3": receipt["original_manifest_blake3"],
        "files": {
            row["file_id"]: {
                "relative_path": row["relative_path"],
                "original_blake3": row["original_blake3"],
                "corrected_blake3": row["corrected_blake3"],
                "original_capture_time": row["original_capture_time"],
                "corrected_capture_time": row["corrected_capture_time"],
            }
            for row in receipt["files"]
        },
    }


def _validate_receipt_data(batch_id: str, manifest: dict, manifest_raw: bytes, receipt: dict) -> dict:
    """Validate one already decoded receipt and return its amendment map."""
    if not isinstance(receipt, dict):
        raise ReceiptError("校正回执顶层必须是对象")
    required = {
        "schema", "status", "batch_id", "manifest_id", "original_manifest_blake3",
        "correction_id", "anchor_recorded_time", "anchor_local_time", "uncertainty_seconds",
        "no_reset_confirmed", "completed_at", "files",
    }
    if set(receipt) != required or receipt["schema"] != RECEIPT_SCHEMA:
        raise ReceiptError("校正回执字段或 schema 不符")
    if receipt["status"] not in ("prepared", "completed"):
        raise ReceiptError("校正回执状态无效")
    if receipt["batch_id"] != batch_id or receipt["manifest_id"] != manifest.get("manifest_id"):
        raise ReceiptError("校正回执批次或清单身份不符")
    if not isinstance(receipt["original_manifest_blake3"], str) or not HEX.fullmatch(receipt["original_manifest_blake3"]):
        raise ReceiptError("原始清单摘要无效")
    if blake3(manifest_raw).hexdigest() != receipt["original_manifest_blake3"]:
        raise ReceiptError("校正回执引用的原始清单已改变")
    if not isinstance(receipt["correction_id"], str) or not ID.fullmatch(receipt["correction_id"]):
        raise ReceiptError("校正标识无效")
    anchor_recorded = _timestamp(receipt["anchor_recorded_time"], name="参照原始时间")
    anchor_local = _timestamp(receipt["anchor_local_time"], local=True, name="参照本地时间")
    if anchor_recorded.tzinfo is not None and anchor_recorded.utcoffset() is None:
        raise ReceiptError("参照原始时间无效")
    if receipt["no_reset_confirmed"] is not True:
        raise ReceiptError("缺少连续时钟确认")
    if type(receipt["uncertainty_seconds"]) is not int or not 0 <= receipt["uncertainty_seconds"] <= 3600:
        raise ReceiptError("校正误差范围无效")
    completed = receipt["status"] == "completed"
    if completed != (receipt["completed_at"] is not None):
        raise ReceiptError("完成状态与完成时间不一致")
    if receipt["completed_at"] is not None:
        completed_at = _timestamp(receipt["completed_at"], name="完成时间")
        if completed_at.tzinfo is None:
            raise ReceiptError("完成时间必须带时区")
    if receipt["status"] == "prepared":
        # A plan is never an archive authorization.  Reject it before trying
        # to interpret its (normally empty) file list.
        raise ReceiptError("校正回执仍为 prepared，禁止消费或归档")
    files = receipt["files"]
    if not isinstance(files, list) or not files or len(files) > MAX_FILES:
        raise ReceiptError("校正文件数量无效")
    manifest_files = {f.get("file_id"): f for f in manifest.get("files", [])}
    paths, ids = set(), set()
    amendment = {}
    for row in files:
        fields = {
            "file_id", "relative_path", "size_bytes", "original_blake3", "corrected_blake3",
            "original_capture_time", "corrected_capture_time", "patches", "readback_verified_at",
        }
        if not isinstance(row, dict) or set(row) != fields:
            raise ReceiptError("校正文件字段不符")
        file_id, path = row["file_id"], row["relative_path"]
        if not isinstance(file_id, str) or not file_id or file_id in ids:
            raise ReceiptError("校正文件标识重复或无效")
        _parts(path)
        if path in paths or PurePosixPath(path).suffix.upper() not in (".MP4", ".LRF"):
            raise ReceiptError("校正文件路径重复或类型不支持")
        ids.add(file_id); paths.add(path)
        original = manifest_files.get(file_id)
        if (original is None or original.get("relative_path") != path or original.get("copy_status") != "verified"
                or original.get("destination_relative_path") != "SOURCE_DATA/" + path):
            raise ReceiptError("校正文件不是本批次已验证文件")
        if type(row["size_bytes"]) is not int or row["size_bytes"] <= 0 or row["size_bytes"] != original.get("size_bytes"):
            raise ReceiptError("校正文件大小不符")
        old_hash = (original.get("hash") or {}).get("source")
        if (not isinstance(old_hash, str) or not HEX.fullmatch(old_hash)
                or row["original_blake3"] != old_hash
                or (original.get("hash") or {}).get("destination") != old_hash):
            raise ReceiptError("校正文件原始摘要不符")
        if not isinstance(row["corrected_blake3"], str) or not HEX.fullmatch(row["corrected_blake3"]) or row["corrected_blake3"] == old_hash:
            raise ReceiptError("校正后摘要无效或未发生变化")
        original_time = _capture(original)
        if row["original_capture_time"] != original_time:
            raise ReceiptError("校正文件原始拍摄时间不符")
        expected_time = _offset_corrected(receipt["anchor_recorded_time"], receipt["anchor_local_time"], original_time)
        if _canonical_local(row["corrected_capture_time"]) != expected_time:
            raise ReceiptError("校正后的拍摄时间不符合固定偏移")
        if not isinstance(row["readback_verified_at"], str):
            raise ReceiptError("校正文件缺少回读时间")
        readback_at = _timestamp(row["readback_verified_at"], name="文件回读时间")
        if readback_at.tzinfo is None:
            raise ReceiptError("文件回读时间必须带时区")
        patches = row["patches"]
        if not isinstance(patches, list) or not patches or len(patches) > 10_000:
            raise ReceiptError("校正补丁为空")
        ranges = []
        kinds = set()
        for patch in patches:
            if not isinstance(patch, dict) or set(patch) != {"atom", "offset", "before_hex", "after_hex"}:
                raise ReceiptError("校正补丁字段不符")
            atom = patch["atom"]
            if atom not in ATOM:
                raise ReceiptError("校正补丁 atom 路径无效")
            if type(patch["offset"]) is not int or patch["offset"] < 0:
                raise ReceiptError("校正补丁偏移无效")
            before, after = patch["before_hex"], patch["after_hex"]
            if (not isinstance(before, str) or not isinstance(after, str) or len(before) != len(after)
                    or len(before) not in (16, 32) or not re.fullmatch(r"[0-9a-f]+", before)
                    or not re.fullmatch(r"[0-9a-f]+", after)):
                raise ReceiptError("校正补丁字节串无效")
            raw_before, raw_after = bytes.fromhex(before), bytes.fromhex(after)
            width = len(raw_before) // 2
            if raw_before == raw_after:
                raise ReceiptError("校正补丁未改变时间")
            old_seconds, new_seconds = _utc_seconds(original_time), _utc_seconds(row["corrected_capture_time"], local=True)
            try:
                expected_before = old_seconds.to_bytes(width, "big") * 2
                expected_after = new_seconds.to_bytes(width, "big") * 2
            except OverflowError as exc:
                raise ReceiptError("校正时间超出 atom 字段宽度") from exc
            if raw_before != expected_before or raw_after != expected_after:
                raise ReceiptError("校正补丁不是原始/校正时间的 UTC 映射")
            end = patch["offset"] + len(raw_before)
            if end > row["size_bytes"]:
                raise ReceiptError("校正补丁越过文件边界")
            ranges.append((patch["offset"], end))
            kinds.add(ATOM[atom])
        ranges.sort()
        if any(end > start for (_, end), (start, _) in zip(ranges, ranges[1:])):
            raise ReceiptError("校正补丁发生重叠")
        if (kinds != {"mvhd", "tkhd", "mdhd"} or
                sum(ATOM[p["atom"]] == "mvhd" for p in patches) != 1 or
                sum(ATOM[p["atom"]] == "tkhd" for p in patches) != sum(ATOM[p["atom"]] == "mdhd" for p in patches)):
            raise ReceiptError("校正补丁缺少 mvhd/tkhd/mdhd 时间字段")
        amendment[file_id] = dict(row)
    return {
        "schema": RECEIPT_SCHEMA,
        "status": receipt["status"],
        "correction_id": receipt["correction_id"],
        "original_manifest_blake3": receipt["original_manifest_blake3"],
        "anchor_recorded_time": receipt["anchor_recorded_time"],
        "anchor_local_time": _canonical_local(receipt["anchor_local_time"]),
        "uncertainty_seconds": receipt["uncertainty_seconds"],
        "files": amendment,
        "evidence": _amendment_evidence(receipt),
    }


def validate_receipt(root: Path, batch_id: str, manifest: dict, manifest_raw: bytes) -> dict | None:
    """Validate an optional on-disk receipt without mutating the manifest."""
    receipt = read_receipt(root, batch_id)
    if receipt is None:
        return None
    return _validate_receipt_data(batch_id, manifest, manifest_raw, receipt)


def validate_correction(original_manifest_dict: dict, original_manifest_raw_bytes: bytes, receipt_dict: dict) -> dict:
    """Public pure validator used by the correction writer and focused tests."""
    batch_id = ((original_manifest_dict.get("batch") or {}).get("batch_id"))
    if not isinstance(batch_id, str):
        raise ReceiptError("原始清单缺少批次身份")
    return _validate_receipt_data(batch_id, original_manifest_dict, original_manifest_raw_bytes, receipt_dict)


def amendment_evidence(manifest: dict):
    amendment = manifest.get("_amendment")
    return amendment.get("evidence") if isinstance(amendment, dict) else None
