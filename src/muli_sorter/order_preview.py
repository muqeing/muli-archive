"""Read-only order linkage preview.

This module joins explicit shooting dates in an existing confirmation model to
the smallest Feishu order projection needed for folder candidates.  It never
creates folders, changes the model's decisions, or writes to Feishu.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
import importlib.util
import json
from pathlib import Path
import re
import sys
from uuid import uuid4

from .intake import relative
from .matching import project_index
from .order_catalog import OrderError, cli_read, read_orders, read_product_codes
from .order_projects import enrich_confirmation, plan_folders
from .queue_files import publish
from .review import validate_model
from .review_render import render_review


OUTPUT_FILES = ("确认模型.json", "拍摄段确认.html", "目录处理意图.json", "读取摘要.json")
_ORDER_ID = re.compile(r"\d{5}\Z")


def _explicit_dates(model: dict) -> tuple[list[str], int]:
    dates, unknown = set(), 0
    for unit in model.get("units", []):
        value = unit.get("capture_date")
        try:
            parsed = date.fromisoformat(value) if isinstance(value, str) else None
            if parsed is None or parsed.isoformat() != value:
                raise ValueError()
        except (TypeError, ValueError):
            unknown += 1
            continue
        dates.add(value)
    return sorted(dates), unknown


def _catalog(orders: list[dict], dates: list[str], *, fetched_at: str, revision=None) -> dict:
    return {
        "schema_version": "orders/0.5",
        "source": "feishu_cli_readonly",
        "fetched_at": fetched_at,
        "table_revision": revision,
        "complete_for_dates": dates,
        "orders": orders,
    }


def _read_order_batches(resource, dates, reader):
    batches = [dates[i : i + 60] for i in range(0, len(dates), 60)]
    orders, revisions, record_dates = [], set(), defaultdict(set)
    fetched_at = None
    for batch_dates in batches:
        try:
            catalog = read_orders(resource, batch_dates, reader=reader)
        except OrderError:
            raise
        except Exception:
            raise OrderError("订单读取失败") from None
        revision = catalog.get("table_revision")
        revisions.add(revision)
        fetched_at = max(fetched_at or "", catalog.get("fetched_at") or "")
        for order in catalog.get("orders", []):
            order_id = order.get("order_id")
            record_id = order.get("record_id")
            record_dates[record_id].add(order.get("shoot_date"))
            orders.append(order)
    if len(revisions) > 1:
        raise OrderError("订单分组读取期间表版本改变，本次预览作废")
    return _catalog(orders, dates, fetched_at=fetched_at or datetime.now(timezone.utc).isoformat(),
                    revision=next(iter(revisions), None)), batches, record_dates


def _candidate_product_ids(catalog, projects):
    existing = {p.get("order_id") for p in projects if p.get("order_id")}
    counts = defaultdict(int)
    for order in catalog["orders"]:
        counts[order.get("order_id")] += 1
    ids = set()
    for order in catalog["orders"]:
        order_id = order.get("order_id")
        stages = order.get("stage") or []
        product_ids = order.get("product_record_ids")
        if (order_id in existing or counts[order_id] != 1 or
                not isinstance(stages, list) or any(not isinstance(x, str) for x in stages) or
                any(any(word in stage for word in ("取消", "退款")) for stage in stages) or
                not _ORDER_ID.fullmatch(order_id or "") or not order.get("customer_name") or
                not isinstance(product_ids, list) or len(product_ids) != 1):
            continue
        ids.add(product_ids[0])
    return sorted(ids)


def _read_products(resource, ids, reader):
    result, batches = {}, [ids[i : i + 200] for i in range(0, len(ids), 200)]
    for batch in batches:
        try:
            result.update(read_product_codes(resource, batch, reader=reader))
        except OrderError:
            raise
        except Exception:
            raise OrderError("产品读取失败") from None
    return result, batches


def _service_namer(folder_namer):
    """Adapt the folder service's ``(path, name)`` result to plan_folders."""
    def name(payload):
        value = folder_namer(payload)
        if isinstance(value, str) and value:
            return value
        if (not isinstance(value, tuple) or len(value) != 2 or
                not isinstance(value[0], (str, Path)) or not isinstance(value[1], str) or
                not value[0] or not value[1] or Path(value[0]).name != value[1]):
            raise ValueError("目录服务必须返回 (path, name)")
        path = Path(value[0])
        if path.is_absolute():
            path = path.relative_to('/projects')
        relative(path.as_posix())
        return path.as_posix()
    return name


def prepare_preview(model, projects, resource, reader, folder_namer):
    """Return ``(enriched_model, folder_intents, read_summary)``.

    All remote reads complete before the model is enriched.  Thus a failed
    date or product read cannot produce a successful partial preview.
    """
    validate_model(model)
    dates, unknown_count = _explicit_dates(model)
    catalog, date_batches, record_dates = _read_order_batches(resource, dates, reader) if dates else (
        _catalog([], [], fetched_at=datetime.now(timezone.utc).isoformat()), [], defaultdict(set))
    model_project_ids = {project.get("project_id") for project in model.get("projects", [])}
    queried_order_ids = {order.get("order_id") for order in catalog["orders"]}
    changed_live_projects = sorted({project.get("project_id") for project in projects
                                    if project.get("order_id") in queried_order_ids and
                                    project.get("project_id") not in model_project_ids})
    if changed_live_projects:
        raise OrderError("项目目录索引已改变，请重新生成素材确认模型")
    product_ids = _candidate_product_ids(catalog, projects)
    product_codes, product_batches = _read_products(resource, product_ids, reader)
    order_counts = Counter(o.get("order_id") for o in catalog["orders"])
    duplicates = sorted(order_id for order_id, count in order_counts.items() if order_id and count > 1)
    duplicate_records = sorted(rid for rid, values in record_dates.items() if rid and len(values) > 1)
    intents = plan_folders(catalog, projects, product_codes, _service_namer(folder_namer))
    unknown_existing = sorted({row.get("project_id") for row in intents["intentions"]
                               if row.get("action") == "use_existing" and row.get("project_id") not in model_project_ids})
    if unknown_existing:
        raise OrderError("项目目录索引已改变，请重新生成素材确认模型")
    enriched = enrich_confirmation(model, catalog, intents)
    actions = defaultdict(int)
    for row in intents["intentions"]:
        actions[row.get("action")] += 1
    summary = {
        "schema_version": "order-preview-summary/0.1",
        "source": "feishu_cli_readonly",
        "explicit_capture_date_count": len(dates),
        "unknown_capture_date_units": unknown_count,
        "order_query_batch_count": len(date_batches),
        "order_count": len(catalog["orders"]),
        "duplicate_order_ids": duplicates,
        "duplicate_record_ids": duplicate_records,
        "product_record_ids_queried": len(product_ids),
        "product_query_batch_count": len(product_batches),
        "folder_intent_counts": dict(sorted(actions.items())),
        "covered_dates": dates,
        "folder_write_authorized": False,
    }
    return enriched, intents, summary


def _reject_symlink_components(path: Path):
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            if current.is_symlink():
                # macOS exposes /tmp and /var as stable system aliases.  They
                # are safe path aliases; user-created aliases below them are
                # still rejected.
                if current in (Path("/tmp"), Path("/var")):
                    continue
                raise ValueError("输出路径不能包含软链接")
        except OSError:
            raise ValueError("无法核验输出路径") from None


def _validate_output_target(output, projects) -> Path:
    raw = Path(output)
    candidate = raw.absolute().resolve(strict=False)
    if candidate == Path("/Volumes") or candidate.is_relative_to(Path("/Volumes")):
        raise ValueError("预览输出必须位于本地工作区")
    _reject_symlink_components(raw.absolute())
    protected = Path(projects).resolve(strict=True)
    if candidate == protected or candidate.is_relative_to(protected) or protected.is_relative_to(candidate):
        raise ValueError("预览输出必须与项目目录分离")
    if candidate.exists():
        raise ValueError("预览输出目录必须是新目录")
    return candidate


def _create_output(path: Path):
    path.mkdir(parents=True, exist_ok=False, mode=0o700)
    return path


def _load_folder_module(path_value: str):
    path = Path(path_value)
    if not path.is_absolute() or path.suffix != ".py" or path.is_symlink() or not path.is_file():
        raise ValueError("目录服务模块必须是显式可信的本地 Python 文件")
    spec = importlib.util.spec_from_file_location("muli_sorter_trusted_folder_service_" + uuid4().hex, path)
    if spec is None or spec.loader is None:
        raise ValueError("目录服务模块无法加载")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    function = getattr(module, "project_paths", None)
    if not callable(function):
        raise ValueError("目录服务模块缺少 project_paths")
    return function


def _json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n").encode()


def main(argv=None):
    parser = argparse.ArgumentParser(description="生成本地订单联动只读预览")
    parser.add_argument("--model", required=True)
    parser.add_argument("--projects", required=True)
    parser.add_argument("--resource", required=True)
    parser.add_argument("--folder-service-module", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    output = None
    try:
        from .cli import load_json
        model = load_json(args.model)
        resource = load_json(args.resource)
        projects_root = Path(args.projects).resolve(strict=True)
        projects = project_index(projects_root)
        output = _validate_output_target(args.output, projects_root)
        project_paths = _load_folder_module(args.folder_service_module)
        enriched, intents, summary = prepare_preview(model, projects, resource, cli_read,
                                                      lambda payload: project_paths(payload, Path("/projects")))
        _create_output(output)
        publish(output, "", {
            "确认模型.json": _json_bytes(enriched),
            "拍摄段确认.html": render_review(enriched, project_root_label=str(projects_root)).encode(),
            "目录处理意图.json": _json_bytes(intents),
            "读取摘要.json": _json_bytes(summary),
        })
        print(json.dumps({"ok": True, "output": str(output), "summary": summary}, ensure_ascii=False))
        return 0
    except Exception:
        print("订单预览失败；未生成成功预览", file=sys.stderr)
        return 1


__all__ = ["prepare_preview", "main"]


if __name__ == "__main__":
    raise SystemExit(main())
