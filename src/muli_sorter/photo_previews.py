"""On-demand photo previews, isolated decoder, bounded queue and persistent cache."""
import json
import os
from pathlib import Path
import queue
import re
import subprocess
import sys
import threading
import time

from .archive_io import directory, signature
from .intake import read_bytes
from .order_feed_io import atomic_json, digest, read_json
from .queue_files import publish
from .video_previews import identity, source_fd

RECIPE = 'photo-720-jpeg-raw-thumb-first-v1'
RAW = {'.arw', '.cr2', '.cr3', '.nef', '.nrw', '.dng', '.raf', '.rw2', '.orf', '.pef', '.srw', '.raw'}
PHOTO = RAW | {'.jpg', '.jpeg', '.png', '.tif', '.tiff', '.webp'}
NAME = re.compile(r'[0-9a-f]{64}\.jpg\Z')


def candidates(unit):
    # A RAW+JPG pair is one photo. Prefer its RAW preview and never display twice.
    return sorted([f for f in unit.get('files', []) if Path(f['source_path']).suffix.lower() in PHOTO],
                  key=lambda f: (Path(f['source_path']).suffix.lower() not in RAW, f['source_path']))


def key(unit):
    return digest({'recipe': RECIPE, 'files': [identity(f) for f in candidates(unit)]})


def extract(fd, file):
    before = signature(fd)
    env = dict(os.environ, OMP_NUM_THREADS='1', OPENBLAS_NUM_THREADS='1', MKL_NUM_THREADS='1')
    result = subprocess.run([sys.executable, '-m', 'muli_sorter.photo_decode', str(fd),
                             'raw' if Path(file['source_path']).suffix.lower() in RAW else 'photo'],
                            pass_fds=(fd,), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            env=env, timeout=40, check=True)
    label, data = result.stdout.split(b'\n', 1)
    label = label.decode()
    if label not in ('照片预览', 'RAW 内嵌预览', 'RAW 解码预览'):
        raise ValueError('preview_label_invalid')
    if not 4 <= len(data) <= 512 * 1024 or not data.startswith(b'\xff\xd8') or not data.endswith(b'\xff\xd9'):
        raise ValueError('preview_invalid')
    if signature(fd) != before:
        raise ValueError('source_changed_during_preview')
    return label, data


class PhotoPreviews:
    def __init__(self, staging, output, *, extractor=extract):
        self.staging, self.output = Path(staging).absolute(), Path(output).absolute()
        if self.output == self.staging or self.output.is_relative_to(self.staging) or self.staging.is_relative_to(self.output):
            raise ValueError('照片预览缓存必须与原素材分开')
        self.output.mkdir(mode=0o700, parents=True, exist_ok=True)
        with directory(self.output):
            pass
        self.extractor = extractor
        self.lock = threading.Lock()
        self.waiting = set()
        self.scope_token = None
        self.queue = queue.Queue(maxsize=128)
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._run, name='photo-previews', daemon=True)
        self.thread.start()

    def close(self):
        self.stop.set()
        self.thread.join(timeout=41)

    def _cached(self, unit, *, check_source=True):
        try:
            entry = read_json(self.output / (key(unit) + '.json'))
            if entry.get('recipe') != RECIPE:
                return None
            if entry.get('state') == 'error':
                return entry if entry.get('retry_after', 0) > time.time() else None
            if entry.get('state') != 'ready' or not NAME.fullmatch(entry.get('file', '')):
                return None
            file = next((f for f in candidates(unit) if identity(f) == entry.get('source')), None)
            if file is None:
                return None
            stored = entry.get('source_signature')
            if (not isinstance(stored,list) or len(stored)!=5 or any(type(n) is not int for n in stored)
                    or entry['file'] != digest({'source':identity(file),'signature':stored,'recipe':RECIPE})+'.jpg'):
                return None
            if check_source:
                with source_fd(self.staging, file) as fd:
                    if signature(fd) != entry.get('source_signature'):
                        return None
            data = read_bytes(self.output, entry['file'])
            if 4 <= len(data) <= 512 * 1024 and data.startswith(b'\xff\xd8') and data.endswith(b'\xff\xd9'):
                return entry
        except (OSError, ValueError, KeyError, TypeError):
            pass
        return None

    def request(self, model, report_id, unit_ids):
        if report_id != model['report_id']:
            raise ValueError('素材快照已更新，请刷新页面后查看照片')
        if not isinstance(unit_ids, list) or not 1 <= len(unit_ids) <= 6 or any(not isinstance(x, str) for x in unit_ids) or len(set(unit_ids)) != len(unit_ids):
            raise ValueError('每次只接受本拍摄段的 1 至 6 张照片')
        by_id = {u['unit_id']: u for u in model['units'] if u['kind'] == 'photo'}
        if any(uid not in by_id for uid in unit_ids):
            raise ValueError('照片不属于当前素材快照')
        return self.request_units([by_id[uid] for uid in unit_ids])

    def request_units(self, units, *, display_only=False, enqueue=True, scope_token=None):
        # This subset is supplied by the hash-bound pending index. It cannot
        # authorize an archive. Generating a missing preview still checks source.
        result = {}; deferred = []
        with self.lock:
            if scope_token is None:scope_token=self.scope_token
            elif scope_token != self.scope_token:
                return {'previews':{u['unit_id']:{'state':'pending'} for u in units},'deferred':[u['unit_id'] for u in units]}
        for unit in units:
            uid = unit['unit_id']
            entry = self._cached(unit, check_source=not display_only)
            if entry is None:
                k = key(unit)
                with self.lock:
                    if enqueue and k not in self.waiting and not self.stop.is_set():
                        try:
                            self.queue.put_nowait((k, unit, scope_token))
                            self.waiting.add(k)
                        except queue.Full:
                            deferred.append(uid)
                result[uid] = {'state': 'pending'}
            elif entry['state'] == 'ready':
                result[uid] = {'state': 'ready', 'src': 'photo-previews/' + entry['file'],
                               'source_name': entry['source_name'], 'source_label': entry['source_label']}
            else:
                result[uid] = {'state': 'error', 'message': '照片预览暂不可用，仍可确认归属；稍后刷新重试'}
        return {'previews': result, 'deferred': deferred}

    def _generate(self, unit):
        for file in candidates(unit):
            try:
                with source_fd(self.staging, file) as fd:
                    before = signature(fd)
                    label, data = self.extractor(fd, file)
                    if signature(fd) != before:
                        raise ValueError('source_changed_during_preview')
                name = digest({'source': identity(file), 'signature': before, 'recipe': RECIPE}) + '.jpg'
                publish(self.output, None, {name: data})
                return {'state': 'ready', 'recipe': RECIPE, 'file': name, 'source': identity(file),
                        'source_signature': before, 'source_name': Path(file['source_path']).name, 'source_label': label}
            except (OSError, ValueError, KeyError, subprocess.SubprocessError):
                continue
        return {'state': 'error', 'recipe': RECIPE, 'retry_after': time.time() + 300}

    def _run(self):
        while not self.stop.is_set():
            try:
                k, unit, scope_token = self.queue.get(timeout=0.25)
            except queue.Empty:
                continue
            try:
                if scope_token != self.scope_token:
                    continue
                entry = self._cached(unit) or self._generate(unit)
                if scope_token != self.scope_token:
                    continue
                atomic_json(self.output / (k + '.json'), entry)
            except (OSError, ValueError, KeyError, TypeError):
                pass
            finally:
                with self.lock:
                    self.waiting.discard(k)
                self.queue.task_done()
