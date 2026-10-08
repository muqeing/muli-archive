"""Small read-only browser projection, without media scans or project writes."""
from pathlib import Path
import os
import re
from .intake import BATCH, decode, read_bytes


def snapshot(queue):
    state = decode(read_bytes(Path(queue), '队列状态.json'))
    path = state.get('base_model_path', '')
    match = re.fullmatch(r'combined/(sha256:[a-f0-9]{64})/确认模型\.json', path)
    rows = []
    for row in state.get('jobs', []):
        if row.get('state') not in ('waiting_completion', 'verification_required', 'queued', 'processing', 'retry_wait'):
            continue
        bid = row.get('batch_id', '')
        if not isinstance(bid, str) or not BATCH.fullmatch(bid):
            continue
        item = {'batch_id': bid, 'state': row['state'], 'reason': str(row.get('error') or '')[:400]}
        root = os.environ.get('MULI_POSTCOPY_RECEIPTS')
        if root and row['state'] == 'verification_required':
            try:
                raw = read_bytes(Path(root), bid + '.status.json')
                if len(raw) > 64 * 1024:
                    raise ValueError('校验进度超过读取范围')
                status = decode(raw)
                if status.get('batch_id') != bid or status.get('schema') != 'postcopy-status/1':
                    raise ValueError('校验进度身份无效')
                item['verification'] = {k: status[k] for k in ('state', 'updated_at', 'error',
                    'total_files', 'verified_files', 'total_bytes', 'read_bytes', 'project', 'checked_at') if k in status}
            except FileNotFoundError:
                pass
            except (OSError, ValueError):
                item['verification'] = {'state': 'unavailable'}
        rows.append(item)
    return {'schema': 'discovery-status/1', 'base_report_id': match.group(1) if match else '',
            'checked_at': state.get('generated_at'), 'source_ok': state.get('source', {}).get('ok') is True,
            'batches': rows}
