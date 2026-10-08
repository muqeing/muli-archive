"""Render the offline shoot-segment confirmation page.

The page is deliberately a local, self-contained plan editor.  By default it
does not read media, call a server, move files, or imply authorization to write
a project directory; an explicit same-origin submission UI can be enabled by
the caller.
"""

from __future__ import annotations

import json
from io import BytesIO
import math
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from .archive_layout import route_view


_CSS = r"""
:root { color-scheme: light; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; color: #20252b; background: #f4f5f7; line-height: 1.45; }
* { box-sizing: border-box; }
body { margin: 0; }
button, input, select { font: inherit; }
button { cursor: pointer; border: 1px solid #c7ccd3; border-radius: 8px; padding: .52rem .78rem; background: #fff; color: #20252b; }
button:disabled { cursor: not-allowed; opacity: .48; }
.primary { background: #1c5f4a; border-color: #1c5f4a; color: #fff; }
.secondary { background: #eef1f4; }
.link-button { border: 0; background: transparent; color: #9b3b2e; padding-left: 0; }
.page { max-width: 1180px; margin: 0 auto; padding: 1.2rem; }
.topbar { display: flex; justify-content: space-between; align-items: flex-start; gap: 1rem; flex-wrap: wrap; }
.help-link { display: inline-flex; align-items: center; gap: .4rem; min-height: 44px; padding: .4rem .7rem; border: 1px solid #c7ccd3; border-radius: 8px; color: #1c5f4a; text-decoration: none; background: #fff; font-size: .88rem; }
.help-link:hover { background: #e3f2e9; }.help-link:focus-visible { outline: 3px solid #1c5f4a; outline-offset: 3px; }
.help-icon { display: inline-grid; place-items: center; width: 18px; height: 18px; border: 1px solid currentColor; border-radius: 50%; font-weight: 700; }
h1, h2, h3, p { margin-top: 0; }
h1 { margin-bottom: .25rem; font-size: clamp(1.35rem, 3vw, 2rem); }
h2 { font-size: 1.1rem; margin-bottom: .55rem; }
h3 { font-size: 1rem; margin: 0; }
.eyebrow { color: #66717d; font-size: .78rem; letter-spacing: .05em; text-transform: uppercase; margin-bottom: .2rem; }
.muted { color: #66717d; font-size: .88rem; overflow-wrap: anywhere; }
.meta { color: #66717d; font-size: .87rem; margin: .2rem 0; overflow-wrap: anywhere; word-break: break-word; }
.meta code { overflow-wrap: anywhere; word-break: break-word; }
.example-banner, .notice, .warning-box { border-radius: 9px; padding: .7rem .85rem; margin: .8rem 0; }
.example-banner { background: #fff1c8; border: 1px solid #e2bb55; color: #5e4300; }
.notice { min-height: 2.2rem; background: #eef1f4; }
.notice.success { background: #e3f2e9; color: #165438; }
.notice.error { background: #fbe6e3; color: #8d2f23; }
.warning-box { background: #fff6df; color: #654900; font-size: .9rem; }
.panel { background: #fff; border: 1px solid #dfe3e8; border-radius: 12px; padding: 1rem; margin: .9rem 0; box-shadow: 0 2px 8px rgba(24, 32, 40, .04); }
.facts { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: .55rem 1rem; margin: 0; }
.facts div { min-width: 0; }
.facts dt { color: #66717d; font-size: .78rem; }
.facts dd { margin: .1rem 0 0; overflow-wrap: anywhere; }
.toolbar { display: flex; align-items: end; flex-wrap: wrap; gap: .7rem; }
.field { display: grid; gap: .25rem; min-width: 180px; flex: 1 1 220px; }
.field-label { display: grid; gap: .25rem; color: #4f5a66; font-size: .84rem; }
.field input, .field select, .project-line select { width: 100%; min-height: 2.35rem; padding: .42rem .55rem; border: 1px solid #bbc2ca; border-radius: 7px; background: #fff; }
.toolbar-actions { display: flex; flex-wrap: wrap; gap: .5rem; }
.summary { font-weight: 650; margin: .25rem 0 0; }
.segment-card { border: 1px solid #dfe3e8; border-radius: 10px; padding: .85rem; margin: .75rem 0; background: #fff; }
.segment-head { display: flex; align-items: start; justify-content: space-between; gap: .75rem; }
.merge-check, .unit-check, .ack-label { display: flex; align-items: start; gap: .4rem; }
.merge-check { white-space: nowrap; font-size: .84rem; }
.segment-title { display: flex; gap: .6rem; align-items: center; flex: 1; min-width: 0; }
.segment-title h3 { overflow-wrap: anywhere; }
.status { display: inline-flex; border-radius: 999px; padding: .15rem .5rem; font-size: .77rem; white-space: nowrap; background: #eef1f4; color: #4f5a66; }
.status-confirmed { background: #dff1e7; color: #175d3b; }
.status-deferred { background: #fff0d0; color: #795400; }
.segment-meta { color: #66717d; margin: .42rem 0; font-size: .88rem; }
.project-line { margin: .55rem 0; max-width: 650px; }
.unit-list { list-style: none; padding: 0; margin: .5rem 0; }
.unit-details { margin: .65rem 0; border: 1px solid #dfe3e8; border-radius: 8px; padding: .1rem .65rem; }
.unit-details > summary { padding: .65rem 0; font-weight: 600; overflow-wrap: anywhere; }
.unit-details > summary:focus-visible { outline: 2px solid #1c5f4a; outline-offset: 3px; border-radius: 4px; }
.unit-details > button { margin: .25rem .4rem .6rem 0; }
.segment-videos { margin: .8rem 0; }
.segment-videos[hidden] { display: none; }
.segment-videos .unit-preview { margin-left: 0; }
.unit-row { border-top: 1px solid #edf0f2; padding: .55rem 0; }
.unit-content { display: grid; gap: .12rem; min-width: 0; }
.unit-title, .unit-detail, .unit-content .warning, .unit-content .muted { overflow-wrap: anywhere; }
.unit-detail { color: #66717d; font-size: .84rem; }
.warning { color: #8b5b00; font-size: .83rem; }
.unit-preview { min-width: 0; margin: .55rem 0 0 1.5rem; }
.preview-heading { color: #4f5a66; font-size: .82rem; margin-bottom: .35rem; overflow-wrap: anywhere; }
.preview-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: .5rem; max-width: 760px; }
.segment-photos { margin: 1rem 0; padding: .8rem; background: #f6f7f4; border-radius: 10px; }
.photo-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: .5rem; max-width: 960px; }
.photo-trigger { min-width: 0; width: 100%; padding: .3rem; display: grid; gap: .3rem; text-align: left; }
.photo-trigger img { width: 100%; height: auto; aspect-ratio: 3 / 2; object-fit: contain; background: #e5e7e2; border-radius: 5px; }
.photo-caption { font-size: .76rem; overflow-wrap: anywhere; color: #505a50; }
.photo-card .preview-placeholder { aspect-ratio: 3 / 2; display: grid; place-items: center; }
.photo-trigger img[hidden] { display: none; }
.preview-card, .preview-trigger { min-width: 0; }
.preview-trigger { width: 100%; padding: .25rem; display: grid; gap: .2rem; text-align: left; overflow: hidden; }
.preview-trigger img { display: block; width: 100%; aspect-ratio: 16 / 9; object-fit: contain; border-radius: 5px; background: #edf0f2; }
.preview-trigger:disabled { cursor: default; opacity: 1; }
.preview-caption { color: #66717d; font-size: .76rem; overflow-wrap: anywhere; }
.preview-placeholder { grid-column: 1 / -1; margin: 0; padding: .55rem .65rem; border: 1px dashed #c7ccd3; border-radius: 7px; color: #66717d; font-size: .82rem; overflow-wrap: anywhere; }
.preview-overlay { position: fixed; inset: 0; z-index: 10; display: grid; place-items: center; padding: 1rem; background: rgba(15, 20, 25, .76); }
.preview-overlay[hidden] { display: none; }
.preview-dialog { position: relative; width: min(94vw, 960px); max-height: 94vh; padding: 2.6rem .85rem .85rem; border-radius: 10px; background: #fff; box-shadow: 0 8px 40px rgba(0, 0, 0, .35); }
.preview-dialog img { display: block; width: 100%; max-height: 76vh; object-fit: contain; background: #edf0f2; }
.preview-trigger img[hidden], .preview-dialog img[hidden] { display: none; }
.preview-dialog-close { position: absolute; top: .55rem; right: .6rem; }
.preview-dialog-message { margin: .55rem 0 0; color: #66717d; overflow-wrap: anywhere; }
.segment-actions { display: flex; align-items: center; flex-wrap: wrap; gap: .55rem; margin-top: .65rem; }
.ack-label { color: #795400; font-size: .85rem; margin-right: .2rem; }
.manual-project-form { margin: .65rem 0; padding: .7rem; border: 1px solid #e2bb55; border-radius: 8px; background: #fffaf0; }
.manual-project-form[hidden] { display: none; }
.manual-project-grid { display: grid; grid-template-columns: minmax(0, 1fr) minmax(10rem, 13rem); gap: .6rem; align-items: end; }
.manual-project-preview { margin: .55rem 0 0; color: #5e4300; font-size: .84rem; overflow-wrap: anywhere; }
.manual-project-error { min-height: 1.2rem; margin: .35rem 0 0; color: #8d2f23; font-size: .84rem; overflow-wrap: anywhere; }
.manual-project-actions { display: flex; flex-wrap: wrap; gap: .45rem; margin-top: .55rem; }
.pending-project { margin: .45rem 0; padding: .5rem .65rem; border-left: 3px solid #e2bb55; background: #fffaf0; color: #5e4300; font-size: .84rem; overflow-wrap: anywhere; }
.archive-submit-projects, .archive-submit-jobs { display: grid; gap: .45rem; margin: .7rem 0; }
.archive-submit-project, .archive-submit-job { border: 1px solid #dfe3e8; border-radius: 8px; padding: .55rem .65rem; overflow-wrap: anywhere; }
.archive-submit-project strong, .archive-submit-job strong { display: block; }
.archive-submit-job { display: flex; align-items: center; justify-content: space-between; gap: .7rem; flex-wrap: wrap; }
.archive-submit-project .muted, .archive-submit-job-progress { display: block; margin-top: .2rem; }
.archive-submit-job-progress { color: #66717d; font-size: .84rem; }
.archive-submit-options fieldset { border: 1px solid #d9dfe5; border-radius: .65rem; margin: .7rem 0; padding: .65rem; }
.archive-submit-option { display: block; margin: .4rem 0; line-height: 1.65; }
.archive-submit-option input { margin-right: .35rem; }
.archive-submit-actions { display: flex; flex-wrap: wrap; gap: .5rem; margin-top: .65rem; }
.archive-state-summary { display: flex; flex-wrap: wrap; gap: .45rem 1rem; margin: .55rem 0 0; color: #4f5a66; font-size: .87rem; }
.archive-history { margin-top: .7rem; }
.archive-history-list { display: grid; gap: .45rem; margin-top: .55rem; }
.archive-history-item { border: 1px solid #dfe3e8; border-radius: 8px; padding: .55rem .65rem; overflow-wrap: anywhere; }
.archive-history-item strong, .archive-history-item span { display: block; }
.archive-history-more { margin-top: .55rem; }
.material-triage { display: grid; gap: .65rem; margin: .8rem 0; }
.material-triage details { border: 1px solid #dfe3e8; border-radius: 9px; padding: .55rem .7rem; background: #fbfcfd; }
.material-triage summary { font-weight: 650; }
.material-triage-list { display: grid; gap: .45rem; margin: .6rem 0 0; }
.material-triage-group { border-top: 1px solid #edf0f2; padding-top: .45rem; }
.material-triage-group strong, .material-triage-group span { display: block; overflow-wrap: anywhere; }
.material-triage-action { display: flex; align-items: center; flex-wrap: wrap; gap: .5rem; margin-top: .55rem; }
.material-triage-note { color: #66717d; font-size: .84rem; margin: .45rem 0 0; }
.material-triage-warning { color: #8b5b00; font-size: .84rem; }
.empty { color: #66717d; text-align: center; padding: 1.3rem; }
details summary { cursor: pointer; color: #4f5a66; }
@media (max-width: 640px) { .page { padding: .75rem; } .panel { padding: .8rem; } .segment-head { display: block; } .merge-check { margin-bottom: .45rem; } .segment-actions button { flex: 1 1 auto; } .unit-preview { margin-left: 0; } .preview-grid { gap: .3rem; } .preview-trigger { padding: .18rem; } .preview-caption { font-size: .7rem; } .manual-project-grid { grid-template-columns: 1fr; } }
"""


def _json_value(value: Any) -> Any:
    """Convert a model into JSON-safe primitives without invoking repr()."""

    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_json_value(item) for item in value]
    return str(value)


def _json_native(value: Any) -> bool:
    """Recognize immutable JSON leaves and native containers without cloning them."""
    if value is None or type(value) in (str, int, float, bool):
        return True
    if type(value) is dict:
        return all(type(key) is str and _json_native(item) for key, item in value.items())
    if type(value) in (list, tuple):
        return all(_json_native(item) for item in value)
    return False


def _embedded_json(model: Mapping[str, Any]) -> str:
    # Escaping angle brackets prevents a user supplied value from ending the
    # application/json element and introducing a second script element.
    encoded = json.dumps(model if _json_native(model) else _json_value(model), ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return encoded.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


_PREVIEW_PATH = re.compile(r"(?:\.\./\.\./)?video-previews/[0-9a-fA-F]{64}\.jpg")
_PREVIEW_STATES = {"ready", "error", "pending"}
_PREVIEW_SOURCES = {"主视频", "代理视频"}


def _review_previews(previews: Any) -> dict[str, dict[str, Any]]:
    """Keep only the small, local preview contract exposed to the browser."""

    if not isinstance(previews, Mapping):
        return {}
    result: dict[str, dict[str, Any]] = {}
    for raw_unit_id, raw_preview in previews.items():
        if not isinstance(raw_preview, Mapping):
            continue
        unit_id = str(raw_unit_id)
        state = raw_preview.get("state")
        source_label = raw_preview.get("source_label")
        source_name = raw_preview.get("source_name")
        message = raw_preview.get("message")
        entry: dict[str, Any] = {
            "state": state if isinstance(state, str) and state in _PREVIEW_STATES else "pending",
            "source_label": source_label if isinstance(source_label, str) and source_label in _PREVIEW_SOURCES else "主视频",
            "source_name": "未提供",
            "message": str(message) if message is not None else "",
            "frames": [],
        }
        if isinstance(source_name, str) and 1 <= len(source_name) <= 255 and "/" not in source_name and "\\" not in source_name and not any(ord(char) < 32 for char in source_name):
            entry["source_name"] = source_name
        raw_frames = raw_preview.get("frames")
        if isinstance(raw_frames, Sequence) and not isinstance(raw_frames, (str, bytes, bytearray)):
            for raw_frame in raw_frames[:3]:
                if not isinstance(raw_frame, Mapping):
                    continue
                src = raw_frame.get("src")
                time_seconds = raw_frame.get("time_seconds")
                if not isinstance(src, str) or not _PREVIEW_PATH.fullmatch(src):
                    continue
                if isinstance(time_seconds, bool) or not isinstance(time_seconds, (int, float)) or not math.isfinite(time_seconds) or time_seconds < 0:
                    continue
                entry["frames"].append({"src": src, "time_seconds": float(time_seconds)})
        result[unit_id] = entry
    return result


def _load_script() -> str:
    try:
        return Path(__file__).with_name("review_ui.js").read_text(encoding="utf-8")
    except OSError:
        # Keep the output syntactically self-contained if a partial package is
        # copied without the sibling asset; normal source distributions include it.
        return "document.getElementById('review-status').textContent = '交互脚本缺失，页面未初始化。';"


def _load_submit_script() -> str:
    try:
        return Path(__file__).with_name("archive_submit_ui.js").read_text(encoding="utf-8")
    except OSError:
        return ""


def _material_state_for(model: Mapping[str, Any], material_state: Mapping[str, Any] | None) -> Mapping[str, Any]:
    """Resolve the local material routing contract without changing ``model``."""

    if material_state is not None:
        return material_state if isinstance(material_state, Mapping) else {}
    try:
        from .material_triage import build_material_state

        generated = build_material_state(model)
        return generated if isinstance(generated, Mapping) else {}
    except Exception:
        # Older partial installs do not have the optional classifier yet.  The
        # review page remains usable and keeps the original segment model.
        return {}


def render_review(model: dict, previews: Mapping[str, Any] | None = None, *, project_root_label: str = "拍摄项目", submission_enabled: bool = False, photo_previews_enabled: bool = False, move_enabled: bool = False, archive_state: Mapping[str, Any] | None = None, material_state: Mapping[str, Any] | None = None, discovery_state: Mapping[str, Any] | None = None, workflow_state: Mapping[str, Any] | None = None, help_enabled: bool = False, photo_preview_state: Mapping[str, Any] | None = None, _byte_output: bool = False) -> str | bytes:
    """Return a self-contained local confirmation UI for a review model."""

    root = model if isinstance(model, Mapping) else {}
    projects = root.get("projects") if isinstance(root.get("projects"), Sequence) and not isinstance(root.get("projects"), (str, bytes, bytearray)) else []
    units = root.get("units") if isinstance(root.get("units"), Sequence) and not isinstance(root.get("units"), (str, bytes, bytearray)) else []
    excluded = root.get("excluded_batches") if isinstance(root.get("excluded_batches"), Sequence) and not isinstance(root.get("excluded_batches"), (str, bytes, bytearray)) else []
    report_id = str(root.get("report_id") or "未提供")
    snapshot_at = str(root.get("snapshot_at") or "未提供")
    presentation = root.get('presentation')
    total = presentation.get('total_units') if isinstance(presentation, Mapping) else None
    if isinstance(total, bool) or not isinstance(total, int) or total < len(units):
        total = len(units)
    summary = f"素材单元 {total} 个 · 每段仅列候选项目 · 快照非实时"
    safe_project_root_label = str(project_root_label or "拍摄项目").strip()[:200] or "拍摄项目"
    resolved_material_state = _material_state_for(root, material_state)
    settings = {"project_root_label": safe_project_root_label, "submission_enabled": bool(submission_enabled), "move_enabled": bool(move_enabled), "photo_previews_enabled": bool(photo_previews_enabled), "archive_state": _json_value(archive_state) if isinstance(archive_state, Mapping) else {}, "material_state": _json_value(resolved_material_state)}
    if isinstance(photo_preview_state, Mapping):
        settings['photo_preview_state'] = _json_value(photo_preview_state)
    if isinstance(discovery_state, Mapping):
        settings['discovery_state'] = _json_value(discovery_state)
    if submission_enabled:
        material_units = resolved_material_state.get('units', {}) if isinstance(resolved_material_state, Mapping) else {}
        routes = {}
        for unit in units:
            if not unit.get('files'):
                continue
            route_unit = unit
            material = material_units.get(str(unit.get('unit_id'))) if isinstance(material_units, Mapping) else None
            if isinstance(material, Mapping) and material.get('category') == 'shoot':
                warnings = [warning for warning in unit.get('warnings', []) if warning != '中转文件已不在原路径，请查看归档记录或核对来源']
                route_unit = dict(unit)
                route_unit['warnings'] = warnings
            try:
                routes[unit['unit_id']] = route_view(route_unit)
            except (KeyError, TypeError, ValueError):
                continue
        settings['archive_routes'] = routes
    if isinstance(workflow_state, Mapping):
        settings['workflow_state'] = _json_value(workflow_state)
    eyebrow = "木梨素材分类 · 归档工作台" if submission_enabled else "木梨素材分类 · 本地离线确认"
    initial_status = "先确认素材归属，再提交归档。" if submission_enabled else "页面只保存本地计划；未执行任何媒体操作。"
    jump_submit = '<button id="jump-submit" class="secondary" type="button">查看本次提交</button>' if submission_enabled else ""
    if submission_enabled:
        local_plan = '''<section class="panel"><details><summary>保存草稿与备用导出</summary><p class="muted">可保存本地草稿或导出备用计划；提交归档请使用上方归档工作台。</p><div id="draft-recovery" hidden><h3>旧报告草稿</h3><div id="draft-recovery-list"></div><div id="draft-recovery-preview" hidden></div></div><div id="draft-status" class="notice" role="status" aria-live="polite" aria-atomic="true" hidden></div><div class="toolbar-actions"><button id="save-draft" type="button">保存草稿</button><button id="restore-draft" type="button">恢复本报告草稿</button><button id="recover-other-drafts" type="button">查找旧报告草稿</button><button id="export-plan" class="primary" type="button">导出归档计划（不执行）</button></div></details></section>'''
    else:
        local_plan = '''<section class="panel"><h2>本地计划</h2><p class="muted">导出的是分类确认计划，不会移动原片、不执行归档，也不表示允许真实写入。</p><div id="draft-recovery" hidden><h3>旧报告草稿</h3><div id="draft-recovery-list"></div><div id="draft-recovery-preview" hidden></div></div><div id="draft-status" class="notice" role="status" aria-live="polite" aria-atomic="true" hidden></div><div class="toolbar-actions"><button id="save-draft" type="button">保存草稿</button><button id="restore-draft" type="button">恢复本报告草稿</button><button id="recover-other-drafts" type="button">查找旧报告草稿</button><button id="export-plan" class="primary" type="button">导出归档计划（不执行）</button></div></section>'''
    # Excluded batch values are added by the UI with textContent.
    excluded_items = ""
    html = f'''<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>按拍摄段确认项目</title>
<style>{_CSS}</style>
</head>
<body>
<main class="page">
  <header class="topbar">
    <div><p class="eyebrow">{eyebrow}</p><h1>按拍摄段确认项目</h1><p class="meta">{summary}</p></div>
    <div class="meta">报告：<code id="report-id"></code><br>快照：<span id="snapshot-at"></span></div>
    {'<a class="help-link" href="/help/" target="_blank" rel="noopener" aria-label="使用帮助（新标签页）"><span class="help-icon" aria-hidden="true">?</span>使用帮助</a>' if help_enabled else ""}
  </header>
  <section id="workflow-panel" class="panel" hidden><h2>拷贝 → 校验交接 → 确认项目 → 归档</h2><p><a id="workflow-back">拷贝备份</a></p><label class="field"><span>定位拷贝批次</span><select id="workflow-batch"><option value="">全部批次</option></select></label><p id="workflow-progress" role="status"></p><p class="muted">批次定位只筛选显示，不改变归档清单。已归档素材默认隐藏；提交前仍需确认项目并核对完整清单。</p></section>
  <div id="example-banner" class="example-banner" hidden>这是合成演示数据（example_data=true），不能代表真实媒体或真实归档结果。</div>
  <div id="review-status" class="notice">{initial_status}</div>
  <section id="discovery-panel" class="panel" hidden><h2>新拷贝素材</h2><p class="muted">校验通过后进入待归档拍摄段；本区每 5 秒更新进度。</p><div id="discovery-batches"></div><p id="discovery-message" class="muted"></p><button id="discovery-reload" type="button" hidden>保存当前草稿并查看新素材</button></section>
  <section id="archive-state-panel" class="panel" hidden>
    <h2>归档状态</h2>
    <div id="archive-state-summary" class="archive-state-summary"></div>
    <details id="archive-history" class="archive-history">
      <summary id="archive-history-summary">已归档历史</summary>
      <div id="archive-history-list" class="archive-history-list"></div>
      <button id="archive-history-previous" class="secondary archive-history-more" type="button" hidden>上一页</button>
      <button id="archive-history-more" class="secondary archive-history-more" type="button" hidden>显示更多已归档记录</button>
    </details>
  </section>
  <section class="panel">
    <h2>当前状态</h2><p id="summary" class="summary"></p>
    <div class="toolbar">
      <label class="field"><span>按拍摄日期筛选</span><select id="date-filter"><option value="">全部日期</option></select></label>
      <label class="field"><span>搜索本段候选项目</span><input id="project-search" type="search" placeholder="在各段候选中搜索项目名、销售编号或日期"></label>
      <div class="toolbar-actions"><button id="split-units" type="button" disabled>按选中素材拆分拍摄段</button><button id="merge-segments" type="button" disabled>合并选中拍摄段</button>{jump_submit}</div>
    </div>
  </section>
  <section id="excluded-box" class="panel" hidden><h2>已排除批次</h2><ul id="excluded-batches">{excluded_items}</ul></section>
  <section class="panel"><h2>拍摄段</h2><div id="material-triage" class="material-triage" hidden></div><div id="timeline"></div></section>
  {local_plan}
</main>
<script id="review-model" type="application/json">\x00MULI_MODEL_JSON\x00</script>
<script id="review-previews" type="application/json">\x00MULI_PREVIEWS_JSON\x00</script>
<script id="review-settings" type="application/json">\x00MULI_SETTINGS_JSON\x00</script>
<script id="photo-preview-ready" type="application/json">\x00MULI_PHOTO_STATE_JSON\x00</script>
<script>{Path(__file__).with_name('review_projection_ui.js').read_text(encoding='utf-8')}</script>
<script>{Path(__file__).with_name("photo_sampling.js").read_text(encoding="utf-8")}</script>
<script>{_load_script()}</script>
{('<script>' + _load_submit_script() + '</script>') if submission_enabled else ''}
{('<script>' + Path(__file__).with_name('discovery_ui.js').read_text(encoding='utf-8') + '</script>') if submission_enabled else ''}
{('<script>' + Path(__file__).with_name('workflow_ui.js').read_text(encoding='utf-8') + '</script>') if workflow_state else ''}
</body>
</html>'''
    payloads = {"\x00MULI_MODEL_JSON\x00": root,
                "\x00MULI_PREVIEWS_JSON\x00": _review_previews(previews),
                "\x00MULI_SETTINGS_JSON\x00": settings,
                "\x00MULI_PHOTO_STATE_JSON\x00": photo_preview_state or {}}
    parts = re.split("(\x00MULI_(?:MODEL|PREVIEWS|SETTINGS|PHOTO_STATE)_JSON\x00)", html)
    if not _byte_output:
        return ''.join(_embedded_json(payloads[part]) if part in payloads else part for part in parts)
    # Queue publication needs UTF-8 bytes, not a second giant Unicode document.
    # Keep JSON escaping identical, while encoding bounded fragments directly.
    output = BytesIO()
    encoder = json.JSONEncoder(ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    for part in parts:
        if part not in payloads:
            output.write(part.encode())
            continue
        value = payloads[part]
        value = value if _json_native(value) else _json_value(value)
        for fragment in encoder.iterencode(value):
            output.write(fragment.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026").encode())
    return output.getvalue()


def render_review_bytes(model, previews=None, **options):
    """Render the identical page directly to UTF-8 for the queue publisher."""
    return render_review(model, previews, _byte_output=True, **options)


__all__ = ["render_review", "render_review_bytes"]
