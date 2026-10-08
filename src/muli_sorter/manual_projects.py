"""No-order project proposals. Paths are derived here, never supplied by a browser."""
from datetime import date
import re
import unicodedata


class ManualProjectError(ValueError):
    pass


def fold(value):
    return unicodedata.normalize("NFC", value).casefold()


def project_path(name, shoot_date):
    if (not isinstance(name, str) or name != unicodedata.normalize("NFC", name).strip()
            or not 1 <= len(name) <= 80 or len(name.encode("utf-8")) > 200
            or name.endswith(".") or re.search(r'[\\/<>:"|?*\x00-\x1f\x7f-\x9f\u200b-\u200f\u202a-\u202e\u2060-\u206f\ufeff]', name)):
        raise ManualProjectError("项目名称无效：请用简短名称，不含路径、特殊符号或末尾句点")
    if not isinstance(shoot_date, str) or not re.fullmatch(r"20\d{2}-\d{2}-\d{2}", shoot_date):
        raise ManualProjectError("拍摄日期必须为 2000–2099 年内的有效日期")
    try:
        day = date.fromisoformat(shoot_date)
    except ValueError as exc:
        raise ManualProjectError("拍摄日期无效") from exc
    return f"{day.year}/{day.month}月/{day:%Y%m%d}_自建_{name}"


def manual_catalog(model, definitions):
    """Validate the small input contract, including the units it was proposed for."""
    units = {u["unit_id"] for u in model["units"]}
    if not isinstance(definitions, list) or len(definitions) > max(len(units), 1):
        raise ManualProjectError("自建项目列表无效或过多")
    ids = {p["project_id"] for p in model["projects"]}
    paths = {fold(p["path"]) for p in model["projects"]}
    result = {}
    for item in definitions:
        if not isinstance(item, dict) or set(item) != {"project_id", "name", "shoot_date", "unit_ids"}:
            raise ManualProjectError("自建项目只接受名称、日期和所选素材，不接受指定路径")
        pid = item["project_id"]
        if not isinstance(pid, str) or not re.fullmatch(r"manual-[0-9a-f]{32}", pid) or pid in ids:
            raise ManualProjectError("自建项目编号无效或重复")
        scope = item["unit_ids"]
        if (not isinstance(scope, list) or not scope or any(not isinstance(x, str) for x in scope)
                or len(set(scope)) != len(scope) or set(scope) - units):
            raise ManualProjectError("自建项目的素材范围无效")
        path = project_path(item["name"], item["shoot_date"])
        if fold(path) in paths:
            raise ManualProjectError("同名项目目录已存在或已列入计划，请核对后使用已有项目")
        ids.add(pid)
        paths.add(fold(path))
        result[pid] = {"project_id": pid, "name": "【待新建】" + item["name"], "path": path,
                       "dates": [item["shoot_date"]], "order_id": None, "exists": False,
                       "evidence_source": "manual_no_order",
                       "folder_action": "create_manual_after_confirmed_assignment"}
    return result
