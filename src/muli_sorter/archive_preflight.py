"""Read-only checks before a classified archive may be reviewed for writing.

This module deliberately stops at a reviewable plan.  It never creates a
directory, reads media bytes for a copy, talks to an order system, or grants a
media write permission.
"""
from __future__ import annotations

from collections import defaultdict
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import unicodedata

from .archive_layout import studio_target_rows as target_rows
from .archive_io import directory
from .archive_source import verify_sources
from .intake import relative
from .review import compile_plan


SCHEMA_VERSION = "archive-preflight/0.1"


def _fold(path: str) -> str:
    """Use the same Unicode and case comparison for all planned paths."""
    return unicodedata.normalize("NFC", path).casefold()


def _reason(code: str, detail: str | None = None) -> str:
    return code if not detail else f"{code}: {detail}"


def _intentions(folder_intents):
    if folder_intents is None:
        return []
    if isinstance(folder_intents, dict):
        value = folder_intents.get("intentions", [])
    else:
        value = folder_intents
    return value if isinstance(value, list) else []


def _walk_directory(root_fd: int, path: str):
    """Return a safe directory fd, or a status without following links.

    The caller owns a returned fd.  A missing component returns ``missing``;
    an existing non-directory or symlink is reported separately so the report
    can distinguish a folder that may be created from an unsafe target.
    """
    parts = relative(path)
    fd = os.dup(root_fd)
    traversed = []
    try:
        for part in parts:
            try:
                info = os.stat(part, dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:
                return fd, "missing", "/".join(traversed + [part])
            except OSError as exc:
                return fd, "error", str(exc)
            if stat.S_ISLNK(info.st_mode):
                return fd, "symlink", "/".join(traversed + [part])
            if not stat.S_ISDIR(info.st_mode):
                return fd, "not_directory", "/".join(traversed + [part])
            try:
                nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            except OSError as exc:
                return fd, "error", str(exc)
            os.close(fd)
            fd = nxt
            traversed.append(part)
        return fd, "ok", "/".join(traversed)
    except BaseException:
        os.close(fd)
        raise


def _entry(parent_fd: int, name: str):
    try:
        info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return "missing"
    except OSError as exc:
        return "error:" + str(exc)
    if stat.S_ISLNK(info.st_mode):
        return "symlink"
    if stat.S_ISDIR(info.st_mode):
        return "directory"
    if stat.S_ISREG(info.st_mode):
        return "file"
    return "other"


def _target_parent(projects_fd: int, path: str):
    """Inspect a target parent and return ``(fd, state, missing_dirs)``."""
    parent = str(PurePosixPath(path).parent)
    fd, state, detail = _walk_directory(projects_fd, parent)
    if state != "missing":
        return fd, state, [] if state == "ok" else [detail]

    # The walk stopped at the first missing component.  Existing components
    # are safe; the remaining components are only a proposed mkdir chain.
    planned = _planned_missing(parent, detail)
    return fd, "missing", planned


def _planned_missing(path: str, detail: str):
    """List only the first missing component and descendants."""
    parts = list(relative(path))
    missing_parts = detail.split("/") if detail else []
    first_missing = max(len(missing_parts) - 1, 0)
    return ["/".join(parts[:i]) for i in range(first_missing + 1, len(parts) + 1)]


def _entry_fold_conflicts(parent_fd: int, name: str):
    """Find same-folded sibling names without descending into their content."""
    wanted = _fold(name)
    try:
        names = []
        with os.scandir(parent_fd) as entries:
            for index, entry in enumerate(entries):
                if index >= 10000:
                    return ['error: target directory exceeds bounded inspection limit']
                names.append(entry.name)
    except OSError as exc:
        return ["error:" + str(exc)]
    return sorted(existing for existing in names if existing != name and _fold(existing) == wanted)


def _intent_identity(intent, project, *, require_creation=False):
    """Check the order/path identity carried by a folder intention."""
    if not isinstance(intent, dict) or intent.get("path") != project.get("path"):
        return False
    project_order = project.get("order_id")
    intent_order = intent.get("order_id")
    shoot_date = intent.get("shoot_date")
    dates = project.get("dates")
    if not (isinstance(project_order, str) and project_order and
            isinstance(intent_order, str) and intent_order and
            project_order == intent_order and
            isinstance(shoot_date, str) and isinstance(dates, list) and shoot_date in dates):
        return False
    if not require_creation:
        return True
    request = intent.get("folder_request")
    payload = request.get("payload") if isinstance(request, dict) else None
    return (intent.get("action") == "create_required" and isinstance(payload, dict) and
            request.get("service") == "photo-project-folder-service" and
            payload.get("order_id") == intent_order and payload.get("shoot_date") == shoot_date and
            isinstance(payload.get("customer_name"), str) and bool(payload["customer_name"].strip()) and
            isinstance(payload.get("package_code"), str) and bool(payload["package_code"].strip()))


def _source_rows(unit, evidence, error=None):
    rows = []
    for item in unit.get("files", []):
        rows.append({
            "source_path": item.get("source_path"),
            "name": item.get("name"),
            "size_bytes": item.get("size_bytes"),
            "blake3": item.get("blake3"),
            "manifest_evidence": evidence,
            "manifest_identity_verified": error is None,
            # verify_sources validates receipt/manifest identity and current
            # size.  It intentionally does not claim a full content read.
            "full_content_hash_verified": False,
        })
    return rows


def _empty_report(model=None, decisions=None, reasons=None):
    return {
        "schema_version": SCHEMA_VERSION,
        "mode": "archive_preflight",
        "status": "blocked",
        "executable": False,
        "media_write_authorized": False,
        "report_id": (model or {}).get("report_id"),
        "decision_id": None,
        "summary": {
            "confirmed_units": 0,
            "confirmed_files": 0,
            "confirmed_bytes": 0,
            "pending_units": 0,
            "deferred_units": 0,
            "projects": 0,
            "targets": 0,
            "files": 0,
            "bytes": 0,
        },
        "projects": [],
        "units": [],
        "directories_to_create": [],
        "blocking_reasons": list(reasons or []),
        "required_before_execution": [
            "完整读取并哈希每个源文件内容，并与新鲜清单核对",
            "实时复核订单和项目目录状态；订单缓存或旧快照不构成执行权威",
            "取得本次具体归档范围的媒体写入授权",
            "目标目录创建后回读目录类型、无软链接、目标身份和文件结果",
        ],
    }


def inspect_plan(model, decisions, staging: Path, projects: Path, runtime_provider, *, folder_intents=None):
    """Inspect confirmed archive assignments without changing the filesystem.

    ``ready_for_write_review`` means that the bounded, read-only checks have
    produced a plan for human review.  It is never execution success or write
    authorization.
    """
    try:
        compiled = compile_plan(model, decisions)
    except Exception as exc:
        report = _empty_report(model, decisions, [_reason("invalid_decisions", str(exc))])
        return report

    units_by_id = {unit["unit_id"]: unit for unit in model["units"]}
    projects_by_id = {a["project"]["project_id"]: a["project"] for a in compiled["assignments"]}
    decision_segments = {segment["segment_id"]: segment for segment in decisions["segments"]}
    confirmed = []
    pending_units = deferred_units = 0
    for segment in decisions["segments"]:
        count = len(segment["unit_ids"])
        if segment["decision"] == "confirmed":
            for unit_id in segment["unit_ids"]:
                confirmed.append((unit_id, segment, projects_by_id[segment["project_id"]]))
        elif segment["decision"] == "pending":
            pending_units += count
        elif segment["decision"] == "deferred":
            deferred_units += count

    report = _empty_report(model, decisions)
    report["decision_id"] = compiled["decision_id"]
    report["summary"]["pending_units"] = pending_units
    report["summary"]["deferred_units"] = deferred_units
    if not confirmed:
        report["blocking_reasons"].append(_reason("needs_assignment_confirmation", "没有已 confirmed 的素材单元"))
        if pending_units == 0:
            report["blocking_reasons"].append(_reason("no_confirmed_units"))
        return report

    root_reasons = []
    try:
        with directory(staging):
            staging_safe = True
    except (OSError, ValueError) as exc:
        staging_safe = False
        root_reasons.append(_reason("staging_root_unusable", str(exc)))

    projects_ctx = directory(projects)

    intents = _intentions(folder_intents)
    intent_by_path = defaultdict(list)
    intent_by_exact_path = defaultdict(list)
    paths_by_order = defaultdict(set)
    intent_reasons = []
    for intent in intents:
        if not isinstance(intent, dict):
            intent_reasons.append(_reason("folder_intent_invalid", "意图不是对象"))
            continue
        path = intent.get("path")
        if not isinstance(path, str):
            continue
        try:
            relative(path)
        except Exception as exc:
            intent_reasons.append(_reason("folder_intent_invalid", str(exc)))
            continue
        intent_by_path[_fold(path)].append(intent)
        intent_by_exact_path[path].append(intent)
        if intent.get("order_id"):
            paths_by_order[intent["order_id"]].add(_fold(path))
    for key, rows in intent_by_path.items():
        if len(rows) > 1:
            intent_reasons.append(_reason("folder_order_intent_conflict", key))
    for order_id, paths in paths_by_order.items():
        if len(paths) > 1:
            intent_reasons.append(_reason("folder_order_intent_conflict", f"订单 {order_id} 对应多个目录"))

    project_rows = {}
    unit_rows = {}
    all_targets = []
    target_keys = defaultdict(list)
    planned_directories = set()

    # The secure projects handle remains open while all existing directory and
    # target entries are inspected.  No mkdir-capable helper is used here.
    context = projects_ctx
    project_fd = None
    context_entered = False
    try:
        try:
            project_fd = context.__enter__()
            context_entered = True
        except (OSError, ValueError) as exc:
            project_fd = None
            root_reasons.append(_reason("projects_root_unusable", str(exc)))

        for unit_id, segment, project in confirmed:
            unit = units_by_id[unit_id]
            project_id = project["project_id"]
            prow = project_rows.setdefault(project_id, {
                "project_id": project_id,
                "path": project.get("path"),
                "units": 0,
                "files": 0,
                "bytes": 0,
                "sources": [],
                "targets": [],
                "directories_to_create": [],
                "blocking_reasons": [],
            })
            urow = {
                "unit_id": unit_id,
                "project_id": project_id,
                "units": 1,
                "files": len(unit.get("files", [])),
                "bytes": sum(item.get("size_bytes", 0) for item in unit.get("files", [])),
                "sources": [],
                "source_evidence": {},
                "targets": [],
                "directories_to_create": [],
                "blocking_reasons": [],
            }
            unit_rows[unit_id] = urow

            source_evidence = {}
            source_error = None
            if staging_safe:
                try:
                    source_evidence = verify_sources(staging, unit, runtime_provider)
                except Exception as exc:
                    source_error = str(exc)
                    urow["blocking_reasons"].append(_reason("source_verification_failed", source_error))
            else:
                source_error = root_reasons[0] if root_reasons else "staging root unavailable"
                urow["blocking_reasons"].append(_reason("source_verification_failed", source_error))
            urow["source_evidence"] = source_evidence
            urow["sources"] = _source_rows(unit, source_evidence, source_error)
            prow["sources"].extend({"unit_id": unit_id, **row} for row in urow["sources"])
            if source_error:
                prow["blocking_reasons"].append(_reason("source_verification_failed", source_error))

            try:
                rows = target_rows(unit, project)
            except Exception as exc:
                rows = []
                urow["blocking_reasons"].append(_reason("target_plan_invalid", str(exc)))
            urow["targets"] = rows
            all_targets.extend((unit_id, project_id, row) for row in rows)

            # Validate the project directory and the only permitted missing
            # project case (a unique, exact create_required intent).
            project_path = project.get("path")
            project_fd_state = "unusable"
            if project_fd is not None:
                try:
                    check_fd, project_fd_state, detail = _walk_directory(project_fd, project_path)
                except Exception as exc:
                    detail = str(exc)
                    project_fd_state = "error"
                else:
                    os.close(check_fd)
            else:
                detail = "projects root unavailable"

            matching = intent_by_exact_path.get(project_path, []) if isinstance(project_path, str) else []
            folded_matching = intent_by_path.get(_fold(project_path), []) if isinstance(project_path, str) else []
            if project_fd_state == "ok":
                prow["exists"] = True
                if folded_matching and (len(matching) != len(folded_matching) or
                        any(row.get("action") != "use_existing" or
                            (row.get("project_id") and row.get("project_id") != project_id) or
                            not _intent_identity(row, project)
                            for row in matching)):
                    prow["blocking_reasons"].append(_reason("folder_order_intent_conflict", project_path))
            elif project_fd_state == "missing":
                prow["exists"] = False
                allowed = len(matching) == 1 and _intent_identity(matching[0], project, require_creation=True)
                if allowed and matching[0].get("project_id") and matching[0]["project_id"] != project_id:
                    allowed = False
                if allowed and len(folded_matching) != len(matching):
                    allowed = False
                if not allowed:
                    code = "manual_project_creation_pending" if project.get("evidence_source") == "manual_no_order" else "missing_project_folder_intent"
                    prow["blocking_reasons"].append(_reason(code, project_path))
                else:
                    # Include the project itself and the archive parents that
                    # the real executor would need to create.
                    prow["directories_to_create"].extend(_planned_missing(project_path, detail))
                    planned_directories.update(prow["directories_to_create"])
            elif project_fd_state == "symlink":
                prow["blocking_reasons"].append(_reason("project_symlink", detail))
            elif project_fd_state == "not_directory":
                prow["blocking_reasons"].append(_reason("project_not_directory", detail))
            else:
                prow["blocking_reasons"].append(_reason("project_directory_unusable", detail))

            # Check target parents and both the final target and generated
            # temporary name.  Missing parents are reported as planned
            # directories, never created by this function.
            for row in rows:
                target = dict(row)
                parent_fd = None
                if project_fd is not None:
                    try:
                        parent_fd, parent_state, missing = _target_parent(project_fd, row["target_path"])
                    except Exception as exc:
                        parent_state, missing = "error", [str(exc)]
                    if parent_state == "missing":
                        urow["directories_to_create"].extend(missing)
                        planned_directories.update(missing)
                    elif parent_state == "symlink":
                        urow["blocking_reasons"].append(_reason("target_parent_symlink", missing[0]))
                    elif parent_state == "not_directory":
                        urow["blocking_reasons"].append(_reason("target_parent_not_directory", missing[0]))
                    elif parent_state == "error":
                        urow["blocking_reasons"].append(_reason("target_parent_unusable", missing[0]))
                else:
                    parent_state = "error"
                    missing = ["projects root unavailable"]

                target["exists"] = False
                target["temp_exists"] = False
                if parent_fd is not None and parent_state == "ok":
                    target_state = _entry(parent_fd, row["target_name"])
                    temp_state = _entry(parent_fd, row["temp"])
                    target["exists"] = target_state != "missing"
                    target["temp_exists"] = temp_state != "missing"
                    if target_state == "symlink":
                        urow["blocking_reasons"].append(_reason("target_symlink", row["target_path"]))
                    elif target_state not in ("missing", "file"):
                        urow["blocking_reasons"].append(_reason("target_conflict", row["target_path"]))
                    elif target_state == "file":
                        urow["blocking_reasons"].append(_reason("target_exists", row["target_path"]))
                    for conflict in _entry_fold_conflicts(parent_fd, row["target_name"]):
                        if conflict.startswith("error:"):
                            urow["blocking_reasons"].append(_reason("target_parent_unreadable", conflict))
                        else:
                            urow["blocking_reasons"].append(_reason("target_name_conflict", conflict))
                    if temp_state == "symlink":
                        urow["blocking_reasons"].append(_reason("temp_symlink", row["temp"]))
                    elif temp_state not in ("missing", "file"):
                        urow["blocking_reasons"].append(_reason("temp_conflict", row["temp"]))
                    elif temp_state == "file":
                        urow["blocking_reasons"].append(_reason("temp_exists", row["temp"]))
                    for conflict in _entry_fold_conflicts(parent_fd, row["temp"]):
                        if conflict.startswith("error:"):
                            urow["blocking_reasons"].append(_reason("target_parent_unreadable", conflict))
                        else:
                            urow["blocking_reasons"].append(_reason("temp_name_conflict", conflict))
                if parent_fd is not None:
                    os.close(parent_fd)
                target["full_content_hash_verified"] = False
                urow["targets"][urow["targets"].index(row)] = target
                prow["targets"].append({"unit_id": unit_id, **target})

                target_keys[_fold(row["target_path"])].append((unit_id, "target", row["target_path"]))
                target_keys[_fold(str(PurePosixPath(row["target_path"]).parent / row["temp"]))].append((unit_id, "temp", row["temp"]))

            prow["units"] += 1
            prow["files"] += urow["files"]
            prow["bytes"] += urow["bytes"]
            prow["directories_to_create"].extend(urow["directories_to_create"])

        for key, matches in target_keys.items():
            if len(matches) > 1:
                units = sorted({item[0] for item in matches})
                reason = _reason("target_path_conflict", f"{key} ({', '.join(units)})")
                for unit_id in units:
                    unit_rows[unit_id]["blocking_reasons"].append(reason)
                for unit_id in units:
                    project_id = unit_rows[unit_id]["project_id"]
                    project_rows[project_id]["blocking_reasons"].append(reason)

        for row in project_rows.values():
            row["directories_to_create"] = sorted(set(row["directories_to_create"]))
        for row in unit_rows.values():
            row["directories_to_create"] = sorted(set(row["directories_to_create"]))
    finally:
        if project_fd is not None and context_entered:
            context.__exit__(None, None, None)

    report["projects"] = sorted(project_rows.values(), key=lambda row: row["project_id"])
    report["units"] = sorted(unit_rows.values(), key=lambda row: row["unit_id"])
    report["directories_to_create"] = sorted(planned_directories)
    report["blocking_reasons"].extend(root_reasons)
    report["blocking_reasons"].extend(intent_reasons)
    for row in report["projects"]:
        report["blocking_reasons"].extend(row["blocking_reasons"])
    for row in report["units"]:
        report["blocking_reasons"].extend(row["blocking_reasons"])
    report["blocking_reasons"] = list(dict.fromkeys(report["blocking_reasons"]))

    confirmed_files = sum(row["files"] for row in report["units"])
    confirmed_bytes = sum(row["bytes"] for row in report["units"])
    report["summary"].update({
        "confirmed_units": len(report["units"]),
        "confirmed_files": confirmed_files,
        "confirmed_bytes": confirmed_bytes,
        "projects": len(report["projects"]),
        "targets": len(all_targets),
        "files": confirmed_files,
        "bytes": confirmed_bytes,
    })
    capacity = {"required_bytes": confirmed_bytes, "available_bytes": None, "sufficient": None}
    try:
        usage = shutil.disk_usage(Path(projects))
        capacity.update(available_bytes=usage.free, sufficient=usage.free >= confirmed_bytes)
        if usage.free < confirmed_bytes:
            report["blocking_reasons"].append(_reason("insufficient_capacity", f"需要 {confirmed_bytes}，可用 {usage.free}"))
    except OSError as exc:
        report["blocking_reasons"].append(_reason("capacity_estimate_failed", str(exc)))
    report["capacity"] = capacity
    report["status"] = "ready_for_write_review" if not report["blocking_reasons"] else "blocked"
    return report
