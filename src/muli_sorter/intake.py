"""Validate bounded completion records without opening media for content reads."""
from __future__ import annotations

import json
from copy import deepcopy
import os
from pathlib import Path, PurePosixPath
import re
import stat
from datetime import datetime, timezone
from blake3 import blake3
from .postcopy_receipt import PostcopyError, validate as validate_postcopy
from .time_correction_receipt import ReceiptError, validate_receipt

BATCH = re.compile(r"BATCH_\d{8}_\d{6,}\Z")
HEX = re.compile(r"[0-9a-f]{64}\Z")
MAX_RECORD_BYTES = 64 * 1024 * 1024


class EvidenceError(ValueError):
    pass


def relative(value):
    if not isinstance(value, str) or not value or "\\" in value or "\0" in value:
        raise EvidenceError("非法相对路径")
    p = PurePosixPath(value)
    if p.is_absolute() or any(x in ("", ".", "..") for x in value.split("/")):
        raise EvidenceError("拒绝越界或非规范路径")
    return p.parts


def _open(root: Path, path: str):
    parts = relative(path)
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = nxt
        return os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    finally:
        os.close(directory)


def read_bytes(root: Path, path: str) -> bytes:
    fd = _open(root, path)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_RECORD_BYTES:
            raise EvidenceError("清单不是普通文件或超过大小限制")
        chunks, total = [], 0
        while chunk := os.read(fd, min(1024 * 1024, MAX_RECORD_BYTES + 1 - total)):
            chunks.append(chunk)
            total += len(chunk)
            if total > MAX_RECORD_BYTES:
                raise EvidenceError("清单超过大小限制")
        after = os.fstat(fd)
        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise EvidenceError("读取期间清单发生变化")
        return b"".join(chunks)
    finally:
        os.close(fd)


def _unique(pairs):
    result = {}
    for k, v in pairs:
        if k in result:
            raise EvidenceError("JSON 存在重复字段")
        result[k] = v
    return result


def decode(data):
    result = json.loads(data, object_pairs_hook=_unique)
    if not isinstance(result, dict):
        raise EvidenceError("JSON 顶层必须是对象")
    return result


def media_stat(root: Path, path: str, size: int):
    fd = _open(root, path)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size != size:
            raise EvidenceError("素材大小或类型与清单不一致")
    finally:
        os.close(fd)


def validate_record(root: Path, batch_id: str, *, allow_examples=False) -> dict:
    if not BATCH.fullmatch(batch_id):
        raise EvidenceError("非法批次目录名")
    receipt_raw = read_bytes(root, f"{batch_id}/ingest_complete.json")
    receipt = decode(receipt_raw)
    raw = read_bytes(root, f"{batch_id}/ingest_manifest.json")
    markdown = read_bytes(root, f"{batch_id}/ingest_manifest.md")
    m = decode(raw)
    if receipt.get("algorithm") != "blake3" or receipt.get("json_blake3") != blake3(raw).hexdigest() or receipt.get("md_blake3") != blake3(markdown).hexdigest():
        raise EvidenceError("完成回执与清单摘要不一致")
    if m.get("schema_version") != "1.1":
        raise EvidenceError("清单版本尚未支持")
    if type(m.get("example_data")) is not bool or receipt.get("example_data") is not m["example_data"]:
        raise EvidenceError("演示标识不一致")
    if m["example_data"] and not allow_examples:
        raise EvidenceError("生产读取不接受合成演示回执")
    b = m["batch"]
    revision = m.get("revision")
    if type(revision) is not int or revision < 1:
        raise EvidenceError("无效清单版本号")
    expected = f"{b['batch_uid']}-r{revision}"
    if b.get("batch_id") != batch_id or receipt.get("batch_uid") != b["batch_uid"] or receipt.get("revision") != revision or receipt.get("manifest_id") != expected or m.get("manifest_id") != expected:
        raise EvidenceError("批次身份或清单版本不一致")
    result = b.get("result")
    if b.get("state") != "COMPLETED" or result not in ("COPY_VERIFIED", "COPY_SIZE_VERIFIED") or not b.get("completed_at"):
        raise EvidenceError("批次未成功完成复制校验")
    files, summary = m["files"], m["summary"]
    if not isinstance(files, list) or len(files) > 100_000:
        raise EvidenceError("文件清单过大或格式错误")
    if summary.get("pending_file_count") != 0 or summary.get("failed_file_count") != 0 or summary.get("selected_file_count") != len(files):
        raise EvidenceError("批次文件计数未闭合")
    if m.get("scan_errors"):
        raise EvidenceError("批次包含扫描错误")
    seen, ids, total = set(), set(), 0
    verified = size_verified = skipped = 0
    for f in files:
        path = f["relative_path"]
        relative(path)
        if path in seen or not f.get("file_id") or f["file_id"] in ids:
            raise EvidenceError("文件路径或标识重复")
        seen.add(path)
        ids.add(f["file_id"])
        if type(f.get("size_bytes")) is not int or f["size_bytes"] < 0:
            raise EvidenceError("文件大小无效")
        total += f["size_bytes"]
        h = f["hash"]
        if (h.get("algorithm") != "blake3" or not isinstance(h.get("source"), str) or
                not HEX.fullmatch(h["source"]) or not h.get("readback_verified_at") or
                f.get("error") or
                (result == "COPY_VERIFIED" and f.get("hash_match") is not True) or
                (result == "COPY_SIZE_VERIFIED" and f.get("hash_match") is not None)):
            raise EvidenceError("文件缺少成功校验证据")
        if result == "COPY_VERIFIED" and f["copy_status"] == "verified":
            verified += 1
            if h.get("destination") != h["source"] or f.get("destination_relative_path") != "SOURCE_DATA/" + path or f.get("existing_copy"):
                raise EvidenceError("文件目标路径或校验不一致")
        elif result == "COPY_SIZE_VERIFIED" and f["copy_status"] == "size_verified":
            size_verified += 1
            if (h.get("destination") is not None or h.get("existing_destination") is not None or
                    f.get("destination_relative_path") != "SOURCE_DATA/" + path or
                    f.get("hash_match") is not None or f.get("existing_copy") or f.get("error")):
                raise EvidenceError("size-only 文件目标或校验字段不一致")
        elif f["copy_status"] == "skipped_existing":
            skipped += 1
            if h.get("existing_destination") != h["source"] or not isinstance(f.get("existing_copy"), dict):
                raise EvidenceError("跨批引用缺少校验证据")
            relative(f["existing_copy"]["staging_relative_path"])
        else:
            raise EvidenceError("清单包含未完成文件")
    if (summary.get("selected_bytes") != total or summary.get("verified_file_count") != verified or
            summary.get("previously_ingested_count") != skipped or
            (result == "COPY_SIZE_VERIFIED" and summary.get("size_verified_file_count") != size_verified)):
        raise EvidenceError("清单汇总与逐文件状态不一致")
    # Validate the immutable ingest record first, then attach a private,
    # read-only amendment map.  The original files remain untouched.
    try:
        amendment = validate_receipt(root, batch_id, m, raw)
    except ReceiptError as exc:
        raise EvidenceError(str(exc)) from exc
    if amendment is not None:
        m["_amendment"] = amendment
    if result == "COPY_SIZE_VERIFIED":
        try:
            m["_postcopy_evidence"] = validate_postcopy(root, batch_id, m, raw, receipt_raw)
        except PostcopyError as exc:
            raise EvidenceError(str(exc)) from exc
    return m


def runtime_check(snapshot: dict | None, manifest: dict, now=None):
    """Snapshot is temporary read-only evidence, never authorizes media writes."""
    if not snapshot:
        raise EvidenceError("缺少独立运行状态快照，仅凭文件回执不能确认最终提交")
    try:
        timestamp = datetime.fromisoformat(snapshot["generated_at"])
        if timestamp.tzinfo is None:
            raise ValueError()
        age = ((now or datetime.now(timezone.utc)) - timestamp).total_seconds()
        if not -5 <= age <= 120:
            raise EvidenceError("运行状态快照已过期，需要重新只读采集")
        b = manifest["batch"]
        states = [s for s in snapshot["batches"] if s.get("batch_uid") == b["batch_uid"]]
        if len(states) != 1:
            raise EvidenceError("运行状态中批次不存在或重复")
        s = states[0]
        for key in ("batch_id", "state", "result", "completed_at"):
            if s.get(key) != b.get(key):
                raise EvidenceError("运行状态与已完成清单不一致")
        if s.get("revision") != manifest["revision"]:
            raise EvidenceError("运行状态的清单版本不一致")
    except (KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, EvidenceError):
            raise
        raise EvidenceError("运行状态快照格式错误") from exc


def resolve_files(root: Path, m: dict, completed: dict[str, dict], *, check_media=True):
    resolved = []
    for f in m["files"]:
        original = f
        owner = m
        if f["copy_status"] == "skipped_existing":
            ref = f["existing_copy"]
            owner = completed.get(ref.get("batch_id"))
            if not owner or owner["batch"]["batch_uid"] != ref.get("batch_uid") or owner["manifest_id"] != ref.get("manifest_id"):
                raise EvidenceError("跨批引用的完成证据缺失")
            matches = [p for p in owner["files"] if p["copy_status"] == "verified" and owner["batch"]["batch_id"] + "/" + p["destination_relative_path"] == ref["staging_relative_path"]]
            if len(matches) != 1:
                raise EvidenceError("跨批引用未唯一定位到原文件")
            original = matches[0]
            if original["hash"]["source"] != f["hash"]["source"] or original["size_bytes"] != f["size_bytes"]:
                raise EvidenceError("跨批引用内容证据不一致")
            if owner.get("_amendment"):
                raise EvidenceError("校正批次不能作为跨批重复引用来源")
        path = owner["batch"]["batch_id"] + "/" + original["destination_relative_path"]
        if check_media:
            media_stat(root, path, f["size_bytes"])
        item = {**f, "metadata": deepcopy(original.get("metadata")), "resolved_path": path,
                "resolved_source_id": owner["batch"].get("source_id")}
        amendment = owner.get("_amendment") or {}
        corrected = amendment.get("files", {}).get(original.get("file_id"))
        if corrected is not None:
            capture = deepcopy((item.get("metadata") or {}).get("capture_time") or {})
            corrected_time = corrected["corrected_capture_time"]
            capture["normalized"] = corrected_time
            capture["wall_time"] = corrected_time[:-6] if corrected_time.endswith("+08:00") else corrected_time
            capture["confidence"] = "operator_estimated"
            item["metadata"] = {**(item.get("metadata") or {}), "capture_time": capture}
            item["hash"] = {**item["hash"], "source": corrected["corrected_blake3"],
                             "destination": corrected["corrected_blake3"]}
            item["_amendment"] = {"correction_id": amendment["correction_id"],
                                   "status": amendment["status"],
                                   "original_blake3": corrected["original_blake3"],
                                   "corrected_blake3": corrected["corrected_blake3"],
                                   "original_capture_time": corrected["original_capture_time"],
                                   "corrected_capture_time": corrected_time}
            item["correction"] = {"correction_id": amendment["correction_id"],
                                   "original_manifest_blake3": amendment["original_manifest_blake3"]}
        resolved.append(item)
    return resolved
