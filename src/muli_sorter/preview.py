from datetime import datetime, timezone
from pathlib import Path
from .intake import BATCH, EvidenceError, decode, read_bytes, resolve_files, runtime_check, validate_record
from .matching import group_files, project_index


def build_preview(staging: Path, projects: Path, snapshot=None, bindings=None):
    now = datetime.now(timezone.utc)
    index = project_index(projects)
    results, completed = [], {}
    for entry in sorted(staging.iterdir()):
        if not BATCH.fullmatch(entry.name):
            continue
        result = {"batch_id": entry.name, "status": "waiting", "reasons": [], "file_count": None, "groups": []}
        results.append(result)
        try:
            if entry.is_symlink() or not entry.is_dir():
                raise EvidenceError("批次路径不是普通目录")
            if not (entry / "ingest_complete.json").exists():
                result["reasons"].append("尚无成功完成回执；不读取未完成批次的媒体")
                states = [b for b in (snapshot or {}).get("batches", []) if b.get("batch_id") == entry.name]
                if len(states) == 1:
                    state = states[0].get("state", "UNKNOWN")
                    label = {"COPYING": "正在拷贝", "VERIFYING": "正在校验", "FINALIZING": "正在生成回执", "INTERRUPTED": "已中断", "FAILED": "失败", "QUEUED": "排队中"}.get(state, "尚未确认完成")
                    result["reasons"].append("状态快照：" + label)
                continue
            m = validate_record(staging, entry.name)
            result["file_count"] = len(m["files"])
            runtime_check(snapshot, m, now)
            completed[entry.name] = m
            result["manifest_id"] = m["manifest_id"]
            result["reasons"] = ["回执、两份清单摘要与运行状态快照一致"]
        except (EvidenceError, OSError, KeyError, TypeError, ValueError) as exc:
            result["status"] = "blocked"
            result["reasons"] = [str(exc)]
    for r in results:
        m = completed.get(r["batch_id"])
        if m is None:
            continue
        try:
            files = resolve_files(staging, m, completed)
            r["groups"] = group_files(files, index, bindings or [], m["manifest_id"])
            r["status"] = "verified"
            if any(f["copy_status"] == "skipped_existing" for f in files):
                r["reasons"].append("已解析旧批次引用；这些是已有内容，不代表新增素材")
        except (EvidenceError, OSError, KeyError, TypeError, ValueError) as exc:
            r["status"] = "blocked"
            r["reasons"] = [str(exc)]
    groups = [g for b in results for g in b["groups"]]
    return {"schema_version": "0.1", "mode": "read_only_preview", "generated_at": now.isoformat(),
            "summary": {"batches": len(results), "verified_batches": sum(b["status"] == "verified" for b in results), "blocked_batches": sum(b["status"] != "verified" for b in results),
                        "groups": len(groups), "ready_groups": sum(g["status"] == "ready" for g in groups), "review_groups": sum(g["status"] == "review" for g in groups)},
            "project_count": len(index), "projects": index, "batches": results,
            "limitations": ["只读分类预览，没有复制、移动、链接或删除素材。", "本轮只校验清单摘要、状态和文件大小，未重新读取全量媒体计算哈希；源内容仍需归档执行前重新核验。", "项目候选来自目录名；未读取真实飞书订单或验证拍摄人员、设备对应关系。", "批次可能是选择性导入；完成回执不等于整张卡完整导入。", "多个批次可能引用同一份素材，分组计数不能当作新增素材数量。", "运行状态使用采集时刻的只读快照，预览不构成持续运行或自动归档验收。"]}
