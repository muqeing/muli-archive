"""Display-only copies whose sources must remain; never verified archive evidence.

Bind the explicit retained-copy terminal record to its immutable request and
exact selected file identities. No source/target stat or content read occurs.
"""
import re

from .order_feed_io import digest
from .review import compile_plan


HEX = re.compile(r'[a-f0-9]{64}')


def retained_projection(job, request, roots):
    from .archive_history import file_identity
    jid = job.get('job_id')
    if (not isinstance(jid, str) or not HEX.fullmatch(jid)
            or job.get('status') != 'copied_unverified'
            or job.get('storage') != 'unverified_copy'
            or job.get('source_removal_disabled') is not True
            or job.get('cleanup_started')
            or job.get('archive_options', {}).get('mode') != 'copy'
            or job.get('verification', {}).get('content_verified') is not False):
        raise ValueError('来源保留任务状态不完整')
    override = job.get('copy_override', {})
    if (override.get('job_id') != jid or override.get('status') != 'copied_unverified'
            or override.get('source_removal') is not False
            or override.get('verification') != 'not_full_content_verified'
            or not override.get('completed_at')):
        raise ValueError('来源保留记录尚未完成')
    binding = digest(request)
    if (request.get('job_id') != jid or request.get('roots') != roots
            or job.get('request_digest') != binding
            or override.get('request_sha256') != binding):
        raise ValueError('来源保留记录与原请求不符')
    model, decisions, plans = request['model'], request['decisions'], request['file_plans']
    if job.get('example_data') is not model.get('example_data'):
        raise ValueError('来源保留记录环境不符')
    compiled = compile_plan(model, {k:v for k,v in decisions.items() if k != 'archive_options'})
    units = {u['unit_id']: u for u in model['units']}
    selected, sources, files, total_bytes = {}, set(), 0, 0
    for assignment in compiled['assignments']:
        for uid in assignment['unit_ids']:
            if uid in selected:
                raise ValueError('来源保留请求包含重复素材')
            unit = units[uid]
            rows = unit['files']
            # Targets and all source rows are from the same immutable request;
            # only a complete unit may leave the new-submission list.
            planned = [plans[row['source_path']] for row in rows]
            if file_identity(planned) != file_identity(rows):
                raise ValueError('来源保留文件范围不符')
            for row in planned:
                if row['source_path'] in sources:
                    raise ValueError('来源保留请求包含重复文件')
                sources.add(row['source_path'])
            count = len(rows)
            selected[uid] = {'scope_digest': digest(file_identity(rows)), 'file_count': count,
                             'project_name': assignment['project']['name']}
            files += count
            total_bytes += sum(row['size_bytes'] for row in rows)
    if not selected or set(plans) != sources:
        raise ValueError('来源保留清单与选择范围不符')
    summary = job.get('summary', {})
    if (summary.get('total_units') != len(selected) or summary.get('completed_units') != len(selected)
            or summary.get('total_files') != files or summary.get('completed_files') != files
            or summary.get('total_bytes') != total_bytes or summary.get('processed_bytes') != total_bytes
            or summary.get('removed_sources') != 0
            or any(override.get(k) != files for k in ('total_files', 'completed_files', 'source_files_present', 'target_entries_present'))
            or override.get('total_bytes') != total_bytes):
        raise ValueError('来源保留记录数量与原清单不符')
    return {'job_id': jid, 'units': selected}


def retained_summary(projections, current, archived):
    """Only exact current scopes are excluded; verified receipts retain priority."""
    excluded, jobs, warnings = {}, [], []
    verified = {uid for uid,row in archived.items() if row.get('in_current_model')}
    for projection in projections:
        members = []
        for uid, row in projection['units'].items():
            if uid not in current or uid in verified or uid in excluded:
                continue
            if row['scope_digest'] != current[uid]:
                warnings.append('部分来源保留记录与当前素材范围不同，仍保留待处理。')
                continue
            excluded[uid] = row
            members.append(row)
        if members:
            jobs.append({'job_id': projection['job_id'], 'status': 'copied_unverified',
                         'source_preserved': True, 'content_verified': False,
                         'unit_count': len(members), 'file_count': sum(r['file_count'] for r in members),
                         'project_names': sorted({r['project_name'] for r in members})})
    return {'retained_unit_ids': sorted(excluded),
            'retained_current_files': sum(r['file_count'] for r in excluded.values()),
            'retained_jobs': jobs}, warnings
