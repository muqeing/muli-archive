"""Presentation-only pending detail and bounded history; never a compile model.

The canonical report digest and complete decision coverage remain unchanged.
Verified archive/discard identities and request-bound retained copies may lose
file detail here. Retained copies remain separate from verified archive counts.
"""
from .order_feed_io import digest, read_json


def compact_archive(state, project_unit_ids=()):
    entries = state['archived_units']
    current = [row for row in entries if row.get('in_current_model')]
    return {'version': 'archive-summary/1', 'report_id': state['report_id'],
            'archived_unit_ids': [row['unit_id'] for row in current],
            'parent_projects': {row['unit_id']: row['project'] for row in current if row['unit_id'] in project_unit_ids},
            'current_files': sum(row.get('file_count', len(row.get('files', []))) for row in current),
            'history_total': len(entries), 'history_generation': digest(state),
            'warnings': list(state.get('warnings', [])),
            'retained_unit_ids': list(state.get('retained_unit_ids', [])),
            'retained_current_files': state.get('retained_current_files', 0),
            'retained_jobs': list(state.get('retained_jobs', []))}


def pending_view(model, archive, materials):
    hidden = {row['unit_id'] for row in archive['archived_units'] if row.get('in_current_model')}
    hidden.update(archive.get('retained_unit_ids', []))
    hidden.update(uid for uid, row in materials['units'].items() if row.get('category') == 'discarded')
    units, index, roles = [], [], []
    for unit in model['units']:
        uid = unit['unit_id']
        if uid not in hidden:
            units.append(unit)
        else:
            # Required for old draft validation and full, deferred plan coverage.
            index.append([uid, unit.get('kind'), unit.get('capture_date'), unit.get('capture_time'), unit.get('candidate_project_ids', [])])
            row = materials['units'][uid]
            roles.append([row['category'], row.get('parent_unit_id')])
    view = {key: value for key, value in model.items() if key != 'units'}
    view['units'] = units
    view['presentation'] = {'version': 'pending-detail/1', 'total_units': len(model['units']), 'hidden_units': index}
    material_view = {key: value for key, value in materials.items() if key != 'units'}
    material_view['units'] = {unit['unit_id']: materials['units'][unit['unit_id']] for unit in units}
    material_view['hidden_roles'] = roles
    return view, material_view


def history_page(bundle, report_id, generation, offset, limit):
    if report_id != bundle['archive_state']['report_id']:
        raise ValueError('历史记录来自其他报告，请保存草稿后刷新页面')
    if generation and generation != bundle['history_generation']:
        raise ValueError('历史记录已更新，请重新展开历史')
    if not 0 <= offset <= len(bundle['archive_state']['archived_units']) or not 1 <= limit <= 50:
        raise ValueError('历史分页范围无效')
    entries = bundle['archive_state']['archived_units']
    end = min(len(entries), offset + limit)
    return {'version': 'archive-history-page/1', 'report_id': report_id,
            'generation': bundle['history_generation'], 'total': len(entries),
            'offset': offset, 'next_offset': end if end < len(entries) else None,
            'archived_units': entries[offset:end]}


def page_signature(queue, archive_state=None):
    """Cheap presentation dependencies; never evidence for an archive operation.

    Atomic receipt writers change directory metadata. Historical destination
    media are deliberately not re-statted on page reads. Explicit source/history
    checks and business preflight retain their existing live validation.
    """
    import time
    from pathlib import Path
    from .order_feed import epoch
    def read(path):
        try:
            return read_json(path)
        except FileNotFoundError:
            return {}
    def metadata(path):
        try:
            info = Path(path).lstat()
            return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        except FileNotFoundError:
            return None
    queue = Path(queue)
    state = read(queue / '队列状态.json')
    path = state.get('base_model_path')
    # Do not follow arbitrary manifest paths, including an absolute path.
    import re
    canonical = metadata(queue / path) if isinstance(path, str) and re.fullmatch(
        r'combined/sha256:[a-f0-9]{64}/确认模型\.json', path) else None
    feed = read(queue / 'order-feed/current.json')
    feed_state = read(queue / 'order-feed/状态.json')
    now = time.time()
    try:
        valid = (feed_state.get('stopped') is False and feed_state.get('state') == 'ready'
                 and -5 <= now - epoch(feed_state['checked_at']) <= 180
                 and -5 <= now - epoch(feed['verified_at']) <= 600
                 and now <= epoch(feed['valid_until']))
    except (ValueError, TypeError, KeyError):
        valid = False
    controls = ()
    if archive_state is not None:
        root = Path(archive_state)
        controls = tuple(metadata(root / name) for name in
            ('requests', 'units', 'external-history', 'discarded-projection.json', 'review-display-change.json'))
    return (path, canonical, feed.get('content_id'), valid, controls)
