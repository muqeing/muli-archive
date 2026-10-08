"""Preview or explicitly apply confirmed no-order folder creation; never touch media.

An approval binds the decision digest, exact root identity and derived target paths.
Existing leaf folders are reusable only with a matching creation receipt and inode.
This is an operator CLI, not a write endpoint exposed by the offline review page.
"""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import stat
import sys

from .archive_io import DIR, atomic_json, directory, open_file, persistent_identity
from .cli import load_json
from .intake import relative
from .manual_projects import fold
from .review import compile_plan, digest


class FolderError(ValueError):
    pass


def _identity(fd):
    return persistent_identity(fd)


def _check_name(fd, name):
    for index, existing in enumerate(os.listdir(fd)):
        if index >= 10000:
            raise FolderError("目录内容过多，需人工核对")
        if existing != name and fold(existing) == fold(name):
            raise FolderError("目录名称存在大小写或 Unicode 冲突：" + name)
    try:
        info = os.stat(name, dir_fd=fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    if not stat.S_ISDIR(info.st_mode):
        raise FolderError("目标路径包含非目录或软链接：" + name)
    return True


def _inspect(root_fd, path):
    fd = os.dup(root_fd)
    try:
        for part in relative(path):
            if not _check_name(fd, part):
                return None
            nxt = os.open(part, DIR, dir_fd=fd)
            os.close(fd)
            fd = nxt
        return _identity(fd)
    finally:
        os.close(fd)


def _request(model, decisions, root, root_fd):
    compiled = compile_plan(model, decisions)
    targets = {a["project"]["project_id"]: {"project_id": a["project"]["project_id"], "path": a["project"]["path"]}
               for a in compiled["assignments"] if a["project"].get("evidence_source") == "manual_no_order"}
    if not targets:
        raise FolderError("没有已确认归属的无订单待建项目")
    return {"schema_version": "manual-folder-request/0.1", "report_id": model["report_id"],
            "decision_id": compiled["decision_id"], "projects_root": str(root),
            "root_identity": _identity(root_fd), "targets": sorted(targets.values(), key=lambda p: p["path"])}


def _receipt_name(request_id):
    return "manual-folders-" + request_id.split(":", 1)[1] + ".json"


def _read_receipt(fd, request_id, request):
    try:
        receipt_fd = open_file(fd, _receipt_name(request_id))
    except FileNotFoundError:
        return {}
    try:
        if os.fstat(receipt_fd).st_size > 4 * 1024 * 1024:
            raise FolderError("建目录回执过大")
        with os.fdopen(os.dup(receipt_fd), encoding="utf-8") as stream:
            receipt = json.load(stream)
    finally:
        os.close(receipt_fd)
    if receipt.get("request_id") != request_id or receipt.get("request") != request:
        raise FolderError("建目录回执与当前请求不一致")
    expected = {p["path"] for p in request["targets"]}
    created = receipt.get("created")
    if (not isinstance(created, dict) or set(created) - expected or any(
            not isinstance(value, list) or len(value) != 2 or
            any(type(n) is not int or n < 0 for n in value) for value in created.values())):
        raise FolderError("建目录回执格式无效")
    return created


def _inspect_targets(fd, request, created):
    result = []
    for target in request["targets"]:
        row = dict(target)
        row["full_path"] = str(Path(request["projects_root"]) / target["path"])
        try:
            found = _inspect(fd, target["path"])
            if found is None:
                if target["path"] in created:
                    raise FolderError("已创建目录已消失，需核对回执")
                row["state"] = "create_required"
            elif created.get(target["path"]) == found:
                row["state"] = "created_verified"
            else:
                raise FolderError("目标目录已存在且没有对应创建回执，停止自动复用")
        except (OSError, ValueError) as exc:
            row.update(state="blocked", reason=str(exc))
        result.append(row)
    return result


def preview_folders(model, decisions, projects_root, receipts_root=None):
    root = Path(projects_root).absolute()
    with directory(root) as fd:
        request = _request(model, decisions, root, fd)
        request_id = "sha256:" + digest(request)
        created = {}
        if receipts_root is not None:
            with directory(receipts_root) as receipts_fd:
                created = _read_receipt(receipts_fd, request_id, request)
        rows = _inspect_targets(fd, request, created)
    return {"mode": "manual_folder_creation_preview", "request_id": request_id, "request": request,
            "status": "blocked" if any(p["state"] == "blocked" for p in rows) else "ready_for_folder_confirmation",
            "folder_write_authorized": False, "media_write_authorized": False, "targets": rows}


@contextmanager
def _lock(fd):
    lock_fd = open_file(fd, ".manual-project-folders.lock", os.O_RDWR | os.O_CREAT)
    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise FolderError("已有建目录任务正在执行") from exc
        yield
    finally:
        os.close(lock_fd)


def _create(root_fd, path):
    fd = os.dup(root_fd)
    parts = relative(path)
    try:
        for index, part in enumerate(parts):
            found = _check_name(fd, part)
            if index == len(parts) - 1 and found:
                raise FolderError("建目录前目标已被占用：" + path)
            if not found:
                # O_EXCL-equivalent mkdir: a race is an error, never a blind reuse.
                os.mkdir(part, 0o770, dir_fd=fd)
                os.fsync(fd)
            nxt = os.open(part, DIR, dir_fd=fd)
            os.close(fd)
            fd = nxt
        return _identity(fd)
    finally:
        os.close(fd)


def apply_folders(model, decisions, projects_root, receipts_root, approved_request_id, *, checkpoint=None):
    root, receipts = Path(projects_root).absolute(), Path(receipts_root).absolute()
    if receipts == root or receipts.is_relative_to(root):
        raise FolderError("建目录回执需放在独立服务目录，不写入项目目录")
    with directory(root) as fd, directory(receipts) as receipts_fd:
        request = _request(model, decisions, root, fd)
        request_id = "sha256:" + digest(request)
        if not approved_request_id or approved_request_id != request_id:
            raise FolderError("执行前需确认当前建目录请求；路径或计划变化后需重新核对")
        with _lock(receipts_fd):
            created = _read_receipt(receipts_fd, request_id, request)
            rows = _inspect_targets(fd, request, created)
            if any(p["state"] == "blocked" for p in rows):
                raise FolderError("建目录前检查未通过：" + "; ".join(p.get("reason", "") for p in rows if p["state"] == "blocked"))
            receipt = {"schema_version": "manual-folder-receipt/0.1", "request_id": request_id, "request": request,
                       "status": "creating", "created": created, "media_write_authorized": False,
                       "updated_at": datetime.now(timezone.utc).isoformat()}
            atomic_json(receipts_fd, _receipt_name(request_id), receipt)
            try:
                for row in rows:
                    if row["state"] == "created_verified":
                        continue
                    identity = _create(fd, row["path"])
                    if _inspect(fd, row["path"]) != identity:
                        raise FolderError("建目录后回读身份不一致")
                    created[row["path"]] = identity
                    receipt["updated_at"] = datetime.now(timezone.utc).isoformat()
                    atomic_json(receipts_fd, _receipt_name(request_id), receipt)
                    if checkpoint:
                        checkpoint(row["path"])
                rows = _inspect_targets(fd, request, created)
                if any(row["state"] != "created_verified" for row in rows):
                    raise FolderError("最终建目录回读未通过")
                # Check the configured root path still denotes the open handle.
                with directory(root) as fresh_fd:
                    if _identity(fresh_fd) != request["root_identity"]:
                        raise FolderError("执行期间项目根目录发生变化")
                receipt["status"] = "created_verified"
            except (OSError, ValueError, RuntimeError) as exc:
                receipt.update(status="partial_requires_review", error=str(exc))
            receipt["updated_at"] = datetime.now(timezone.utc).isoformat()
            receipt["targets"] = _inspect_targets(fd, request, created)
            atomic_json(receipts_fd, _receipt_name(request_id), receipt)
            return receipt


def main(argv=None):
    parser = argparse.ArgumentParser(description="预览或按已确认请求创建无订单项目空目录；不操作素材")
    parser.add_argument("--model", required=True)
    parser.add_argument("--decisions", required=True)
    parser.add_argument("--projects-root", required=True)
    parser.add_argument("--receipts-root", help="已存在的独立服务回执目录")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--approved-request-id")
    args = parser.parse_args(argv)
    try:
        model, decisions = load_json(args.model), load_json(args.decisions)
        if args.apply:
            if not args.receipts_root:
                raise FolderError("执行时必须指定独立回执目录")
            result = apply_folders(model, decisions, args.projects_root, args.receipts_root, args.approved_request_id)
        else:
            result = preview_folders(model, decisions, args.projects_root, args.receipts_root)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] in ("ready_for_folder_confirmation", "created_verified") else 2


if __name__ == "__main__":
    raise SystemExit(main())
