"""Local association decisions only. This module never operates on media."""
from __future__ import annotations
from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
import json
import re
from .intake import relative
from .manual_projects import ManualProjectError, manual_catalog


class ReviewError(ValueError):
    pass


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(value):
    # Large confirmation models are already structured by unit. Hash the exact
    # same canonical byte sequence without materializing the whole JSON again.
    if (type(value) is dict and type(value.get('units')) is list and
            len(value['units']) > 256 and all(type(key) is str for key in value)):
        result=sha256()
        result.update(b'{')
        for index, key in enumerate(sorted(value)):
            if index:result.update(b',')
            result.update(canonical(key));result.update(b':')
            if key == 'units':
                result.update(b'[')
                for unit_index, unit in enumerate(value[key]):
                    if unit_index:result.update(b',')
                    result.update(canonical(unit))
                result.update(b']')
            else:
                result.update(canonical(value[key]))
        result.update(b'}')
        return result.hexdigest()
    return sha256(canonical(value)).hexdigest()


# The snapshot id names the material, not the moment it was published. Wall
# clock fields stay in the model for display, but a plain republish of identical
# material must keep the same id: otherwise every open confirmation page and its
# saved draft is invalidated by a refresh that changed nothing.
IDENTITY_EXCLUDED = ('report_id', 'snapshot_at')


def identity_value(model):
    """Return the material half of a model, used for the snapshot identity."""
    return {key: value for key, value in model.items() if key not in IDENTITY_EXCLUDED}


def identity_digest(model):
    return 'sha256:' + digest(identity_value(model))


def legacy_identity_digest(model):
    """Pre-2026-10-08 recipe; accepted so a rolling update stays compatible."""
    return 'sha256:' + digest({k: v for k, v in model.items() if k != 'report_id'})


def _unique_strings(items, label):
    if not isinstance(items, list) or any(not isinstance(v, str) or not v for v in items) or len(set(items)) != len(items):
        raise ReviewError(label + "必须是无重复的字符串列表")
    return set(items)


def build_review_model(report, *, _consume_input=False):
    if report.get("mode") != "read_only_preview":
        raise ReviewError("只能从只读预览生成确认模型")
    projects = deepcopy(report.get("projects", []))
    project_ids = _unique_strings([p["project_id"] for p in projects], "项目编号")
    for p in projects:
        relative(p["path"])
    records, excluded = [], []
    for batch in report["batches"]:
        if batch["status"] != "verified":
            excluded.append({k: deepcopy(batch.get(k)) for k in ("batch_id", "status", "reasons")})
            continue
        if not batch.get("manifest_id"):
            raise ReviewError("完成批次缺少清单标识")
        for group in batch["groups"]:
            if group.get("file_count", 0) and not group.get("units"):
                raise ReviewError("旧预览缺少逐文件组，需重新生成只读预览")
            for unit in group.get("units", []):
                records.append({**unit, "provenance": [{"batch_id": batch["batch_id"], "manifest_id": batch["manifest_id"], "group_id": group["group_id"]}]})
            if _consume_input:
                # Only the queue lends this freshly decoded, private report.
                # Public callers keep their original input untouched.
                group['units']=[]
    # Merge overlapping bundles from repeated/partial batch references. A physical
    # source file cannot end up in two independently confirmable units.
    parent = list(range(len(records)))
    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    paths = {}
    for i, record in enumerate(records):
        if not record.get("files"):
            raise ReviewError("空素材单元")
        for f in record["files"]:
            relative(f["source_path"])
            if not re.fullmatch(r"[0-9a-f]{64}", f.get("blake3") or "") or type(f.get("size_bytes")) is not int or f["size_bytes"] < 0:
                raise ReviewError("素材单元缺少有效的内容证据")
            if f["source_path"] in paths:
                prior, old = paths[f["source_path"]]
                if (old["blake3"], old["size_bytes"]) != (f["blake3"], f["size_bytes"]):
                    raise ReviewError("相同来源路径出现不同的内容证据")
                parent[find(i)] = find(prior)
            else:
                paths[f["source_path"]] = (i, f)
    components = {}
    for i, record in enumerate(records):
        components.setdefault(find(i), []).append(record)
    # Components now own the lightweight records; the lookup phase is over.
    records.clear(); paths.clear(); parent.clear()
    units, group_membership = [], {}
    for component in components.values():
        files = {f["source_path"]: f for r in component for f in r["files"]}
        # Detach only the unique output descriptors, not every input reference
        # and its unused derived lists before deduplication.
        descriptors = [deepcopy(files[k]) for k in sorted(files)]
        unit_id = "unit-" + digest(descriptors)[:24]
        captures = {r.get("capture_time") for r in component}
        dates = {r.get("capture_date") for r in component}
        kinds = {r.get("kind") for r in component}
        warnings = list(dict.fromkeys(w for r in component for w in r.get("warnings", [])))
        if len(captures) != 1 or len(kinds) != 1:
            warnings.append("重复引用中的拍摄时间或素材类型不一致，需要人工核对")
        provenance = []
        for r in component:
            for p in r["provenance"]:
                if p not in provenance:
                    provenance.append(p)
        representative = provenance[0]
        group_membership.setdefault((representative["manifest_id"], representative["group_id"]), []).append(unit_id)
        unit = {"unit_id": unit_id, "capture_time": next(iter(captures)) if len(captures) == 1 else None,
                "capture_date": next(iter(dates)) if len(dates) == 1 else None,
                "timezone_trusted": all(r.get("timezone_trusted") is True for r in component),
                "device": " / ".join(dict.fromkeys(r.get("device", "设备未识别") for r in component)),
                "kind": next(iter(kinds)) if len(kinds) == 1 else "auxiliary", "files": descriptors,
                "file_names": [f["name"] for f in descriptors], "file_count": len(descriptors), "bytes": sum(f["size_bytes"] for f in descriptors),
                "warnings": warnings, "candidate_project_ids": sorted(set(p for r in component for p in r.get("candidate_project_ids", []) if p in project_ids)),
                "provenance": provenance}
        units.append(unit)
        component.clear()  # Release this input bundle as its output is built.
    units.sort(key=lambda u: (u["capture_time"] or "9999", u["unit_id"]))
    lookup = {u["unit_id"]: u for u in units}
    segments = []
    for key, ids in group_membership.items():
        ids.sort(key=lambda uid: (lookup[uid]["capture_time"] or "9999", uid))
        day = lookup[ids[0]]["capture_date"] or "日期待核对"
        segments.append({"segment_id": "segment-" + digest([key, ids])[:20], "label": day + " 拍摄段", "unit_ids": ids,
                         "project_id": None, "decision": "pending", "acknowledge_date_mismatch": False})
    model = {"schema_version": "0.2", "snapshot_at": report["generated_at"], "example_data": report.get("example_data", False),
             "projects": projects, "units": units, "initial_segments": segments, "excluded_batches": excluded}
    # Deduplication is complete. Retain only the final model while hashing it.
    records.clear(); components.clear(); paths.clear(); parent.clear()
    lookup.clear(); group_membership.clear()
    model["report_id"] = identity_digest(model)
    return model


def validate_model(model):
    if model.get("schema_version") != "0.2":
        raise ReviewError("确认模型内容或版本不一致")
    # Accept the current material identity and the pre-2026-10-08 recipe, which
    # also covered the publish time. That keeps a rolling container update and a
    # rollback compatible while new pages use the stable identity.
    if model.get("report_id") not in (identity_digest(model), legacy_identity_digest(model)):
        raise ReviewError("确认模型内容或版本不一致")
    units = {u["unit_id"]: u for u in model["units"]}
    if len(units) != len(model["units"]):
        raise ReviewError("素材单元标识重复")
    seen = set()
    for u in units.values():
        for f in u["files"]:
            relative(f["source_path"])
            if f["source_path"] in seen:
                raise ReviewError("素材单元存在重叠文件")
            seen.add(f["source_path"])
    for p in model["projects"]:
        relative(p["path"])
    _unique_strings([p["project_id"] for p in model["projects"]], "项目编号")
    return units, {p["project_id"]: p for p in model["projects"]}


def validate_decisions(model, plan):
    units, projects = validate_model(model)
    required = {"schema_version", "mode", "media_write_authorized", "report_id", "created_at", "segments"}
    version = plan.get("schema_version")
    if version == "0.3":
        required.add("manual_projects")
    if set(plan) != required or version not in ("0.2", "0.3") or plan.get("mode") != "classification_confirmation_only" or plan.get("media_write_authorized") is not False:
        raise ReviewError("只接受不含媒体写入授权的归属确认计划")
    if plan["report_id"] != model["report_id"]:
        raise ReviewError("计划来自其他快照，不能套用到当前素材")
    try:
        projects.update(manual_catalog(model, plan.get("manual_projects", [])))
    except ManualProjectError as exc:
        raise ReviewError(str(exc)) from exc
    manual_scopes = {p["project_id"]: set(p["unit_ids"]) for p in plan.get("manual_projects", [])}
    manual_dates = {p["project_id"]: p["shoot_date"] for p in plan.get("manual_projects", [])}
    from .material_triage import companion_links
    linked, _ = companion_links(model)
    try:
        if datetime.fromisoformat(plan["created_at"]).tzinfo is None:
            raise ValueError()
    except (ValueError, TypeError):
        raise ReviewError("计划时间必须包含时区")
    segments = plan["segments"]
    if not isinstance(segments, list) or len(segments) > max(len(units), 1):
        raise ReviewError("拍摄段数量无效")
    seen, ids = set(), set()
    manual_scope_checks = []
    keys = {"segment_id", "label", "unit_ids", "project_id", "decision", "acknowledge_date_mismatch"}
    for s in segments:
        if not isinstance(s, dict) or set(s) != keys:
            raise ReviewError("拍摄段字段不符")
        if not isinstance(s["segment_id"], str) or not 1 <= len(s["segment_id"]) <= 120 or s["segment_id"] in ids:
            raise ReviewError("拍摄段编号无效或重复")
        ids.add(s["segment_id"])
        if not isinstance(s["label"], str) or len(s["label"]) > 160:
            raise ReviewError("拍摄段名称过长或无效")
        members = _unique_strings(s["unit_ids"], "素材单元")
        if not members or members - units.keys() or seen & members:
            raise ReviewError("素材单元未知、重复或空拍摄段")
        seen |= members
        if s["decision"] not in ("pending", "confirmed", "deferred") or type(s["acknowledge_date_mismatch"]) is not bool:
            raise ReviewError("拍摄段状态无效")
        project_id = s["project_id"]
        if project_id is not None and (not isinstance(project_id, str) or project_id not in projects):
            raise ReviewError("项目不在当前目录或已核对的自建项目计划中")
        if (project_id in manual_scopes and not members.intersection(manual_scopes[project_id]) and
                not any(linked.get(uid) in manual_scopes[project_id] for uid in members) and
                not any(units[uid].get("capture_date") == manual_dates[project_id] for uid in members)):
            manual_scope_checks.append((project_id, members))
        if s["decision"] == "confirmed":
            if project_id is None:
                raise ReviewError("确认归属前必须选定项目")
            dates = projects[project_id].get("dates", [])
            mismatch = any(units[uid].get("capture_date") not in dates for uid in members)
            if mismatch and not s["acknowledge_date_mismatch"]:
                raise ReviewError("跨日期或日期未知的选择需要显式确认")
    if seen != units.keys():
        raise ReviewError("计划遗漏了素材单元，未决定的素材也应保留为待处理")
    # A same-date segment may reuse a manual project created in another segment.
    # Its separately listed sidecars inherit that confirmed parent's authorization,
    # even though the parent was not in the project's original creation scope.
    segments_by_unit = {uid: segment for segment in segments for uid in segment["unit_ids"]}
    for project_id, members in manual_scope_checks:
        for uid in members:
            parent = segments_by_unit.get(linked.get(uid))
            if (parent is None or parent["decision"] != "confirmed" or parent["project_id"] != project_id or
                    not (set(parent["unit_ids"]).intersection(manual_scopes[project_id]) or
                         any(units[parent_uid].get("capture_date") == manual_dates[project_id]
                             for parent_uid in parent["unit_ids"]))):
                raise ReviewError("自建项目不属于当前拍摄段的素材范围")
    return plan


def compile_plan(model, decisions):
    validate_decisions(model, decisions)
    units, projects = validate_model(model)
    projects.update(manual_catalog(model, decisions.get("manual_projects", [])))
    assignments = []
    for segment in decisions["segments"]:
        if segment["decision"] != "confirmed":
            continue
        files = [deepcopy(f) for uid in segment["unit_ids"] for f in units[uid]["files"]]
        warnings = list(dict.fromkeys(w for uid in segment["unit_ids"] for w in units[uid].get("warnings", [])))
        # Existing paths come from the model; manual paths derive from validated names/dates.
        assignments.append({"segment_id": segment["segment_id"], "label": segment["label"], "project": deepcopy(projects[segment["project_id"]]),
                            "unit_ids": segment["unit_ids"], "files": files, "warnings": warnings,
                            "provenance": [p for uid in segment["unit_ids"] for p in units[uid]["provenance"]]})
    return {"schema_version": decisions["schema_version"], "mode": "validated_classification_plan", "executable": False, "media_write_authorized": False,
            "report_id": model["report_id"], "decision_id": "sha256:" + digest(decisions), "generated_at": datetime.now(timezone.utc).isoformat(),
            "summary": {"confirmed_segments": len(assignments), "confirmed_units": sum(len(s["unit_ids"]) for s in assignments),
                        "pending_units": sum(len(s["unit_ids"]) for s in decisions["segments"] if s["decision"] == "pending"),
                        "deferred_units": sum(len(s["unit_ids"]) for s in decisions["segments"] if s["decision"] == "deferred")},
            "assignments": assignments, "decisions": deepcopy(decisions),
            "required_before_execution": ["重新核验源清单、实时状态和完整文件内容", "确认具体目标路径、权限、空间及同名冲突", "处理主文件缺失、伴随文件和时间问题", "取得具体真实归档范围的写入确认"]}
