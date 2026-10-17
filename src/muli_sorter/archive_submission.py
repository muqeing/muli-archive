"""Durable acknowledgement for explicit submit; never automatically replays."""
from copy import deepcopy
import threading
import time
from .archive_io import ArchiveError
from .archive_diagnostics import diagnostic_issues
from .order_feed_io import atomic_json, read_json, digest


class Submissions:
    def __init__(self, jobs):
        self.jobs = jobs
        self.root = jobs.state / 'submissions'
        self.root.mkdir(mode=0o700, exist_ok=True)
        self.lock = threading.RLock()
        self.active = {}
        self.progress = {}
        self.closed = False

    def _save(self, path, value):
        writer=getattr(self.jobs,'save_control',atomic_json)
        return writer(path,value)

    def path(self, token):
        from .archive_jobs import JOB_ID
        if not isinstance(token, str) or not JOB_ID.fullmatch(token):
            raise ArchiveError('提交标识无效')
        return self.root / (token + '.json')

    def get(self, token):
        with self.lock:
            p = self.path(token)
            if not p.exists():
                return {'submission_id':token, 'status':'not_found'}
            row = read_json(p)
            if row['submission_id'] != token:
                raise ArchiveError('提交记录身份不一致')
            if row['status'] == 'checking' and token not in self.active:
                # A process may have stopped after persisting a real job but
                # before acknowledgement. Only that prebound job may resolve it.
                job_path = self.jobs._job_path(row['expected_job_id'])
                if job_path.exists():
                    job = self.jobs.get(row['expected_job_id'])
                    if job.get('report_id') == row['report_id']:
                        return {'submission_id':token, 'status':'accepted', 'job_id':job['job_id']}
                request = self.jobs.state/'requests'/('request-'+row['expected_job_id']+'.json')
                available = request.is_file() and (self.jobs.state/'confirmation-tickets'/(token+'.json')).is_file()
                return {'submission_id':token, 'status':'unknown',
                        'recovery_available':available,
                        'expected_job_id':row['expected_job_id'],
                        'interrupted_stage':'request_saved_before_job' if available else 'unresolved',
                        'error':'提交中断，任务尚未生成；可核对原计划后恢复原提交。' if available else
                                '上次提交被中断，尚不能确认结果；不会自动重放，请保留此页面并核对任务。'}
            result = {k:v for k,v in row.items() if k in ('submission_id','status','job_id','error','issues','created_at')}
            progress = self.progress.get(token)
            if progress is not None:
                result['progress'] = dict(progress, elapsed_seconds=round(time.time()-row['created_at'], 1))
            return result

    def start(self, token, decisions, confirmed):
        p = self.path(token)
        binding = digest(decisions)
        with self.lock:
            if p.exists():
                prior = read_json(p)
                if prior.get('decisions_digest') != binding or confirmed is not True:
                    raise ArchiveError('提交范围与原记录不一致')
                return self.get(token)
            if self.closed or confirmed is not True or not self.jobs.enabled:
                raise ArchiveError('必须明确确认提交')
            if self.active:
                raise ArchiveError('已有提交正在核对，请等待原提交结果')
            # Do not wait on the submit mutex: that defeats a quick receipt.
            ticket = self.jobs.previews.get(token)
            if not ticket or ticket['expires_at'] < time.time() or ticket['decisions'] != decisions:
                raise ArchiveError('提交范围已变化或预检已过期，请重新检查')
            row = {'submission_id':token, 'status':'checking', 'created_at':time.time(),
                   'decisions_digest':binding, 'expected_job_id':ticket['prepared']['job_id'],
                   'report_id':ticket.get('origin_report_id', ticket['model']['report_id'])}
            self._save(p, row)
            thread = threading.Thread(target=self._run, args=(token, deepcopy(decisions), row),
                                      name='archive-submit', daemon=True)
            self.active[token] = thread
            thread.start()
            return self.get(token)

    def recover(self, token, confirmed):
        """Explicitly resume an interrupted admission, using its original ID."""
        p = self.path(token)
        with self.lock:
            if self.closed or confirmed is not True or not self.jobs.enabled:
                raise ArchiveError('必须明确确认恢复原提交')
            row = read_json(p)
            if row.get('status') not in ('checking', 'unknown', 'accepted'):
                raise ArchiveError('该提交不能恢复，请核对原记录')
            current = self.get(token)
            if current['status'] == 'accepted':
                return current
            if token in self.active:
                return self.get(token)
            if self.active:
                raise ArchiveError('已有提交正在核对，请等待原提交结果')
            # Durable authorization precedes work. GET and ordinary start never
            # call this path, even when a recoverable request is discovered.
            row = dict(row, status='checking', recovery_authorized_at=time.time())
            self._save(p, row)
            thread = threading.Thread(target=self._run_recovery, args=(token, row),
                                      name='archive-submit-recovery', daemon=True)
            self.active[token] = thread
            thread.start()
            return self.get(token)

    def _run_recovery(self, token, row):
        from .archive_admission import recover
        try:
            job = recover(self.jobs, token, row)
            result = dict(row, status='accepted', job_id=job['job_id'], recovered_at=time.time())
        except Exception as exc:
            result = dict(row, status='unknown', error=str(exc)[:500])
        with self.lock:
            try:
                self._save(self.path(token), result)
            finally:
                self.active.pop(token, None)

    def _run(self, token, decisions, row):
        def progress(**changes):
            with self.lock:
                value = self.progress.setdefault(token, {})
                delta = changes.pop('bytes_delta', 0)
                value['bytes_read'] = value.get('bytes_read', 0) + delta
                value.update(changes)
        try:
            job = self.jobs.submit(token, decisions, True, progress=progress)
            result = dict(row, status='accepted', job_id=job['job_id'])
        except Exception as exc:
            # Errors can occur after a request/reservation write. Preserve an
            # ambiguous state rather than inviting another business submission.
            request = self.jobs.state/'requests'/('request-'+row['expected_job_id']+'.json')
            ticket = self.jobs.previews.get(token) or {}
            result = dict(row, status='unknown' if request.exists() else 'rejected', error=str(exc)[:500],
                          issues=diagnostic_issues(exc, ticket.get('model'), ticket.get('scoped_decisions', decisions)))
        with self.lock:
            try:
                self._save(self.path(token), result)
            finally:
                self.active.pop(token, None)
                self.progress.pop(token, None)

    def close(self):
        with self.lock:
            self.closed = True
            threads = list(self.active.values())
        for thread in threads:
            thread.join(30)
            if thread.is_alive():
                raise ArchiveError('提交仍在核对，不能关闭归档服务')
