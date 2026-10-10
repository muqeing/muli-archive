"""Attach the current review/file location to errors without extra IO.

Context stays on the exception, never on the service or a shared thread-local.
An outer unit/project context fills gaps; an inner exact file takes precedence.
"""
from contextlib import contextmanager


@contextmanager
def diagnostic_context(**location):
    try:
        yield
    except (OSError, ValueError, KeyError, TypeError) as exc:
        inner = getattr(exc, 'archive_location', {})
        exc.archive_location = {**location, **inner}
        raise


def diagnostic_issues(exc, model=None, decisions=None):
    location = getattr(exc, 'archive_location', {})
    unit_id = location.get('unit_id')
    if not unit_id and location.get('source_path') and isinstance(model, dict):
        unit_id = next((u.get('unit_id') for u in model.get('units', [])
                        if any(f.get('source_path') == location['source_path'] for f in u.get('files', []))), None)
    segment_id = location.get('segment_id')
    project_id = location.get('project_id')
    model = model if isinstance(model, dict) else {}
    decisions = decisions if isinstance(decisions, dict) else {}
    rows = decisions.get('segments')
    rows = rows if isinstance(rows, list) else []
    def members(segment):
        value = segment.get('unit_ids')
        return [uid for uid in value if isinstance(uid, str)] if isinstance(value, list) else []
    segments = [s for s in rows if isinstance(s, dict)
                and ((unit_id and unit_id in members(s))
                     or (segment_id and s.get('segment_id') == segment_id)
                     or (not unit_id and not segment_id and project_id and s.get('decision') == 'confirmed'
                         and s.get('project_id') == project_id))]
    member_ids = {unit_id} if unit_id else {uid for s in segments for uid in members(s)}
    units = [u for u in model.get('units', []) if u.get('unit_id') in member_ids]
    source_path = location.get('source_path')
    files = [{'name':f.get('name', ''), 'source_path':f.get('source_path', '')}
             for u in units for f in u.get('files', [])
             if not source_path or f.get('source_path') == source_path]
    # A file may be absent from the current model (or come from a bound recovery
    # request). The exact observed path is still useful; never guess a unit.
    if source_path and not files:
        files = [{'name':location.get('name', ''), 'source_path':source_path}]
    reason = str(exc)
    mismatch = reason == '计划来自其他快照，不能套用到当前素材'
    return [{'scope': 'file' if source_path else 'unit' if unit_id else 'segment' if segment_id else 'project' if project_id else 'plan',
             'reason':reason,
             'segments':[{'segment_id':s.get('segment_id'), 'label':s.get('label', ''),
                          'project_id':s.get('project_id')} for s in segments],
             'units':[{'unit_id':u.get('unit_id'), 'capture_date':u.get('capture_date'),
                       'capture_time':u.get('capture_time')} for u in units],
             'files':files, 'target_path':location.get('target_path'),
             'changed_signature_fields':location.get('changed_signature_fields', []),
             'project_id':project_id,
             'report_id':decisions.get('report_id') if mismatch else None,
             'current_report_id':model.get('report_id') if mismatch else None,
             'guidance':('这是整份页面计划与后台清单版本不一致，尚未开始检查具体素材，尚未核实哪些素材发生变化。请先保存草稿，重新加载页面，再用“查找旧报告草稿”核对并恢复仍存在的素材归属，重新确认后检查。'
                         if mismatch else '仅定位到当前检查失败的位置，其他素材是否通过尚未判定。')}]
