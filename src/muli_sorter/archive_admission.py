"""Publish exactly one confirmed plan, including explicit interrupted admission.

Recovery completes admission only. The normal executor retains every media,
batch-lock, content-evidence and destination check before moving any file.
"""
from copy import deepcopy
from datetime import datetime, timezone
import time

from .archive import record
from .archive_diagnostics import diagnostic_context
from .archive_io import ArchiveError, atomic_json
from .order_feed_io import atomic_json as save_json, read_json, digest


def confirmed_request(jobs, ticket):
    plan = ticket['prepared']
    request = {'model': ticket['model'],
               'decisions': deepcopy(ticket.get('scoped_decisions', ticket['decisions'])),
               'job_id': plan['job_id'], 'roots': jobs.identity,
               'file_plans': plan['file_plans'], 'admission_policy': 'confirmed-plan/v1'}
    if 'scope_binding' in ticket:
        request['scope_binding'] = ticket['scope_binding']
    from .archive_rename_io import STRATEGY
    if plan['execution_strategy'] == STRATEGY:
        request.update(execution_strategy=STRATEGY, source_evidence=plan['evidence'],
                       target_parent_identities=plan['target_parent_identities'],
                       verification_policy='postcopy-signature-or-full-read/v1',
                       validated_signatures=plan['validated_signatures'])
    return request


def publish(jobs, ticket, request, *, progress=None, saved_request=False, confirmed_at=None):
    """Caller holds jobs.mutex; no worker wake until all control records exist."""
    plan, job_id = ticket['prepared'], request['job_id']
    if progress:
        progress(phase='保存本次归档任务', current_file='')
    reservations = record(jobs.state_fd, 'reservations.json', {})
    for row in plan['file_plans'].values():
        with diagnostic_context(source_path=row['source_path'], name=row.get('name'), target_path=row['target_path']):
            prior = reservations.get(row['source_path'])
            reservation = {'target_path': row['target_path'], 'blake3': row['blake3']}
            if prior is not None and prior != reservation:
                raise ArchiveError('本次素材已被另一份归档计划占用，请保留原任务并核对')
            reservations[row['source_path']] = reservation
    if not saved_request:
        save_json(jobs.state/'requests'/('request-'+job_id+'.json'), request)
    atomic_json(jobs.state_fd, 'reservations.json', reservations)
    # Drop the large historical table before constructing/hashing a job.
    del reservations
    stamp = datetime.now(timezone.utc).isoformat()
    s = plan['summary']
    job = {'job_id': job_id, 'status': 'queued', 'phase': '等待归档',
           'created_at': stamp, 'confirmed_at': confirmed_at or stamp,
           'report_id': ticket.get('origin_report_id', ticket['model']['report_id']),
           'example_data': not jobs.production,
           'storage': 'verified_move' if plan['archive_options']['mode'] == 'move' else 'independent_copy',
           'archive_options': plan['archive_options'], 'file_plans': plan['file_plans'],
           'execution_strategy': plan['execution_strategy'], 'request_digest': digest(request),
           'target_identities': plan['target_identities'], 'folders': {},
           'summary': {'total_units': s['units'], 'total_files': s['files'], 'total_bytes': s['bytes'],
                       'completed_units': 0, 'completed_files': 0, 'processed_bytes': 0,
                       'planned_direct_move_files': s.get('direct_move_files', 0), 'direct_moved_files': 0,
                       'skipped_files': 0, 'copy_files': 0, 'renamed_files': s['renamed_files'], 'removed_sources': 0},
           'projects': plan['projects'], 'outcomes': [], 'errors': []}
    jobs._save(job)
    jobs.wake.set()
    return job


def recover(jobs, token, submission, *, progress=None):
    """Complete a recorded, originally confirmed admission; never re-preflight."""
    with jobs.mutex:
        if not jobs.enabled or jobs.worker_busy.is_set():
            raise ArchiveError('归档服务忙碌或写入未启用，不能恢复提交')
        jobs._check_roots()
        ticket = jobs.previews.get(token)
        if not ticket or submission.get('submission_id') != token:
            raise ArchiveError('原确认票据或提交记录不存在，不能恢复')
        created = submission.get('created_at')
        if (type(created) not in (int, float) or not 0 < created <= time.time()
                or created > ticket['expires_at']
                or digest(ticket['decisions']) != submission.get('decisions_digest')
                or ticket['prepared']['job_id'] != submission.get('expected_job_id')
                or ticket.get('origin_report_id', ticket['model']['report_id']) != submission.get('report_id')):
            raise ArchiveError('原提交与确认票据的时间、范围或任务绑定不一致')
        expected = confirmed_request(jobs, ticket)
        job_id = expected['job_id']
        request = read_json(jobs.state/'requests'/('request-'+job_id+'.json'))
        if digest(request) != digest(expected):
            raise ArchiveError('已保存请求与原确认计划不一致，不能恢复')
        if jobs._job_path(job_id).exists():
            job = jobs.get(job_id)
            if (job.get('request_digest') != digest(request)
                    or job.get('report_id') != submission['report_id']):
                raise ArchiveError('原任务与已保存请求绑定不一致')
            return job
        # An absent job with execution records is not a pre-execution crash.
        if any((jobs.state/'requests').glob('direct-entry-'+job_id+'-*.json')):
            raise ArchiveError('任务缺失但已有移动执行记录，需核对，不能重建')
        return publish(jobs, ticket, request, progress=progress, saved_request=True,
                       confirmed_at=datetime.fromtimestamp(created, timezone.utc).isoformat())
