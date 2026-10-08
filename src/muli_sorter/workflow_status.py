"""Read-only handoff projection; never starts or alters an archive task."""
from urllib.parse import urlsplit
from .intake import BATCH


def service_origin(value):
    if not value:
        return None
    try:
        url = urlsplit(value)
        valid = (url.scheme in ('http', 'https') and url.hostname and
                 url.username is None and url.password is None and not url.query and
                 not url.fragment and url.path in ('', '/') and url.port != 0)
    except ValueError:
        valid = False
    if not valid:
        raise ValueError('服务入口必须是无凭据的 HTTP 或 HTTPS 根地址')
    return url.scheme + '://' + url.netloc


def snapshot(model, archive, materials, discovery, ingest_url):
    archived = {r['unit_id'] for r in archive.get('archived_units', []) if r.get('in_current_model')}
    retained = set(archive.get('retained_unit_ids', []))
    batches = {}
    for unit in model.get('units', []):
        bids = {r.get('batch_id') for r in unit.get('provenance', []) if isinstance(r, dict)}
        for bid in bids:
            if not isinstance(bid, str) or not BATCH.fullmatch(bid):
                continue
            row = batches.setdefault(bid, {'batch_id': bid, 'units': 0, 'files': 0,
                'archived': 0, 'retained': 0, 'discarded': 0, 'pending': 0, 'exceptions': 0, 'support': 0})
            row['units'] += 1
            row['files'] += unit.get('file_count', len(unit.get('files', [])))
            role = materials.get('units', {}).get(unit['unit_id'], {}).get('category', 'exception')
            key = ('archived' if unit['unit_id'] in archived else 'retained' if unit['unit_id'] in retained else 'discarded' if role == 'discarded'
                   else 'pending' if role in ('shoot', 'companion') else 'support' if role == 'support' else 'exceptions')
            row[key] += 1
    for row in batches.values():
        row['phase'] = ('awaiting_confirmation' if row['pending'] else 'needs_attention' if row['exceptions']
                        else 'copied_unverified' if row['retained'] else 'archived' if row['archived'] else 'no_pending_shoot')
    for incoming in (discovery or {}).get('batches', []):
        bid = incoming['batch_id']
        row = batches.setdefault(bid, {'batch_id': bid, 'units': 0, 'files': 0,
            'archived': 0, 'retained': 0, 'discarded': 0, 'pending': 0, 'exceptions': 0, 'support': 0})
        verification = incoming.get('verification', {})
        row['reason'] = str(verification.get('error') or incoming.get('reason') or '')[:400]
        if verification.get('state') == 'manual_archive_verified':
            row['phase'] = 'manual_archived'
            row['verified_files'] = verification.get('verified_files')
        elif verification.get('state') in ('failed', 'blocked', 'interrupted', 'unavailable') or '失败' in row['reason']:
            row['phase'] = 'needs_attention'
        else:
            row['phase'] = {'waiting_completion': 'copying', 'verification_required': 'verifying',
                            'queued': 'organizing', 'processing': 'organizing', 'retry_wait': 'needs_attention'}.get(incoming['state'], 'unknown')
    if discovery is not None and discovery.get('source_ok') is not True:
        for row in batches.values():
            row['phase'] = 'unknown'
    return {'schema': 'workflow-handoff/1', 'ingest_url': service_origin(ingest_url),
            'checked_at': (discovery or {}).get('checked_at'), 'batches': sorted(batches.values(), key=lambda r: r['batch_id'], reverse=True)}
