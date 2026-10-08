"""Read-only projection of verified archive receipts onto the current review.

The classification snapshot and archive requests remain immutable. A receipt,
not a missing staging file or a task's aggregate count, establishes completion.
"""
from collections import OrderedDict
from pathlib import Path, PurePosixPath
import os
import re
import threading

from .intake import relative
from .order_feed_io import atomic_json, read_json, digest
from .external_archive_history import project_external_history


HEX = r'[a-f0-9]{64}'
HISTORY_CACHE_ITEMS = 16384
HISTORY_CACHE_BYTES = 64 * 1024 * 1024
# A live queue already holds about 48k archived units, so the smaller cap made
# every refresh evict and rebuild its own newest projections.
PROJECTED_CACHE_UNITS = 98304
# Durable per-job projection index. It is derived state only: every entry is
# reused only while each source record still has the same inode, size and
# timestamps, and it is rebuilt from the receipts whenever that fails.
HISTORY_INDEX_VERSION = 'archive-history-index/2'
HISTORY_INDEX_DIR = 'display-history-index'


def file_identity(files):
    rows = [(f['source_path'], f['name'], f['size_bytes'], f['blake3']) for f in files]
    if not rows or len({r[0] for r in rows}) != len(rows):
        raise ValueError('素材文件范围不完整或重复')
    return sorted(rows)


class ArchiveHistory:
    def __init__(self, state, identity, label, production):
        self.state, self.identity = Path(state), identity
        self.label, self.production = label.rstrip('/'), production
        self.cache = OrderedDict()
        self.cache_bytes = 0
        self.reports = OrderedDict()
        self.lock = threading.Lock()
        self.projected = OrderedDict()
        self.retained_projected = OrderedDict()
        self.projected_units = 0
        self._dependencies = None
        self._dependency_changed = False
        self.index_root = self._index_root()
        self.index_token = digest({'version': HISTORY_INDEX_VERSION, 'identity': identity,
                                   'label': self.label, 'production': self.production})

    def read(self, path):
        info = path.lstat()
        key = (info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        if self._dependencies is not None:
            if path in self._dependencies and self._dependencies[path] != key:
                self._dependency_changed = True
            self._dependencies[path] = key
        previous = self.cache.get(path)
        if previous and previous[0] == key:
            self.cache.move_to_end(path)
            return previous[1]
        data = read_json(path)
        if previous:
            self.cache_bytes -= previous[0][1]
        self.cache[path] = (key, data)
        self.cache_bytes += info.st_size
        self.cache.move_to_end(path)
        while len(self.cache) > HISTORY_CACHE_ITEMS or self.cache_bytes > HISTORY_CACHE_BYTES:
            _, (old_key, _) = self.cache.popitem(last=False)
            self.cache_bytes -= old_key[1]
        return data

    def snapshot(self, model):
        with self.lock:
            current = {u['unit_id']: digest(file_identity(u['files'])) for u in model['units']}
            archive = self._snapshot(model['report_id'], current)
            copies = self._content_copies(model['units'], archive['archived_units'], current)
            # Scope digests and content copies share one entry so a later lookup
            # can never see one without the other.
            self.reports[model['report_id']] = {'current': current, 'copies': copies}
            self.reports.move_to_end(model['report_id'])
            while len(self.reports) > 16:
                self.reports.popitem(last=False)
            return self._with_content_copies(archive, copies)

    def for_report(self, report_id):
        """Keep a page's identity stable when newly created projects trigger a scan."""
        with self.lock:
            entry = self.reports.get(report_id)
            if entry is None:
                raise ValueError('当前页面的素材报告已过期，请先保存草稿，再刷新页面核对归档状态。')
            self.reports.move_to_end(report_id)
            return self._with_content_copies(self._snapshot(report_id, entry['current']),
                                             entry['copies'])

    @staticmethod
    def _content_copies(units, archived_rows, current):
        """Current units whose file content already lives in an archived receipt.

        A card can be copied again before it is formatted, so the same photo can
        arrive under a new batch path with a new resolved identity. Content
        identity (blake3 plus size) is what makes it the same material.
        """
        known = {row['unit_id'] for row in archived_rows}
        index = {}
        for row in archived_rows:
            for entry in row.get('files') or []:
                key = (entry.get('blake3'), entry.get('size_bytes'))
                if key[0] and key not in index:
                    index[key] = row
        result = {}
        for unit in units:
            uid = unit['unit_id']
            if uid in known or uid not in current:
                continue
            keys = [(f.get('blake3'), f.get('size_bytes')) for f in unit['files']]
            if not keys or any(not key[0] for key in keys):
                continue
            hits = [index.get(key) for key in keys]
            if all(hits):
                result[uid] = hits[0]
        return result

    @staticmethod
    def _with_content_copies(archive, copies):
        if not copies:
            return archive
        # A retained (copied-but-unverified) unit is already hidden by its own
        # list, and the page requires the two lists to stay disjoint.
        retained = set(archive.get('retained_unit_ids') or [])
        extra = []
        for uid, origin in copies.items():
            if uid in retained:
                continue
            extra.append({'unit_id': uid, 'job_id': origin['job_id'], 'mode': origin['mode'],
                          'completed_at': origin['completed_at'], 'in_current_model': True,
                          'content_copy_of': origin['unit_id'],
                          'project': dict(origin['project']),
                          'file_count': origin.get('file_count', len(origin.get('files') or [])),
                          'files': [dict(row) for row in origin.get('files') or []]})
        rows = list(archive['archived_units']) + extra
        rows.sort(key=lambda row: (row['completed_at'], row['unit_id']), reverse=True)
        return {**archive, 'archived_units': rows, 'content_copy_units': len(extra)}

    def for_units(self, model, unit_ids):
        # Only companion parents explicitly needed by this submission. Do not
        # inspect unrelated archive destinations or pollute full-page caches.
        with self.lock:
            current = {u['unit_id']: digest(file_identity(u['files']))
                       for u in model['units'] if u['unit_id'] in unit_ids}
            return self._snapshot(model['report_id'], current, selected_only=True)

    def _snapshot(self, report_id, current, selected_only=False):
        archived, warnings, move_contexts = {}, [], {}
        retained_projections = []
        # Reuse a completed job's compact projection only while every receipt
        # and cleanup journal has the same inode, size, mtime and ctime.
        live_jobs = set()
        for path in sorted((self.state / 'requests').glob('job-*.json')):
            if not re.fullmatch('job-' + HEX + r'\.json', path.name):
                continue
            live_jobs.add(path)
            retained = self.retained_projected.get(path)
            if retained is not None and self._unchanged(retained['dependencies']):
                self.retained_projected.move_to_end(path)
                if not selected_only:
                    retained_projections.append(retained['projection'])
                continue
            self.retained_projected.pop(path, None)
            cached = None if selected_only else self.projected.get(path)
            if cached is not None and not self._unchanged(cached['dependencies']):
                cached = None
            if cached is None and not selected_only:
                # A restart or an evicted entry reloads the compact index
                # instead of reopening every receipt of that job again.
                cached = self._load_index(path)
                if cached is not None:
                    self._remember_projection(path, cached)
            if cached is not None:
                self.projected.move_to_end(path)
                items = cached['items']
            else:
                if not selected_only:self._drop_projection(path)
                items, scopes, dependencies = [], {}, {}
                before_warnings = len(warnings)
                self._dependencies = dependencies
                self._dependency_changed = False
                job = None
                try:
                    job = self.read(path)
                    if job['job_id'] != path.stem[4:] or job.get('example_data') is not (not self.production):
                        raise ValueError('归档任务身份不符')
                    if job.get('status') == 'copied_unverified':
                        # A display exclusion only: never return these entries
                        # as verified archive/companion-parent evidence.
                        if selected_only:
                            continue
                        from .archive_retained_copies import retained_projection
                        request_path = self.state / 'requests' / ('request-' + job['job_id'] + '.json')
                        try:
                            projection = retained_projection(job, self.read(request_path), self.identity)
                        finally:
                            # Keep compact scopes rather than the historical
                            # full request model; unchanged polls stat 2 records.
                            previous = self.cache.pop(request_path, None)
                            if previous:
                                self.cache_bytes -= previous[0][1]
                        if self._dependency_changed or not self._unchanged(dependencies):
                            raise ValueError('来源保留记录在读取期间变化')
                        self.retained_projected[path] = {'dependencies': dict(dependencies), 'projection': projection}
                        while (len(self.retained_projected) > 16 or
                               sum(len(row['projection']['units']) for row in self.retained_projected.values()) > PROJECTED_CACHE_UNITS):
                            self.retained_projected.popitem(last=False)
                        # This job may itself contain a large historical
                        # outcomes snapshot. Keep only its compact projection.
                        for dependency in dependencies:
                            previous = self.cache.pop(dependency, None)
                            if previous:
                                self.cache_bytes -= previous[0][1]
                        retained_projections.append(projection)
                        continue
                    mode = job.get('archive_options', {}).get('mode', 'copy')
                    if mode not in ('copy', 'move'):
                        raise ValueError('归档方式未知')
                    for outcome in job.get('outcomes', []):
                        if selected_only and outcome.get('unit_id') not in current:
                            continue
                        if outcome.get('status') != 'completed':
                            continue
                        try:
                            item = self._item(job, outcome, mode, {}, warnings,
                                              move_contexts, scope_digests=scopes)
                            if item is not None:
                                items.append((item, scopes[item["unit_id"]]))
                        except (OSError, ValueError, KeyError, TypeError, AttributeError):
                            warnings.append('部分归档记录暂时无法核对，对应素材仍保留待处理；请查看归档任务记录。')
                except (OSError, ValueError, KeyError, TypeError, AttributeError):
                    warnings.append('部分归档记录暂时无法核对，对应素材仍保留待处理；请查看归档任务记录。')
                finally:
                    self._dependencies = None
                if (not selected_only and job is not None and job.get('status') == 'completed'
                        and len(warnings) == before_warnings and not self._dependency_changed
                        and self._unchanged(dependencies)
                        and len(items) <= PROJECTED_CACHE_UNITS):
                    self._remember_projection(path, {'items': items, 'dependencies': dependencies})
                    try:
                        self._store_index(path, items, dependencies)
                    except (OSError, ValueError, TypeError):
                        pass  # The durable index only accelerates later reads.
                    # The compact projection replaces large parsed journals and
                    # receipt bodies; do not retain both representations.
                    for dependency in dependencies:
                        previous = self.cache.pop(dependency, None)
                        if previous:
                            self.cache_bytes -= previous[0][1]
            for original, scope_digest in items:
                item = dict(original, project=dict(original["project"]),
                            files=[dict(row) for row in original["files"]])
                uid, when = item['unit_id'], item['completed_at']
                item['in_current_model'] = uid in current and current[uid] == scope_digest
                if uid in current and not item['in_current_model']:
                    warnings.append('一项归档记录与当前素材范围不同，相关素材仍保留待处理。')
                previous = archived.get(uid)
                if previous is None or (when, item['job_id']) > (previous['completed_at'], previous['job_id']):
                    archived[uid] = item
        for path in set(self.retained_projected) - live_jobs:
            self.retained_projected.pop(path, None)
        for path in set(self.projected) - live_jobs:
            self._drop_projection(path)
        if not selected_only:
            self._prune_index(live_jobs)
        external, external_warnings = project_external_history(
            self.state, self.identity, self.label, self.production, current, reader=self.read, selected_only=selected_only)
        warnings.extend(external_warnings)
        # A service-generated receipt is authoritative when both records name
        # the same unit. External receipts only fill a missing history item.
        for item in external:
            if item['unit_id'] not in archived:
                archived[item['unit_id']] = item
        from .archive_retained_copies import retained_summary
        retained, retained_warnings = retained_summary(retained_projections, current, archived)
        warnings.extend(retained_warnings)
        return {**retained, 'report_id': report_id,
                'archived_units': sorted(archived.values(), key=lambda r: (r['completed_at'], r['unit_id']), reverse=True),
                'warnings': list(dict.fromkeys(warnings))}

    def _index_root(self):
        override = os.environ.get('MULI_ARCHIVE_HISTORY_INDEX')
        if override:
            path = Path(override)
            if not path.is_absolute():
                raise ValueError('归档历史索引目录必须是绝对路径')
            return path
        return self.state / HISTORY_INDEX_DIR

    def _index_path(self, path):
        name = path.name
        if not re.fullmatch('job-' + HEX + r'\.json', name):
            raise ValueError('归档任务文件名无效')
        return self.index_root / (name[4:-5] + '.json')

    @staticmethod
    def _index_dependencies(dependencies):
        return [[str(path), list(token)] for path, token in sorted(dependencies.items())]

    @staticmethod
    def _index_dependency_map(rows, root):
        result = {}
        if not isinstance(rows, list):
            raise ValueError('归档历史索引依赖格式无效')
        for row in rows:
            if not isinstance(row, list) or len(row) != 2:
                raise ValueError('归档历史索引依赖格式无效')
            name, token = row
            path = Path(name)
            if (not isinstance(name, str) or not path.is_absolute() or not isinstance(token, list)
                    or len(token) != 4 or any(type(value) is not int for value in token)):
                raise ValueError('归档历史索引依赖格式无效')
            if root not in path.parents:
                raise ValueError('归档历史索引依赖越界')
            result[path] = tuple(token)
        return result

    def _load_index(self, path):
        """Reuse a persisted projection only while every source record is unchanged."""
        try:
            document = read_json(self._index_path(path))
        except (OSError, ValueError, KeyError, TypeError):
            return None
        try:
            if (document.get('version') != HISTORY_INDEX_VERSION
                    or document.get('token') != self.index_token
                    or document.get('job_id') != path.name[4:-5]):
                return None
            dependencies = self._index_dependency_map(document['dependencies'], self.state)
            items = []
            for row in document['items']:
                if not isinstance(row, list) or len(row) != 2:
                    raise ValueError('归档历史索引条目格式无效')
                item, scope = row
                if not isinstance(item, dict) or not isinstance(scope, str):
                    raise ValueError('归档历史索引条目格式无效')
                items.append((item, scope))
        except (KeyError, TypeError, ValueError):
            return None
        if not self._unchanged(dependencies):
            return None
        return {'items': items, 'dependencies': dependencies}

    def _store_index(self, path, items, dependencies):
        document = {'version': HISTORY_INDEX_VERSION, 'token': self.index_token,
                    'job_id': path.name[4:-5],
                    'dependencies': self._index_dependencies(dependencies),
                    'items': items}
        atomic_json(self._index_path(path), document)

    def _prune_index(self, live_jobs):
        live = {path.name[4:-5] + '.json' for path in live_jobs}
        try:
            entries = list(self.index_root.iterdir())
        except OSError:
            return
        for entry in entries:
            if entry.name in live or not re.fullmatch(HEX + r'\.json', entry.name):
                continue
            try:
                if entry.is_symlink() or not entry.is_file():
                    continue
                entry.unlink()
            except OSError:
                pass

    def _remember_projection(self, path, cached):
        self.projected[path] = cached
        self.projected_units += len(cached['items'])
        while self.projected_units > PROJECTED_CACHE_UNITS or len(self.projected) > 64:
            self._drop_projection(next(iter(self.projected)))

    @staticmethod
    def _unchanged(dependencies):
        try:
            for path, expected in dependencies.items():
                info = path.lstat()
                if (info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns) != expected:
                    return False
            return True
        except OSError:
            return False

    def _drop_projection(self, path):
        previous = self.projected.pop(path, None)
        if previous is not None:
            self.projected_units -= len(previous['items'])

    def _move_context(self, job, contexts):
        path = self.state / 'requests' / ('move-' + job['job_id'] + '.json')
        info = path.lstat()
        token = (info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        cached = contexts.get(path)
        if cached is not None and cached[0] == token:
            return cached[1]
        journal = self.read(path)
        owner = journal['identity']
        if (owner['job_id'] != job['job_id'] or owner['roots'] != self.identity
                or owner['request_digest'] != job['request_digest']):
            raise ValueError('移动清理回执身份不符')
        planned = {r['source_path']: r for r in owner['files']}
        removed = {r['source_path'] for r in journal['files'] if r['state'] == 'removed'}
        after = path.lstat()
        if (after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns) != token:
            raise ValueError('核对期间移动清理回执变化，请刷新归档状态')
        context = (planned, removed)
        contexts[path] = (token, context)
        return context

    def _item(self, job, outcome, mode, current, warnings, move_contexts=None, scope_digests=None):
        name = outcome.get('receipt', '')
        if not re.fullmatch('receipt-' + HEX + r'\.json', name):
            raise ValueError('归档回执编号无效')
        receipt = self.read(self.state / 'units' / name)
        uid = outcome['unit_id']
        rows = receipt['files']
        if (receipt.get('status') != 'completed' or receipt.get('unit_id') != uid
                or receipt.get('job_id') != name[8:-5]
                or receipt.get('example_data') is not (not self.production)
                or not receipt.get('completed_at')
                or receipt.get('file_count') != len(rows)
                or outcome.get('files') != len(rows)
                or not all(r.get('published') and r.get('target_signature') for r in rows)):
            raise ValueError('归档回执未完整核验')
        identity = file_identity(rows)
        project = receipt['project']
        project_path = '/'.join(relative(project['path']))
        for row in rows:
            target = '/'.join(relative(row['target_path']))
            if not target.startswith(project_path + '/'):
                raise ValueError('归档目标不属于回执项目')
        when = receipt['completed_at']
        if mode == 'move':
            if not job.get('cleanup_started'):
                return None  # A copied destination alone is not a completed move.
            planned, removed = self._move_context(job, move_contexts if move_contexts is not None else {})
            if not all(planned.get(r['source_path']) == r for r in rows):
                raise ValueError('移动清理回执范围不符')
            if not all(r['source_path'] in removed for r in rows):
                return None
            when = job.get('completed_at') or job['updated_at']
        if scope_digests is not None:
            scope_digests[uid] = digest(identity)
        unit = current.get(uid)
        matches = unit is not None and unit == digest(identity)
        if unit is not None and not matches:
            warnings.append('一项归档记录与当前素材范围不同，相关素材仍保留待处理。')
        item = {'unit_id': uid, 'job_id': job['job_id'], 'mode': mode,
                'completed_at': when, 'in_current_model': matches,
                'project': {'name': project['name'], 'path': self.label + '/' + project_path},
                'file_count': len(rows),
                'files': [{'name': PurePosixPath(r['name']).name,
                           'target_path': self.label + '/' + r['target_path'],
                           'size_bytes': r['size_bytes'],
                           'blake3': r.get('blake3')} for r in rows]}
        return item
