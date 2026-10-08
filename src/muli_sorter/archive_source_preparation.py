"""Prepare source metadata ahead of selection; never authorize a media write.

Cached identities are advisory. The executor must compare the actual opened
file with them at its mutation boundary. This cache never sets COPY_VERIFIED,
changes an Ingest receipt, or hashes media on a cache hit.
"""
from copy import deepcopy
import json
import sqlite3
import threading

from .archive_io import ArchiveError
from .archive_source import verify_sources
from .review import digest


class SourcePreparation:
    def __init__(self, service):
        self.service = service
        self.lock = threading.RLock()
        self.db = None
        self.last_error = None
        try:
            self.db = sqlite3.connect(str(service.state/'source-preparation-v1.sqlite3'),
                                      timeout=1, check_same_thread=False)
            self.db.execute('PRAGMA journal_mode=WAL')
            self.db.execute('PRAGMA synchronous=NORMAL')
            self.db.execute('CREATE TABLE IF NOT EXISTS prepared (key TEXT PRIMARY KEY, payload TEXT NOT NULL, checksum TEXT NOT NULL)')
            self.db.commit()
        except (OSError,sqlite3.Error) as exc:
            self._disable_cache(exc)
        self.stop = threading.Event()
        self.wake = threading.Event()
        self.pending = None
        self.thread = None
        self.last_scope = None

    def _disable_cache(self, exc):
        self.last_error = str(exc)
        connection, self.db = self.db, None
        if connection is not None:
            try:
                connection.close()
            except sqlite3.Error:
                pass

    def _key(self, unit, evidence):
        fields = ('unit_id','files','provenance','kind','capture_time','capture_date',
                  'timezone_trusted','device','_proxy_archive_authorized')
        material = {k:unit[k] for k in fields if k in unit}
        parent = unit.get('_companion_parent')
        if parent:
            material['parent'] = {k:parent[k] for k in fields if k in parent}
        return digest({'schema':'source-preparation/1','roots':self.service.identity,
                       'unit':material,'evidence':evidence})

    def verify(self, unit, cache, validated_signatures=None, runtime_provider=None):
        runtime = runtime_provider or self.service.runtime
        args = dict(production=self.service.production, reviewed_metadata=True,
                    cache=cache, companion_parent=unit.get('_companion_parent'))
        # Read current completion records, not the media. Changed receipts or
        # amendments yield a different key; stale identities never authorize IO.
        evidence = verify_sources(self.service.staging, unit, runtime,
                                  check_media=False, **args)
        key = self._key(unit, evidence)
        with self.lock:
            try:
                row = self.db.execute('SELECT payload, checksum FROM prepared WHERE key=?',(key,)).fetchone() if self.db else None
            except sqlite3.Error as exc:
                self._disable_cache(exc)
                row = None
        saved = None
        if row is not None:
            try:
                value = json.loads(row[0])
                if (isinstance(value, dict) and
                        isinstance(value.get('validated_signatures'), dict) and
                        digest({'key':key,'payload':value}) == row[1] and value['evidence'] == evidence):
                    saved = value
            except (ValueError, TypeError, KeyError):
                pass
        if saved is None:
            signatures = {}
            checked = verify_sources(self.service.staging, unit, runtime,
                                     check_media=True, validated_signatures=signatures, **args)
            if checked != evidence:
                raise ArchiveError('准备来源资料期间完成记录改变')
            saved = {'evidence':evidence,'validated_signatures':signatures}
            raw = json.dumps(saved,ensure_ascii=False,separators=(',',':'),allow_nan=False)
            with self.lock:
                if self.db is not None:
                    try:
                        self.db.execute('INSERT OR REPLACE INTO prepared VALUES (?,?,?)',
                                        (key,raw,digest({'key':key,'payload':saved})))
                        self.db.commit()
                    except sqlite3.Error as exc:
                        self._disable_cache(exc)
        if validated_signatures is not None:
            validated_signatures.update(deepcopy(saved['validated_signatures']))
        return evidence

    def schedule(self, units, *, scope_key=None):
        """Only current pending units; never traverse completed history."""
        with self.lock:
            if self.stop.is_set():
                return
            alive = self.thread is not None and self.thread.is_alive()
            if scope_key is not None and self.last_scope == scope_key and alive:
                return
            self.last_scope = deepcopy(scope_key)
            self.pending = tuple(units)
            if not alive:
                self.thread = threading.Thread(target=self._run, name='archive-source-preparation',daemon=True)
                self.thread.start()
        self.wake.set()

    def _run(self):
        from .material_triage import companion_links
        while not self.stop.is_set():
            self.wake.wait(1)
            self.wake.clear()
            with self.lock:
                units, self.pending = self.pending, None
            if units is None:
                continue
            try:
                by_id = {u['unit_id']:u for u in units}
                links, _ = companion_links({'units':list(units)})
            except (ValueError,KeyError,TypeError) as exc:
                self.last_error = str(exc)
                continue
            cache = {}
            for original in units:
                while (self.service.worker_busy.is_set() or self.service.preflight_lock.locked()) and not self.stop.wait(.2):
                    pass
                if self.stop.is_set():
                    return
                with self.lock:
                    if self.pending is not None:
                        break
                unit = dict(original)
                if unit['unit_id'] in links:
                    parent = by_id.get(links[unit['unit_id']])
                    if parent is None or parent.get('kind') not in ('photo','video','audio'):
                        continue
                    unit['_companion_parent'] = parent
                elif unit.get('kind') not in ('photo','video','audio'):
                    continue
                try:
                    self.verify(unit, cache)
                except Exception as exc:
                    # Keep unready units visible. A preparation error never
                    # creates an archive task or a positive transfer receipt.
                    self.last_error = str(exc)
                    continue

    def close(self):
        self.stop.set()
        self.wake.set()
        if self.thread:
            self.thread.join(30)
            if self.thread.is_alive():
                raise ArchiveError('来源资料准备仍在停止中')
        with self.lock:
            if self.db is not None:
                self.db.close()
                self.db = None
