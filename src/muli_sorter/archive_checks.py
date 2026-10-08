"""Bounded, cancellable read-only preflight work; never submits archive jobs."""
from collections import OrderedDict
from copy import deepcopy
import hashlib
import json
import math
import re
import sqlite3
import threading
import time
from pathlib import Path

from .archive_io import ArchiveError, hash_fd, signature


class CheckCancelled(Exception):
    pass


class TargetDigestCache:
    """Bounded digest cache keyed by the complete live filesystem identity.

    ``disk_path`` is optional for backwards compatibility.  The database is a
    small key/value index, so a lookup reads one row and never loads the cache
    into memory.  Persistent failures disable only persistence; the live hash
    and identity checks remain authoritative.
    """
    _TABLE = 'target_digests'

    def __init__(self, limit=8192, ttl=3600, disk_path=None):
        self.values = OrderedDict()
        self.lock = threading.Lock()
        self.limit, self.ttl = limit, ttl
        self.disk_path = Path(disk_path) if disk_path is not None else None
        self._db = None
        self._db_disabled = False
        self._writes_since_prune = 0
        if self.disk_path is not None:
            self._open_db()

    @staticmethod
    def _key(value):
        return 'v1:' + ','.join(str(int(part)) for part in value)

    @staticmethod
    def _valid_digest(value):
        return isinstance(value, str) and re.fullmatch(r'[0-9a-f]{64}', value) is not None

    @staticmethod
    def _proof(key, value):
        return hashlib.sha256((key + '\0' + value).encode()).hexdigest()

    def _open_db(self):
        try:
            connection = sqlite3.connect(str(self.disk_path), timeout=1,
                                         check_same_thread=False)
            connection.execute('PRAGMA busy_timeout=1000')
            # This cache is a performance hint; losing rows only causes a
            # future real hash. WAL/NORMAL avoids making every media check pay
            # for a full synchronous rollback-journal commit.
            connection.execute('PRAGMA journal_mode=WAL')
            connection.execute('PRAGMA synchronous=NORMAL')
            connection.execute(
                'CREATE TABLE IF NOT EXISTS target_digests ('
                'cache_key TEXT PRIMARY KEY, digest TEXT NOT NULL, proof TEXT NOT NULL, '
                'stored_at REAL NOT NULL)')
            connection.execute(
                'CREATE INDEX IF NOT EXISTS target_digests_stored_at '
                'ON target_digests(stored_at)')
            connection.commit()
            self._db = connection
        except (OSError, sqlite3.Error, TypeError, ValueError):
            self._disable_db()

    def _disable_db(self):
        connection, self._db = self._db, None
        self._db_disabled = True
        if connection is not None:
            try:
                connection.close()
            except sqlite3.Error:
                pass

    def _persistent_get(self, key, now):
        connection = self._db
        if connection is None:
            return None
        try:
            row = connection.execute(
                'SELECT digest, proof, stored_at FROM target_digests WHERE cache_key=?',
                (key,)).fetchone()
            if row is None:
                return None
            value, proof, stored_at = row
            try:
                valid_time = (type(stored_at) in (int, float)
                              and math.isfinite(float(stored_at))
                              and stored_at <= now)
            except (OverflowError, TypeError, ValueError):
                valid_time = False
            if (not self._valid_digest(value) or not valid_time
                    or proof != self._proof(key, value)
                    or now - stored_at >= self.ttl):
                connection.execute('DELETE FROM target_digests WHERE cache_key=?', (key,))
                connection.commit()
                return None
            return value
        except (sqlite3.Error, TypeError, ValueError):
            self._disable_db()
            return None

    def _persistent_put(self, key, value, now):
        connection = self._db
        if connection is None:
            return
        try:
            # The INSERT and bounded eviction are one transaction.  A crash
            # leaves either the old row or the complete new row.
            with connection:
                connection.execute(
                    'INSERT INTO target_digests(cache_key,digest,proof,stored_at) VALUES(?,?,?,?) '
                    'ON CONFLICT(cache_key) DO UPDATE SET digest=excluded.digest,proof=excluded.proof,stored_at=excluded.stored_at',
                    (key, value, self._proof(key, value), now))
                self._writes_since_prune += 1
                # Large caches prune in batches; a small configured limit is
                # kept exact so tests and callers using limit=1 do not grow.
                if self.limit < 256 or self._writes_since_prune >= 256:
                    connection.execute('DELETE FROM target_digests WHERE stored_at < ?',
                                       (now - self.ttl,))
                    count = connection.execute('SELECT COUNT(*) FROM target_digests').fetchone()[0]
                    excess = max(0, int(count) - int(self.limit))
                    if excess:
                        connection.execute(
                            'DELETE FROM target_digests WHERE cache_key IN '
                            '(SELECT cache_key FROM target_digests ORDER BY stored_at ASC LIMIT ?)',
                            (excess,))
                    self._writes_since_prune = 0
        except (sqlite3.Error, TypeError, ValueError):
            self._disable_db()

    def reserve(self, file_count):
        with self.lock:
            self.limit = max(self.limit, min(file_count, 200000))

    def close(self):
        """Close the optional SQLite handle; memory entries remain readable."""
        with self.lock:
            self._disable_db()

    def digest(self, fd, progress=None):
        # A cache hit still opens the named file without following symlinks and
        # verifies its complete live identity. No manifest hash populates this cache.
        with self.lock:
            before = tuple(signature(fd))
            prior = self.values.get(before)
            if prior and time.monotonic() - prior[0] < self.ttl:
                if progress:
                    progress(0)
                if tuple(signature(fd)) != before:
                    raise ArchiveError('复用校验时目标文件发生变化')
                self.values.move_to_end(before)
                return prior[1]
            persistent = self._persistent_get(self._key(before), time.time())
            if persistent is not None:
                if tuple(signature(fd)) != before:
                    raise ArchiveError('复用校验时目标文件发生变化')
                if progress:
                    progress(0)
                self.values[before] = (time.monotonic(), persistent)
                self.values.move_to_end(before)
                while len(self.values) > self.limit:
                    self.values.popitem(last=False)
                return persistent
            value = hash_fd(fd, progress=progress)
            if tuple(signature(fd)) != before:
                raise ArchiveError('计算摘要时目标文件发生变化')
            self.values[before] = (time.monotonic(), value)
            self.values.move_to_end(before)
            while len(self.values) > self.limit:
                self.values.popitem(last=False)
            self._persistent_put(self._key(before), value, time.time())
            return value


class PreflightChecks:
    def __init__(self, runner, max_active=8, max_records=32):
        self.runner = runner
        self.max_active, self.max_records = max_active, max_records
        self.condition = threading.Condition()
        self.records = OrderedDict()
        self.clients = OrderedDict()
        self.thread = None
        self.stopped = False

    @staticmethod
    def validate_client(client_id, revision):
        if not isinstance(client_id, str) or not re.fullmatch('[a-f0-9]{32}', client_id):
            raise ArchiveError('检查页面标识无效，请刷新页面')
        if type(revision) is not int or not 0 <= revision <= 10**12:
            raise ArchiveError('检查版本无效')

    def _view(self, row):
        progress = dict(row['progress'], elapsed_seconds=round(time.monotonic()-row['started'], 1))
        return deepcopy({**row.get('result', {}), 'check_id':row['check_id'], 'status':row['status'], 'progress':progress})

    def _prune(self):
        for key in list(self.records):
            row = self.records[key]
            if row['status'] not in ('queued', 'checking') and (len(self.records) >= self.max_records or time.monotonic()-row['started'] > 1500):
                del self.records[key]
        # Tombstones prevent late network requests from reviving cancelled work.
        for client in list(self.clients):
            if time.monotonic()-self.clients[client]['updated'] > 3600 and not any(r['client_id']==client for r in self.records.values()):
                del self.clients[client]

    def start(self, client_id, revision, decisions):
        self.validate_client(client_id, revision)
        if not isinstance(decisions, dict):
            raise ArchiveError('归档计划格式无效')
        encoded = json.dumps(decisions, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
        key = hashlib.sha256((client_id+':'+str(revision)+':'+encoded).encode()).hexdigest()
        with self.condition:
            if self.stopped:
                raise ArchiveError('检查服务正在停止')
            self._prune()
            previous = self.clients.get(client_id)
            if previous and revision <= previous['revision']:
                if revision == previous['revision'] and previous.get('key') == key and key in self.records:
                    return self._view(self.records[key])
                raise ArchiveError('该检查已被新的选择替代，请重新检查')
            active = [r for r in self.records.values() if r['status'] in ('queued', 'checking') and r['client_id'] != client_id]
            if len(active) >= self.max_active or (client_id not in self.clients and len(self.clients) >= 128):
                raise ArchiveError('正在检查的页面较多，请稍后重新检查')
            for row in self.records.values():
                if row['client_id'] == client_id and row['status'] in ('queued', 'checking'):
                    row['cancel'].set()
                    row['status'] = 'cancelled'
                    row['decisions'] = None
            row = dict(check_id=key, client_id=client_id, revision=revision, decisions=deepcopy(decisions),
                       status='queued', cancel=threading.Event(), started=time.monotonic(),
                       progress=dict(phase='等待检查', checked_files=0, total_files=0, bytes_read=0, current_file=''))
            self.records[key] = row
            self.clients[client_id] = dict(revision=revision, key=key, updated=time.monotonic())
            if self.thread is None:
                self.thread = threading.Thread(target=self._work, name='archive-preflight', daemon=True)
                self.thread.start()
            self.condition.notify_all()
            return self._view(row)

    def cancel(self, client_id, revision):
        self.validate_client(client_id, revision)
        with self.condition:
            self._prune()
            previous = self.clients.get(client_id)
            if previous is None and len(self.clients) >= 128:
                raise ArchiveError('正在检查的页面较多，请稍后重新检查')
            if previous is None or revision >= previous['revision']:
                self.clients[client_id] = dict(revision=revision, updated=time.monotonic())
            for row in self.records.values():
                if row['client_id'] == client_id and row['revision'] <= revision and row['status'] in ('queued', 'checking'):
                    row['cancel'].set()
                    row['status'] = 'cancelled'
                    row['decisions'] = None
            self.condition.notify_all()
            return {'status':'cancelled'}

    def get(self, check_id):
        with self.condition:
            row = self.records.get(check_id)
            if row is None:
                raise ArchiveError('检查记录已过期，请重新检查')
            return self._view(row)

    def _work(self):
        while True:
            with self.condition:
                row = next((r for r in self.records.values() if r['status']=='queued'), None)
                while row is None and not self.stopped:
                    self.condition.wait()
                    row = next((r for r in self.records.values() if r['status']=='queued'), None)
                if self.stopped:
                    return
                row['status'] = 'checking'
                decisions = row['decisions']
            def progress(**update):
                if row['cancel'].is_set():
                    raise CheckCancelled()
                with self.condition:
                    row['progress']['bytes_read'] += update.pop('bytes_delta', 0)
                    row['progress'].update(update)
            try:
                result = self.runner(decisions, progress=progress)
                progress()
            except CheckCancelled:
                result = {'status':'cancelled'}
            except Exception:
                result = {'status':'blocked', 'errors':['检查未能完成，请重新检查；当前素材尚未归档。'], 'projects':[]}
            with self.condition:
                row['decisions'] = None
                row['result'] = result if not row['cancel'].is_set() else {'status':'cancelled'}
                row['status'] = row['result']['status']
                self.condition.notify_all()

    def close(self):
        with self.condition:
            self.stopped = True
            for row in self.records.values():
                row['cancel'].set()
            self.condition.notify_all()
        if self.thread:
            self.thread.join(30)
            if self.thread.is_alive():
                raise ArchiveError('文件检查尚未停止，不能释放服务状态')
