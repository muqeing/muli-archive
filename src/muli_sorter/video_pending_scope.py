"""Presentation-only pending video scope; no archive or Ingest mutation authority."""
from copy import deepcopy
from collections import deque
from pathlib import Path
import stat
import time
import threading

from .order_feed_io import atomic_json, digest, read_json
from .review import identity_digest, validate_model, ReviewError
from .video_previews import load_index, video_units, cached, generate_unit, utc, candidates, ERROR_RECIPE


def worker_error_code(exc):
    if isinstance(exc, FileNotFoundError):
        return 'scope_missing'
    if isinstance(exc, ReviewError):
        return 'model_invalid'
    if isinstance(exc, PermissionError):
        return 'permission_denied'
    if isinstance(exc, ValueError) and str(exc) in (
            'pending_scope_binding_invalid', 'pending_scope_invalid',
            'pending_scope_changed_or_nonvideo'):
        return str(exc)
    return 'scope_unavailable'


def bound_scope(path, descriptor=None):
    payload = read_json(path)
    if (payload.get('schema') != 'pending-video-scope/1' or
            payload.get('scope_id') != digest({k:v for k,v in payload.items() if k != 'scope_id'})):
        raise ValueError('pending_scope_binding_invalid')
    if descriptor is not None and payload['scope_id'] != descriptor.get('scope_id'):
        raise ValueError('pending_scope_generation_mismatch')
    return payload


class VideoDisplayCache:
    def __init__(self, scope, output):
        self.scope, self.output = Path(scope), Path(output)
        self.lock = threading.Lock();self.key=None;self.value={}

    def read(self, descriptor):
        from .video_preview_view import preview_map
        if not descriptor:return {}
        with self.lock:
            try:
                attrs=[]
                for path in (self.scope,self.output/'index.json'):
                    s=path.lstat()
                    if not stat.S_ISREG(s.st_mode):raise ValueError('preview_status_invalid')
                    attrs.append((s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns,s.st_ctime_ns))
                worker_path = self.output/'worker-state.json'
                try:
                    s = worker_path.lstat()
                    attrs.append((s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns,s.st_ctime_ns))
                except FileNotFoundError:
                    attrs.append(None)
                key=(descriptor['scope_id'],attrs)
                if key != self.key:
                    payload=bound_scope(self.scope,descriptor)
                    self.value=preview_map(self.output,payload['model'],prefix='video-previews/')
                    try:
                        worker = read_json(worker_path)
                    except (OSError, ValueError):
                        worker = {}
                    unavailable = worker.get('state') == 'waiting_pending_scope'
                    for unit in payload['model']['units']:
                        entry = self.value.setdefault(unit['unit_id'], {
                            'state': 'error' if unavailable else 'pending',
                            'message': ('截图服务暂时无法读取素材清单；可继续确认归属。' if unavailable
                                        else '截图正在排队生成；生成后重新加载即可查看。'),
                            'frames': []})
                        files = candidates(unit)
                        if files and not entry.get('source_name'):
                            source = Path(files[0]['source_path'])
                            entry['source_name'] = source.name
                            entry['source_label'] = '代理视频' if source.suffix.lower()=='.lrf' else '主视频'
                    self.key=key
                return self.value
            except (OSError,ValueError,KeyError,TypeError):
                return {}


def publish_scope(root, view):
    units = [deepcopy(u) for u in view['units'] if u['kind'] in ('video', 'proxy_only')]
    ids = {u['unit_id'] for u in units}
    for u in units:
        u['candidate_project_ids'] = []
    segments = []
    for s in view['initial_segments']:
        members = [uid for uid in s['unit_ids'] if uid in ids]
        if members:
            segments.append({'segment_id': s['segment_id'], 'unit_ids': members})
    model = {'schema_version': '0.2', 'snapshot_at': view['snapshot_at'],
             'example_data': view['example_data'], 'units': units, 'projects': [],
             'initial_segments': segments, 'excluded_batches': []}
    model['report_id'] = identity_digest(model)
    validate_model(model)
    payload = {'schema': 'pending-video-scope/1', 'canonical_report_id': view['report_id'], 'model': model}
    payload['scope_id'] = digest(payload)
    path = Path(root) / 'current.json'
    try:
        previous = read_json(path)
    except (OSError, ValueError):
        previous = None
    if previous != payload:
        atomic_json(path, payload)
    return {'scope_id': payload['scope_id'], 'videos': len(units)}


class PendingVideoWorker:
    """Load a small pending scope only when it changes, inspect each clip once.

    An unavailable scope fails closed. There is no fallback to full history.
    Cached previews and terminal errors are not rechecked every idle iteration.
    """
    def __init__(self, scope, staging, output, *, extractor=None):
        self.scope, self.staging, self.output = Path(scope), Path(staging), Path(output)
        self.extractor = extractor
        self.signature = None
        self.units, self.todo, self.index = {}, deque(), None

    def attrs(self):
        info = self.scope.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > 48*1024*1024:
            raise ValueError('pending_scope_invalid')
        return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns

    def refresh(self):
        before = self.attrs()
        if before == self.signature:
            return False
        payload = bound_scope(self.scope)
        model = payload['model']
        validate_model(model)
        units = video_units(model)
        if len(units) != len(model['units']) or before != self.attrs():
            raise ValueError('pending_scope_changed_or_nonvideo')
        self.units = {u['unit_id']:u for u in units}
        self.todo = deque(self.units)
        self.index = load_index(self.output)
        self.index['entries'] = {uid:self.index['entries'].get(uid, {'state':'pending','frames':[]}) for uid in self.units}
        self.index.update(base_report_id=payload['canonical_report_id'], scope_id=payload['scope_id'], checked_at=utc())
        atomic_json(self.output / 'index.json', self.index)
        self.signature = before
        return True

    def step(self):
        self.refresh()
        if not self.todo:
            # Only failed clips whose explicit retry delay elapsed are revisited.
            self.todo = deque(uid for uid,e in self.index['entries'].items()
                         if e.get('state') == 'error' and e.get('retry_after',0) <= time.time())
        if not self.todo:
            return False
        uid = self.todo.popleft()
        unit, entry = self.units[uid], self.index['entries'][uid]
        if (entry.get('state') == 'error' and entry.get('error_recipe') == ERROR_RECIPE
                and entry.get('retry_after',0) > time.time()):
            return True
        if cached(self.output, entry, unit, self.staging):
            return True
        kwargs = {'extractor':self.extractor} if self.extractor is not None else {}
        entry = generate_unit(self.staging, self.output, unit, **kwargs)
        if self.attrs() != self.signature:
            return True  # A newer scope owns the next iteration's publication.
        self.index['entries'][uid] = entry
        self.index['checked_at'] = utc()
        atomic_json(self.output / 'index.json', self.index)
        return True

    def run(self, stop, *, once=False):
        last_state = None
        while not stop.is_set():
            try:
                active = self.step()
                state = {name:sum(e['state']==name for e in self.index['entries'].values()) for name in ('ready','error','pending')}
                state.update(state='running' if active else 'ready', video_units=len(self.units),
                             scope_id=self.index['scope_id'])
                if state != last_state:
                    atomic_json(self.output / 'worker-state.json', dict(state, checked_at=utc()))
                    last_state = state
                if once and not active:
                    return 0 if all(e['state']=='ready' for e in self.index['entries'].values()) else 1
                if not active:
                    stop.wait(2)
            except (OSError, ValueError, KeyError, TypeError) as exc:
                atomic_json(self.output / 'worker-state.json', {'state':'waiting_pending_scope',
                            'error_code':worker_error_code(exc),'checked_at':utc()})
                last_state = None  # Recovery must replace the published error even if counts are unchanged.
                if once:
                    return 1
                stop.wait(2)
        return 0
