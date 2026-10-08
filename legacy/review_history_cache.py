"""Display-only archive history, refreshed by one background reader.

No preflight or worker reads this cache. A provisional view is always read-only.
Directory change tokens trigger early refresh; bounded periodic checks still
discover in-place edits and externally moved targets.
"""
from pathlib import Path
import threading
import time

from .archive_history import file_identity
from .order_feed_io import digest


def record_token(state):
    result = []
    for name in ('requests', 'units', 'external-history', 'discarded-index'):
        path = Path(state) / name
        try:
            st = path.lstat()
            if path.is_symlink():
                raise ValueError('展示记录目录不能是链接')
            result.append([st.st_dev, st.st_ino, st.st_mtime_ns, st.st_ctime_ns])
        except FileNotFoundError:
            result.append(None)
    return result


class ReviewHistoryCache:
    def __init__(self, history, signature, *, interval=300, clock=time.monotonic,
                 can_refresh=lambda: True, restored=None):
        self.history, self.signature = history, signature
        self.interval, self.clock, self.can_refresh = interval, clock, can_refresh
        self.lock = threading.RLock()
        self.current = restored
        self.current_key = None  # Restored evidence is never currently verified.
        self.checked = float('-inf')
        self.revision = 0
        self.epoch = 0
        self.worker = None
        self.closed = False
        self.failed_at = float('-inf')
        self.error = None

    def _key(self, model):
        return (model['report_id'], self.signature())

    def view(self, model):
        key, now = self._key(model), self.clock()
        with self.lock:
            fresh = (self.current is not None and key == self.current_key
                     and now - self.checked <= self.interval)
            if (not fresh and not self.closed and self.worker is None
                    and now - self.failed_at >= 5 and self.can_refresh()):
                # Caller owns this immutable model instance until publication.
                self.worker = threading.Thread(target=self._run, args=(model, key, self.epoch),
                                               name='review-history', daemon=True)
                self.worker.start()
            current, error = self.current, self.error
        if current is None:
            archive = {'report_id': model['report_id'], 'archived_units': [], 'warnings': []}
        elif current['report_id'] == model['report_id']:
            archive = current
        else:
            # Source scope must still match before a prior archived ID can hide
            # any part of a newly generated model, even in a read-only display.
            scopes = {u['unit_id']: digest(file_identity(u['files'])) for u in model['units']}
            # Public history rows contain target labels, not source hashes.
            # Carry only scope digests established during the prior verified
            # snapshot; an older cache without them must not hide a new unit.
            previous_scopes = current.get('_display_scope_digests', {})
            rows = [dict(r, in_current_model=(r['unit_id'] in previous_scopes and
                        scopes.get(r['unit_id']) == previous_scopes[r['unit_id']]))
                    for r in current['archived_units']]
            archive = dict(current, report_id=model['report_id'], archived_units=rows)
        return archive, fresh, error

    def _run(self, model, before, epoch):
        try:
            value = self.history.snapshot(model)
            scopes = {u['unit_id']:digest(file_identity(u['files'])) for u in model['units']}
            value = dict(value, _display_scope_digests={r['unit_id']:scopes[r['unit_id']]
                for r in value['archived_units'] if r.get('in_current_model') and r['unit_id'] in scopes})
            after = self._key(model)
            with self.lock:
                if not self.closed and before == after and epoch == self.epoch:
                    self.current = value
                    self.current_key = before
                    self.checked = self.clock()
                    self.error = None
                    self.revision += 1
        except Exception as exc:
            with self.lock:
                if not self.closed and epoch == self.epoch:
                    self.error = str(exc)
                    self.failed_at = self.clock()
        finally:
            with self.lock:
                self.worker = None

    def invalidate(self):
        with self.lock:
            self.current_key = None
            self.checked = float('-inf')
            self.revision += 1
            self.epoch += 1

    def close(self):
        with self.lock:
            self.closed = True
