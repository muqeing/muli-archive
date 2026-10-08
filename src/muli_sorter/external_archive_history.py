"""Read-only projection of user-supplied external archive receipts.

External receipts describe files that were archived outside of this service.
They are evidence only: this module never creates the receipt directory and
never hashes a destination while a history page is being read.
"""
from datetime import datetime
import os
from pathlib import Path, PurePosixPath
import re
import stat

from .archive_io import ArchiveError, directory, open_file, persistent_identity, signature, subdirectory
from .intake import relative
from .order_feed_io import digest, read_json


SCHEMA = "external-archive-receipt/1"
RECEIPT_NAME = re.compile(r"external-([0-9a-f]{64})\.json\Z")
HEX = re.compile(r"[0-9a-f]{64}\Z")
PROJECT_YEAR = re.compile(r"20[0-9]{2}\Z")
PROJECT_MONTH = re.compile(r"(?:0?[1-9]|1[0-2])月\Z")
EXTERNAL_HISTORY = "external-history"
EXTERNAL_WARNING = "既有归档记录无法核对，相关素材仍保留待处理；请刷新归档状态或查看核验报告。"


class ExternalReceiptError(ValueError):
    """A malformed receipt or a destination that no longer matches it."""


def _file_identity(files):
    rows = [(row["source_path"], row["name"], row["size_bytes"], row["blake3"])
            for row in files]
    if not rows or len({row[0] for row in rows}) != len(rows):
        raise ExternalReceiptError("外部归档回执的素材范围为空或重复")
    return sorted(rows)


def _aware_timestamp(value):
    if not isinstance(value, str):
        raise ExternalReceiptError("外部归档回执完成时间无效")
    try:
        timestamp = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ExternalReceiptError("外部归档回执完成时间无效") from exc
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ExternalReceiptError("外部归档回执完成时间必须带时区")


def _project_path(value):
    try:
        parts = relative(value)
    except (TypeError, ValueError) as exc:
        raise ExternalReceiptError("外部归档项目路径无效") from exc
    if (len(parts) != 3 or not PROJECT_YEAR.fullmatch(parts[0])
            or not PROJECT_MONTH.fullmatch(parts[1])):
        raise ExternalReceiptError("外部归档项目必须位于安全的年份/月/项目目录")
    return parts


def _validate_signature(value):
    if (not isinstance(value, list) or len(value) != 5
            or any(type(part) is not int or part < 0 for part in value)):
        raise ExternalReceiptError("外部归档目标签名无效")
    return value


def _validate_receipt(receipt, filename, identity, production):
    required = {"schema_version", "receipt_id", "status", "example_data", "roots",
                "completed_at", "unit_id", "project", "files"}
    if not isinstance(receipt, dict) or set(receipt) != required:
        raise ExternalReceiptError("外部归档回执字段不完整")
    match = RECEIPT_NAME.fullmatch(filename)
    if not match or receipt["receipt_id"] != match.group(1):
        raise ExternalReceiptError("外部归档回执文件名与编号不一致")
    unsigned = {key: value for key, value in receipt.items() if key != "receipt_id"}
    if (receipt["schema_version"] != SCHEMA
            or digest(unsigned) != receipt["receipt_id"]
            or receipt["status"] != "completed"
            or type(receipt["example_data"]) is not bool
            or receipt["example_data"] is not (not production)
            or receipt["roots"] != identity):
        raise ExternalReceiptError("外部归档回执身份或摘要不一致")
    _aware_timestamp(receipt["completed_at"])
    if not isinstance(receipt["unit_id"], str) or not receipt["unit_id"]:
        raise ExternalReceiptError("外部归档单元编号无效")

    project = receipt["project"]
    if not isinstance(project, dict) or set(project) != {"name", "path"}:
        raise ExternalReceiptError("外部归档项目字段无效")
    if not isinstance(project["name"], str) or not project["name"]:
        raise ExternalReceiptError("外部归档项目名称无效")
    project_parts = _project_path(project["path"])

    files = receipt["files"]
    row_keys = {"source_path", "name", "size_bytes", "blake3", "target_path", "target_signature"}
    if not isinstance(files, list) or not files:
        raise ExternalReceiptError("外部归档回执没有完整文件集")
    source_paths, target_paths = set(), set()
    for row in files:
        if not isinstance(row, dict) or set(row) != row_keys:
            raise ExternalReceiptError("外部归档文件字段无效")
        try:
            source_parts = relative(row["source_path"])
            name_parts = relative(row["name"])
            target_parts = relative(row["target_path"])
        except (TypeError, ValueError) as exc:
            raise ExternalReceiptError("外部归档文件路径无效") from exc
        if (row["source_path"] in source_paths or row["target_path"] in target_paths
                or PurePosixPath(source_parts[-1]).name != name_parts[-1]):
            raise ExternalReceiptError("外部归档文件名或范围重复")
        if type(row["size_bytes"]) is not int or row["size_bytes"] < 0:
            raise ExternalReceiptError("外部归档文件大小无效")
        if not isinstance(row["blake3"], str) or not HEX.fullmatch(row["blake3"]):
            raise ExternalReceiptError("外部归档文件内容摘要无效")
        target_signature = _validate_signature(row["target_signature"])
        if target_signature[2] != row["size_bytes"]:
            raise ExternalReceiptError("外部归档目标大小与回执不一致")
        if len(target_parts) <= len(project_parts) or tuple(target_parts[:3]) != project_parts:
            raise ExternalReceiptError("外部归档目标不属于回执项目")
        if PurePosixPath(target_parts[-1]).name != name_parts[-1]:
            raise ExternalReceiptError("外部归档目标文件名与原文件不一致")
        source_paths.add(row["source_path"])
        target_paths.add(row["target_path"])
    _file_identity(files)
    return project_parts


def _check_targets(projects_root, expected_root_identity, project_parts, files):
    """Check destination metadata through no-follow directory handles."""
    try:
        with directory(projects_root) as projects_fd:
            if persistent_identity(projects_fd) != expected_root_identity:
                raise ExternalReceiptError("外部归档项目根目录身份已改变")
            with subdirectory(projects_fd, "/".join(project_parts)):
                pass
            for row in files:
                target_parts = relative(row["target_path"])
                parent_parts = target_parts[:-1]
                with subdirectory(projects_fd, "/".join(parent_parts)) as parent_fd:
                    fd = open_file(parent_fd, target_parts[-1])
                    try:
                        if os.fstat(fd).st_nlink != 1:
                            raise ExternalReceiptError("外部归档目标不能是硬链接")
                        if signature(fd) != row["target_signature"]:
                            raise ExternalReceiptError("外部归档目标签名已变化")
                    finally:
                        os.close(fd)
    except (OSError, ArchiveError) as exc:
        raise ExternalReceiptError("外部归档目标缺失或类型不安全") from exc


def _item(receipt, project_parts, label, current):
    identity = _file_identity(receipt["files"])
    unit_id = receipt["unit_id"]
    current_unit = current.get(unit_id)
    matches = current_unit is not None and current_unit == digest(identity)
    item = {
        "unit_id": unit_id,
        "job_id": receipt["receipt_id"],
        "mode": "copy",
        "origin": "existing_project_files",
        "completed_at": receipt["completed_at"],
        "in_current_model": matches,
        "project": {"name": receipt["project"]["name"],
                    "path": label.rstrip("/") + "/" + "/".join(project_parts)},
        "file_count": len(receipt["files"]),
        "files": [{"name": PurePosixPath(row["name"]).name,
                   "target_path": label.rstrip("/") + "/" + row["target_path"],
                   "size_bytes": row["size_bytes"]} for row in receipt["files"]],
    }
    return item


def project_external_history(state, identity, label, production, current, reader=read_json, selected_only=False):
    """Return valid external items and at most one warning.

    The directory is intentionally only inspected when it already exists as a
    real directory. No call here creates state or touches media contents.
    """
    root = Path(state) / EXTERNAL_HISTORY
    try:
        info = root.lstat()
    except FileNotFoundError:
        return [], []
    except OSError:
        return [], [EXTERNAL_WARNING]
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        return [], [EXTERNAL_WARNING]

    items = []
    warning = False
    projects_root = identity.get("projects") if isinstance(identity, dict) else None
    if not isinstance(projects_root, str):
        return [], [EXTERNAL_WARNING]
    try:
        paths = sorted(root.iterdir(), key=lambda p: p.name)
    except OSError:
        return [], [EXTERNAL_WARNING]
    for path in paths:
        try:
            if not path.is_file() or path.is_symlink():
                raise ExternalReceiptError("外部归档回执文件类型无效")
            receipt = reader(path)
            if selected_only and receipt.get('unit_id') not in current:
                continue
            project_parts = _validate_receipt(receipt, path.name, identity, production)
            _check_targets(projects_root, identity["target_identity"], project_parts, receipt["files"])
            item = _item(receipt, project_parts, label, current)
            if item["unit_id"] in current and not item["in_current_model"]:
                warning = True
            items.append(item)
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            warning = True
    # Multiple receipts may describe one unit. Identical destination sets are
    # harmless duplicates; differing project paths or destinations are
    # ambiguous and must not be projected as whichever receipt is newest.
    grouped = {}
    for item in items:
        key = (item["project"]["path"],
               tuple(sorted(file["target_path"] for file in item["files"])))
        grouped.setdefault(item["unit_id"], {}).setdefault(key, []).append(item)
    collapsed = []
    for unit_id, variants in grouped.items():
        if len(variants) > 1:
            warning = True
            continue
        duplicate_items = next(iter(variants.values()))
        collapsed.append(max(duplicate_items,
                            key=lambda item: (item["completed_at"], item["job_id"])))
    collapsed.sort(key=lambda item: (item["completed_at"], item["job_id"]), reverse=True)
    return collapsed, [EXTERNAL_WARNING] if warning else []
