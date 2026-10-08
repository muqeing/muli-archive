"""Render a self-contained, read-only preview for a sorter report.

The renderer deliberately has no filesystem, network, or template dependencies.
Report values are only inserted through HTML escaping; the small amount of
JavaScript operates on the resulting DOM and never evaluates report data.
"""

from __future__ import annotations

import html
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from numbers import Integral, Real
from typing import Any
from zoneinfo import ZoneInfo


_BATCH_STATUS_LABELS = {
    "verified": "已核验",
    "waiting": "等待拷贝完成",
    "blocked": "已阻塞",
}
_KIND_LABELS = {
    "photo": "照片",
    "video": "主视频",
    "audio": "录音",
    "proxy_only": "代理视频",
    "auxiliary": "辅助文件",
}
_GROUP_STATUS_LABELS = {
    "ready": "候选明确",
    "review": "待确认",
    "auxiliary": "辅助文件",
}


def _text(value: Any, default: str = "未提供") -> str:
    """Return a display string without allowing surprising object reprs."""

    if value is None:
        return default
    if isinstance(value, (str, int, float)) and not isinstance(value, bool):
        return str(value)
    return default


def _items(value: Any) -> list[Any]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return list(value)
    return []


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _integer(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, Integral):
        return int(value)
    if isinstance(value, Real):
        return int(value)
    return default


def _reason_list(value: Any) -> list[str]:
    return [_text(item, "") for item in _items(value) if _text(item, "")]


def _esc(value: Any, default: str = "未提供") -> str:
    return html.escape(_text(value, default), quote=True)


def _esc_attr(value: str) -> str:
    return html.escape(value, quote=True)


def _human_bytes(value: Any) -> str:
    amount = max(0, _integer(value))
    units = ("B", "KB", "MB", "GB", "TB")
    number = float(amount)
    unit = units[0]
    for unit in units:
        if number < 1024 or unit == units[-1]:
            break
        number /= 1024
    if unit == "B":
        return f"{amount:,} B"
    return f"{number:.1f} {unit}"


def _status_class(status: str, prefix: str) -> str:
    allowed = {
        "batch": set(_BATCH_STATUS_LABELS),
        "group": set(_GROUP_STATUS_LABELS),
    }[prefix]
    return status if status in allowed else "unknown"


def _status_label(status: str, labels: Mapping[str, str]) -> str:
    return labels.get(status, "未标记")


def _kind_label(kind: str) -> str:
    return _KIND_LABELS.get(kind, kind if kind else "未提供")


def _snapshot_time(value: Any) -> str:
    """Format an ISO timestamp as a non-live Shanghai-time snapshot label."""

    raw = _text(value, "")
    if not raw:
        return "未提供"
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return raw
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S（北京时间）")


def _reason_html(reasons: list[str], label: str = "待确认理由") -> str:
    if not reasons:
        return ""
    content = "".join(f'<li>{_esc(reason, "")}</li>' for reason in reasons)
    return f'<div class="reasons"><strong>{_esc(label)}</strong><ul>{content}</ul></div>'


def _candidate_html(candidate: Mapping[str, Any]) -> str:
    name = _text(candidate.get("name"), "未命名项目")
    order_id = _text(candidate.get("order_id"), "")
    path = _text(candidate.get("path"), "未提供")
    evidence = _reason_list(candidate.get("evidence"))
    order_html = f'<div class="candidate-order">销售编号：{_esc(order_id)}</div>' if order_id else ""
    evidence_html = ""
    if evidence:
        evidence_html = (
            '<ul class="evidence">'
            + "".join(f"<li>{_esc(item, '')}</li>" for item in evidence)
            + "</ul>"
        )
    return (
        '<li class="candidate">'
        f'<div class="candidate-title">{_esc(name, "未命名项目")}</div>'
        f"{order_html}"
        f'<div class="candidate-path">目标路径：<code>{_esc(path)}</code></div>'
        f"{evidence_html}"
        "</li>"
    )


def _group_html(group: Mapping[str, Any], batch_status: str) -> str:
    group_id = _text(group.get("group_id"), "未命名分组")
    status = _text(group.get("status"), "review")
    status_class = _status_class(status, "group")
    status_label = _status_label(status, _GROUP_STATUS_LABELS)
    file_names = [_text(item, "未命名文件") for item in _items(group.get("file_names"))]
    candidates = [_mapping(item) for item in _items(group.get("candidates"))]
    candidate_html = (
        "".join(_candidate_html(candidate) for candidate in candidates)
        if candidates
        else '<li class="muted">暂无项目候选</li>'
    )
    file_html = (
        "".join(f"<li><code>{_esc(name, '')}</code></li>" for name in file_names)
        if file_names
        else '<li class="muted">暂无文件名</li>'
    )
    reasons = _reason_list(group.get("reasons"))
    capture_date = _text(group.get("capture_date"), "未提供")
    device = _text(group.get("device"), "未提供")
    kind = _text(group.get("kind"), "未提供")
    kind_label = _kind_label(kind)
    search_text = " ".join(
        [
            group_id,
            capture_date,
            device,
            kind,
            status_label,
            *reasons,
            *file_names,
            *[
                " ".join(
                    [
                        _text(candidate.get("name"), ""),
                        _text(candidate.get("order_id"), ""),
                        _text(candidate.get("path"), ""),
                        *(_reason_list(candidate.get("evidence"))),
                    ]
                )
                for candidate in candidates
            ],
        ]
    )
    return f"""
      <article class="group-card" data-filter-item data-status="{_esc_attr(status)}"
          data-batch-status="{_esc_attr(batch_status)}" data-search="{_esc_attr(search_text)}">
        <div class="group-heading">
          <div>
            <p class="eyebrow">候选分组</p>
            <h3>{_esc(capture_date)} · {_esc(kind_label)}</h3>
            <p class="trace-id">追踪 ID：<code>{_esc(group_id, '未命名分组')}</code></p>
          </div>
          <span class="status status-{_esc_attr(status_class)}">{_esc(status_label)}</span>
        </div>
        <dl class="facts">
          <div><dt>拍摄日期</dt><dd>{_esc(capture_date)}</dd></div>
          <div><dt>设备</dt><dd>{_esc(device)}</dd></div>
          <div><dt>素材类型</dt><dd>{_esc(kind_label)}</dd></div>
          <div><dt>文件数</dt><dd>{_integer(group.get('file_count')):,}</dd></div>
          <div><dt>容量</dt><dd>{_human_bytes(group.get('bytes'))}</dd></div>
        </dl>
        {_reason_html(reasons)}
        <section class="subsection">
          <h4>项目候选</h4>
          <ul class="candidate-list">{candidate_html}</ul>
        </section>
        <details class="file-list">
          <summary>查看文件名（{len(file_names):,}）</summary>
          <ul>{file_html}</ul>
        </details>
      </article>
    """


def _batch_html(batch: Mapping[str, Any]) -> str:
    batch_id = _text(batch.get("batch_id"), "未命名批次")
    status = _text(batch.get("status"), "waiting")
    status_class = _status_class(status, "batch")
    status_label = _status_label(status, _BATCH_STATUS_LABELS)
    reasons = _reason_list(batch.get("reasons"))
    groups = [_mapping(item) for item in _items(batch.get("groups"))]
    file_count = _integer(batch.get("file_count"))
    file_count_label = f"{file_count:,}" if batch.get("file_count") is not None else "未读取"
    search_text = " ".join([batch_id, status_label, *reasons])
    groups_html = "".join(_group_html(group, status) for group in groups)
    if not groups:
        groups_html = '<p class="empty">这个批次还没有候选分组。</p>'
    return f"""
      <section class="batch-card" data-batch data-filter-item data-status="{_esc_attr(status)}"
          data-search="{_esc_attr(search_text)}">
        <div class="batch-heading">
          <div>
            <p class="eyebrow">批次</p>
            <h2>{_esc(batch_id, '未命名批次')}</h2>
          </div>
          <span class="status status-{_esc_attr(status_class)}">{_esc(status_label)}</span>
        </div>
        <div class="batch-meta"><span>文件数 {file_count_label}</span><span>候选分组 {len(groups):,}</span></div>
        {_reason_html(reasons, '检查结果')}
        <div class="group-list">{groups_html}</div>
      </section>
    """


def _summary_value(summary: Mapping[str, Any], key: str) -> int:
    return _integer(summary.get(key))


def render_report(report: dict) -> str:
    """Return a self-contained offline HTML preview for ``report``.

    Missing fields are rendered as neutral defaults. This function does not
    inspect paths, read media, or imply that any archive or confirmation ran.
    """

    root = _mapping(report)
    summary = _mapping(root.get("summary"))
    batches = [_mapping(item) for item in _items(root.get("batches"))]
    limitations = _reason_list(root.get("limitations"))
    generated_at = _text(root.get("generated_at"), "未提供")
    mode = _text(root.get("mode"), "read_only_preview")
    batch_html = "".join(_batch_html(batch) for batch in batches)
    if not batch_html:
        batch_html = '<p class="empty empty-page">当前没有可预览的批次。</p>'
    limitation_html = "".join(f"<li>{_esc(item, '')}</li>" for item in limitations)
    if not limitation_html:
        limitation_html = "<li>暂无补充限制</li>"

    # The document intentionally contains no external URLs, script src, or
    # report JSON script block. All report data is rendered as escaped text.
    return f'''<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>木梨素材分类 · 只读预览</title>
  <style>
    :root {{
      color-scheme: light;
      --ink: #233044;
      --muted: #667085;
      --line: #dfe5ec;
      --panel: #ffffff;
      --canvas: #f5f7fa;
      --accent: #275dad;
      --accent-soft: #eaf1ff;
      --green: #18794e;
      --green-soft: #e8f6ee;
      --amber: #93620b;
      --amber-soft: #fff4d6;
      --red: #a33c35;
      --red-soft: #fdecea;
    }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; background: var(--canvas); color: var(--ink); font: 15px/1.6 -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif; }}
    .shell {{ width: min(1120px, calc(100% - 32px)); margin: 0 auto; padding: 34px 0 56px; }}
    .hero {{ display: flex; flex-wrap: wrap; gap: 16px; align-items: flex-start; justify-content: space-between; margin-bottom: 24px; }}
    .eyebrow {{ margin: 0 0 4px; color: var(--muted); font-size: 12px; letter-spacing: .08em; text-transform: uppercase; }}
    h1, h2, h3, h4 {{ margin: 0; line-height: 1.3; }}
    h1 {{ font-size: clamp(24px, 4vw, 34px); letter-spacing: -.02em; }}
    h2 {{ font-size: 20px; }}
    h3 {{ font-size: 17px; }}
    h4 {{ font-size: 14px; }}
    .hero-copy {{ min-width: 240px; }}
    .hero-copy p:last-child {{ margin: 9px 0 0; color: var(--muted); }}
    .readonly-badge {{ display: inline-flex; border: 1px solid #b9cdf4; border-radius: 999px; padding: 7px 12px; color: var(--accent); background: var(--accent-soft); font-weight: 700; white-space: nowrap; }}
    .generated {{ margin: 6px 0 0; color: var(--muted); font-size: 13px; text-align: right; }}
    .summary-grid {{ display: grid; grid-template-columns: repeat(6, minmax(0, 1fr)); gap: 10px; margin-bottom: 18px; }}
    .metric, .notice, .toolbar, .batch-card {{ border: 1px solid var(--line); border-radius: 14px; background: var(--panel); box-shadow: 0 4px 18px rgba(29, 47, 74, .04); }}
    .metric {{ padding: 15px; }}
    .metric-label {{ color: var(--muted); font-size: 13px; }}
    .metric-value {{ display: block; margin-top: 5px; font-size: 25px; font-weight: 750; }}
    .notice {{ margin-bottom: 18px; padding: 14px 16px; background: #fffdf7; }}
    .notice h2 {{ font-size: 15px; }}
    .notice ul, .reasons ul, .evidence, .candidate-list, .file-list ul {{ margin: 7px 0 0; padding-left: 19px; }}
    .notice li, .reasons li, .evidence li {{ color: #596273; }}
    .toolbar {{ display: flex; flex-wrap: wrap; gap: 10px; align-items: center; margin-bottom: 18px; padding: 12px; }}
    .toolbar label {{ color: var(--muted); font-size: 13px; font-weight: 650; }}
    input, select {{ min-height: 40px; border: 1px solid #cfd7e2; border-radius: 9px; padding: 8px 11px; color: var(--ink); background: #fff; font: inherit; }}
    input {{ flex: 1 1 300px; }}
    select {{ min-width: 170px; }}
    input:focus, select:focus {{ outline: 3px solid #d7e4ff; border-color: var(--accent); }}
    .result-count {{ margin-left: auto; color: var(--muted); font-size: 13px; }}
    .batch-card {{ margin-bottom: 18px; padding: 20px; }}
    .batch-heading, .group-heading {{ display: flex; flex-wrap: wrap; gap: 14px; align-items: flex-start; justify-content: space-between; }}
    .batch-heading > div, .group-heading > div {{ min-width: 0; overflow-wrap: anywhere; }}
    .batch-meta {{ display: flex; flex-wrap: wrap; gap: 14px; margin-top: 8px; color: var(--muted); font-size: 13px; }}
    .status {{ display: inline-flex; flex: none; border-radius: 999px; padding: 4px 9px; font-size: 12px; font-weight: 700; }}
    .status-verified, .status-ready {{ color: var(--green); background: var(--green-soft); }}
    .status-waiting, .status-review {{ color: var(--amber); background: var(--amber-soft); }}
    .status-blocked {{ color: var(--red); background: var(--red-soft); }}
    .status-auxiliary, .status-unknown {{ color: var(--muted); background: #eef1f5; }}
    .reasons {{ margin-top: 14px; border-left: 3px solid #e3b84f; padding: 8px 0 8px 11px; }}
    .reasons strong {{ font-size: 13px; }}
    .group-list {{ display: grid; gap: 12px; margin-top: 18px; }}
    .group-card {{ border: 1px solid var(--line); border-radius: 11px; padding: 16px; background: #fcfdff; }}
    .facts {{ display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 9px; margin: 15px 0 0; }}
    .facts div {{ min-width: 0; border-radius: 8px; padding: 8px 10px; background: #f1f4f8; }}
    dt {{ color: var(--muted); font-size: 12px; }}
    dd {{ overflow-wrap: anywhere; margin: 2px 0 0; font-weight: 650; }}
    .subsection {{ margin-top: 16px; }}
    .candidate-list {{ margin-top: 8px; }}
    .candidate {{ margin: 8px 0; padding-left: 2px; }}
    .candidate-title {{ font-weight: 700; }}
    .candidate-order, .candidate-path, .trace-id {{ color: var(--muted); font-size: 13px; overflow-wrap: anywhere; }}
    .evidence {{ margin-top: 3px; }}
    code {{ overflow-wrap: anywhere; color: #46536a; font: 12px/1.5 ui-monospace, SFMono-Regular, Menlo, monospace; }}
    .file-list {{ margin-top: 14px; border-top: 1px solid var(--line); padding-top: 11px; }}
    summary {{ cursor: pointer; color: var(--accent); font-size: 13px; font-weight: 650; }}
    .file-list li {{ overflow-wrap: anywhere; color: #596273; }}
    .muted, .empty {{ color: var(--muted); }}
    .empty {{ margin: 0; padding: 14px 0; }}
    .empty-page {{ border: 1px dashed var(--line); border-radius: 12px; padding: 28px; text-align: center; background: var(--panel); }}
    [hidden] {{ display: none !important; }}
    @media (max-width: 850px) {{ .summary-grid {{ grid-template-columns: repeat(3, minmax(0, 1fr)); }} .facts {{ grid-template-columns: repeat(3, minmax(0, 1fr)); }} }}
    @media (max-width: 560px) {{ .shell {{ width: min(100% - 20px, 1120px); padding-top: 22px; }} .summary-grid {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }} .facts {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }} .batch-card {{ padding: 15px; }} .result-count {{ width: 100%; margin-left: 0; }} .generated {{ text-align: left; }} }}
  </style>
</head>
<body>
  <main class="shell">
    <header class="hero">
      <div class="hero-copy">
        <p class="eyebrow">木梨素材分类</p>
        <h1>独立素材分类预览</h1>
        <p>读取清单与文件属性，展示项目候选；原片保持原样。</p>
      </div>
      <div>
        <div class="readonly-badge">只读预览 · 未归档</div>
        <p class="generated">快照时间（非实时）：{_esc(_snapshot_time(generated_at))}</p>
      </div>
    </header>
    <section class="summary-grid" aria-label="批次摘要">
      <div class="metric"><span class="metric-label">批次</span><strong class="metric-value">{_summary_value(summary, 'batches'):,}</strong></div>
      <div class="metric"><span class="metric-label">已核验批次</span><strong class="metric-value">{_summary_value(summary, 'verified_batches'):,}</strong></div>
      <div class="metric"><span class="metric-label">等待或阻断批次</span><strong class="metric-value">{_summary_value(summary, 'blocked_batches'):,}</strong></div>
      <div class="metric"><span class="metric-label">候选分组</span><strong class="metric-value">{_summary_value(summary, 'groups'):,}</strong></div>
      <div class="metric"><span class="metric-label">候选明确</span><strong class="metric-value">{_summary_value(summary, 'ready_groups'):,}</strong></div>
      <div class="metric"><span class="metric-label">待确认分组</span><strong class="metric-value">{_summary_value(summary, 'review_groups'):,}</strong></div>
    </section>
    <section class="notice" aria-labelledby="limitations-title">
      <h2 id="limitations-title">当前限制</h2>
      <ul>{limitation_html}</ul>
    </section>
    <section class="toolbar" aria-label="筛选预览内容">
      <label for="search">搜索</label>
      <input id="search" type="search" autocomplete="off" placeholder="搜索批次、项目、路径或文件名">
      <label for="status">状态</label>
      <select id="status">
        <option value="all">全部状态</option>
        <option value="verified">批次 · 已核验</option>
        <option value="waiting">批次 · 等待拷贝完成</option>
        <option value="blocked">批次 · 已阻塞</option>
        <option value="ready">分组 · 候选明确</option>
        <option value="review">分组 · 待确认</option>
        <option value="auxiliary">分组 · 辅助文件</option>
      </select>
      <span class="result-count" id="result-count" aria-live="polite">显示 {len(batches):,} 个批次</span>
    </section>
    <section id="batches" aria-label="分类批次">
      {batch_html}
    </section>
  </main>
  <script>
    (() => {{
      const search = document.getElementById('search');
      const status = document.getElementById('status');
      const resultCount = document.getElementById('result-count');
      const batches = Array.from(document.querySelectorAll('[data-batch]'));

      function applyFilters() {{
        const query = search.value.trim().toLocaleLowerCase();
        const selectedStatus = status.value;
        const batchStatuses = new Set(['verified', 'waiting', 'blocked']);
        const filteringBatches = batchStatuses.has(selectedStatus);
        let visibleBatches = 0;
        let visibleGroups = 0;
        batches.forEach((batch) => {{
          const batchText = (batch.dataset.search || '').toLocaleLowerCase();
          const batchStatus = batch.dataset.status || '';
          const batchTextMatch = !query || batchText.includes(query);
          const batchStatusMatch = !filteringBatches || batchStatus === selectedStatus;
          let batchHasVisibleGroup = false;
          batch.querySelectorAll('.group-card').forEach((group) => {{
            const groupText = (group.dataset.search || '').toLocaleLowerCase();
            const groupStatus = group.dataset.status || '';
            const groupBatchStatus = group.dataset.batchStatus || '';
            const textMatch = !query || groupText.includes(query) || batchTextMatch;
            const statusMatch = selectedStatus === 'all' || groupStatus === selectedStatus || groupBatchStatus === selectedStatus;
            const showGroup = textMatch && statusMatch;
            group.hidden = !showGroup;
            if (showGroup) {{
              batchHasVisibleGroup = true;
              visibleGroups += 1;
            }}
          }});
          const showEmptyBatch = (selectedStatus === 'all' || filteringBatches) && batchTextMatch && !batch.querySelector('.group-card');
          const showBatch = batchStatusMatch && (batchHasVisibleGroup || showEmptyBatch);
          batch.hidden = !showBatch;
          if (showBatch) visibleBatches += 1;
        }});
        resultCount.textContent = `显示 ${{visibleBatches}} 个批次 · ${{visibleGroups}} 个分组`;
      }}

      search.addEventListener('input', applyFilters);
      status.addEventListener('change', applyFilters);
      applyFilters();
    }})();
  </script>
</body>
</html>'''
