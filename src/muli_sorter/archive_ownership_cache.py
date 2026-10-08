"""Scoped ownership recovery and the explicit full-repair implementation.

Normal archive runs validate only the selected unit IDs and the small durable
pending-intent journal. The archive index and job intents remain authoritative;
the metadata cache is only an old full-repair artifact. Full inventory is kept
behind ``recover_full`` for explicit offline maintenance and migration.
"""
import os
import re
import stat
import time
from .archive_io import ArchiveError, atomic_json, open_file, persistent_identity
from .review import digest
from .intake import decode

NAME = 'ownership-metadata-cache-v2.json'
SCHEMA = 'ownership-metadata-cache/2'
READY_NAME = 'ownership-ready-v1.json'
READY_SCHEMA = 'ownership-ready/1'
PENDING_NAME = 'ownership-pending-v2.json'
PENDING_SCHEMA = 'ownership-pending/2'
PATTERN = re.compile(r'job-([0-9a-f]{64})\.json')


def signature(st):
    if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
        raise ArchiveError('归档执行记录不是唯一链接的普通文件')
    return [st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns, st.st_nlink]


def inventory(fd):
    result = {}
    with os.scandir(fd) as entries:
        for count, entry in enumerate(entries, 1):
            if count > 100_000:
                raise ArchiveError('状态目录过大，停止自动恢复')
            if PATTERN.fullmatch(entry.name):
                result[entry.name] = signature(os.stat(entry.name, dir_fd=fd, follow_symlinks=False))
    return result


def read_identity(fd, name, expected):
    file = open_file(fd, name)
    try:
        if signature(os.fstat(file)) != expected:
            raise ArchiveError('核对期间归档记录发生变化')
        with os.fdopen(os.dup(file), 'rb') as stream:
            data = stream.read(64 * 1024 * 1024 + 1)
        if len(data) > 64 * 1024 * 1024:
            raise ArchiveError('状态记录过大')
        identity = decode(data)['identity']
        jid, uid = digest(identity), identity['unit_id']
        if (not isinstance(uid, str) or not uid or name != 'job-' + jid + '.json' or
                signature(os.fstat(file)) != expected or
                signature(os.stat(name, dir_fd=fd, follow_symlinks=False)) != expected):
            raise ArchiveError('已登记任务被修改或核对期间发生变化')
        return uid, jid
    finally:
        os.close(file)


def _read_pending(fd, binding):
    from .archive import record
    pending = record(fd, PENDING_NAME, None)
    if pending is None:
        return {}
    if (not isinstance(pending, dict) or
            set(pending) != {'schema', 'binding', 'entries', 'digest'} or
            pending['schema'] != PENDING_SCHEMA or pending['binding'] != binding or
            not isinstance(pending['entries'], dict) or
            pending['digest'] != digest({k:v for k,v in pending.items() if k != 'digest'})):
        raise ArchiveError('归档待登记意图日志格式异常，需要离线修复')
    result = {}
    for uid, row in pending['entries'].items():
        if (not isinstance(uid, str) or not uid or
                not isinstance(row, dict) or set(row) != {'job_id', 'base_index_digest', 'base_entry_count'} or
                not isinstance(row['job_id'], str) or not PATTERN.fullmatch('job-' + row['job_id'] + '.json') or
                not isinstance(row['base_index_digest'], str) or not row['base_index_digest'] or
                type(row['base_entry_count']) is not int or row['base_entry_count'] < 0):
            raise ArchiveError('归档待登记意图日志内容异常，需要离线修复')
        result[uid] = row
    return result


def _write_pending(fd, binding, entries):
    data = {'schema':PENDING_SCHEMA, 'binding':binding, 'entries':dict(entries)}
    atomic_json(fd, PENDING_NAME, {**data, 'digest':digest(data)})


def reserve_intent(fd, uid, jid, index=None):
    """Durably reserve a unit before its job intent is written."""
    binding = persistent_identity(fd)
    entries = _read_pending(fd, binding)
    old = entries.get(uid)
    if old is not None and old['job_id'] != jid:
        raise ArchiveError('归档待登记意图与当前单元冲突，需要离线修复')
    if old is None:
        if index is None:
            from .archive import record
            index = record(fd, 'archive-index.json', {})
        if (not isinstance(index, dict) or
                any(not isinstance(k, str) or not isinstance(v, str) for k, v in index.items())):
            raise ArchiveError('归属索引格式异常，需要离线修复')
        if uid in index:
            raise ArchiveError('待登记单元已经存在归属，需要离线修复')
        entries[uid] = {'job_id':jid, 'base_index_digest':digest(index),
                        'base_entry_count':len(index)}
    _write_pending(fd, binding, entries)


def reserve_many(fd, jobs, index):
    """Reserve a direct-move batch with one pending-journal rewrite."""
    if not isinstance(jobs, dict) or not isinstance(index, dict):
        raise ArchiveError('归档待登记批次格式异常')
    binding = persistent_identity(fd)
    entries = _read_pending(fd, binding)
    base_digest, base_count = digest(index), len(index)
    for uid, jid in jobs.items():
        if (not isinstance(uid, str) or not uid or not isinstance(jid, str) or
                not PATTERN.fullmatch('job-' + jid + '.json')):
            raise ArchiveError('归档待登记批次格式异常')
        old = entries.get(uid)
        if old is not None and old['job_id'] != jid:
            raise ArchiveError('归档待登记批次与当前单元冲突，需要离线修复')
        if uid in index:
            raise ArchiveError('待登记批次包含已有归属，需要离线修复')
        # Recovery may already have committed other members of this batch.
        # Bind the remaining reservation to that newly verified baseline.
        entries[uid] = {'job_id':jid, 'base_index_digest':base_digest,
                        'base_entry_count':base_count}
    if jobs:
        _write_pending(fd, binding, entries)


def clear_intent(fd, uid, jid):
    """Drop a pending reservation only after archive-index is durable."""
    binding = persistent_identity(fd)
    entries = _read_pending(fd, binding)
    row = entries.get(uid)
    if row is None:
        return
    if row['job_id'] != jid:
        raise ArchiveError('归档待登记意图已被替换，需要离线修复')
    del entries[uid]
    if entries:
        _write_pending(fd, binding, entries)
    else:
        try:
            os.unlink(PENDING_NAME, dir_fd=fd)
            os.fsync(fd)
        except FileNotFoundError:
            pass


def clear_many(fd, jobs):
    """Clear a direct-move batch with one pending-journal rewrite."""
    if not isinstance(jobs, dict):
        raise ArchiveError('归档待清理批次格式异常')
    binding = persistent_identity(fd)
    entries = _read_pending(fd, binding)
    for uid, jid in jobs.items():
        row = entries.get(uid)
        if row is None:
            continue
        if row['job_id'] != jid:
            raise ArchiveError('归档待登记批次已被替换，需要离线修复')
    for uid in jobs:
        entries.pop(uid, None)
    if entries:
        _write_pending(fd, binding, entries)
    elif jobs:
        try:
            os.unlink(PENDING_NAME, dir_fd=fd)
            os.fsync(fd)
        except FileNotFoundError:
            pass


def write_ready(fd, index):
    """Write the one-time full-repair barrier used by scoped recovery."""
    binding = persistent_identity(fd)
    data = {'schema':READY_SCHEMA, 'binding':binding, 'index_digest':digest(index),
            'entry_count':len(index)}
    atomic_json(fd, READY_NAME, {**data, 'digest':digest(data)})


def initialize_ready_from_cache(fd):
    """Create the scoped marker from a completed legacy full-read cache.

    This is an explicit idle maintenance entry point. It decodes the old
    cache once, checks that its UID/JID projection exactly equals the durable
    archive-index, and never reads media or scans job files.
    """
    from .archive import record
    binding = persistent_identity(fd)
    cache = record(fd, NAME, None)
    if (not isinstance(cache, dict) or set(cache) != {'schema', 'binding', 'entries', 'digest'} or
            cache['schema'] != SCHEMA or cache['binding'] != binding or
            not isinstance(cache['entries'], dict) or
            cache['digest'] != digest({k:v for k,v in cache.items() if k != 'digest'})):
        raise ArchiveError('旧归属全量核对缓存无效，需要重新执行离线维护')
    projection = {}
    for name, row in cache['entries'].items():
        match = PATTERN.fullmatch(name)
        if (not match or not isinstance(row, list) or len(row) != 2 or
                not isinstance(row[1], str) or not row[1]):
            raise ArchiveError('旧归属全量核对缓存内容异常，需要重新执行离线维护')
        if row[1] in projection and projection[row[1]] != match[1]:
            raise ArchiveError('旧归属全量核对缓存存在重复归属，需要离线修复')
        projection[row[1]] = match[1]
    index = record(fd, 'archive-index.json', {})
    if index != projection:
        raise ArchiveError('旧归属全量核对缓存与归属索引不一致，需要离线修复')
    write_ready(fd, index)
    return index


def _require_ready(fd, binding, index, pending):
    from .archive import record
    ready = record(fd, READY_NAME, None)
    if (not isinstance(ready, dict) or set(ready) != {'schema', 'binding', 'index_digest', 'entry_count', 'digest'} or
            ready['schema'] != READY_SCHEMA or ready['binding'] != binding or
            not isinstance(ready['index_digest'], str) or not ready['index_digest'] or
            type(ready['entry_count']) is not int or ready['entry_count'] < 0 or
            ready['digest'] != digest({k:v for k,v in ready.items() if k != 'digest'})):
        raise ArchiveError('归档归属索引尚未完成一次性维护核对，需要离线修复')
    if ready['index_digest'] == digest(index) and ready['entry_count'] == len(index):
        return
    # The only permitted mismatch is the durable crash window after an index
    # write and before its marker refresh. Pending rows carry the exact
    # pre-write index digest, so unrelated changes cannot pass this check.
    baseline = dict(index)
    for uid, row in pending.items():
        if (row['base_index_digest'] != ready['index_digest'] or
                row['base_entry_count'] != ready['entry_count'] or
                (uid in baseline and baseline[uid] != row['job_id'])):
            raise ArchiveError('归属索引代际与维护标记不一致，需要离线修复')
        baseline.pop(uid, None)
    if digest(baseline) != ready['index_digest'] or len(baseline) != ready['entry_count']:
        raise ArchiveError('归属索引代际与维护标记不一致，需要离线修复')


def _selected_scope(selected):
    if not isinstance(selected, dict):
        raise ArchiveError('日常归档必须提供本次所选归属范围')
    scope = {}
    for uid, jid in selected.items():
        if (not isinstance(uid, str) or not uid or not isinstance(jid, str) or
                not PATTERN.fullmatch('job-' + jid + '.json')):
            raise ArchiveError('本次归属范围格式异常')
        scope[uid] = jid
    return scope


def _check_selected(fd, uid, jid, *, allow_missing=False):
    name = 'job-' + jid + '.json'
    try:
        found_uid, found_jid = read_identity(fd, name, signature(os.stat(name, dir_fd=fd, follow_symlinks=False)))
    except FileNotFoundError:
        if allow_missing:
            return False
        raise ArchiveError('归属索引指向的归档意图缺失，需要离线修复')
    if found_uid != uid or found_jid != jid:
        raise ArchiveError('所选单元归属意图与索引不一致，需要离线修复')
    return True


def recover(fd, selected, *, progress=None, require_ready=True, allow_pending_without_job=None):
    """Validate only selected ownership and reconcile its pending intent.

    ``selected`` maps unit_id to the expected job digest. Missing entries are
    allowed for a genuinely new unit. A candidate job without a pending
    reservation is rejected, which closes the crash window of old intent-first
    writes instead of silently adopting or overwriting it.
    """
    from .archive import record
    started = time.monotonic()
    emit = progress or (lambda **_: None)
    binding = persistent_identity(fd)
    scope = _selected_scope(selected)
    deferred = set() if allow_pending_without_job is None else set(allow_pending_without_job)
    if not deferred.issubset(scope):
        raise ArchiveError('待登记恢复范围必须是本次 direct journal 的精确子集')
    index = record(fd, 'archive-index.json', {})
    if (not isinstance(index, dict) or
            any(not isinstance(k, str) or not isinstance(v, str) or
                not PATTERN.fullmatch('job-' + v + '.json') for k, v in index.items())):
        raise ArchiveError('归属索引格式异常，需要离线修复')
    pending = _read_pending(fd, binding)
    if require_ready:
        _require_ready(fd, binding, index, pending)
        if digest(index) != record(fd, READY_NAME)['index_digest'] and not set(pending).issubset(scope):
            raise ArchiveError('归属索引代际与本次待登记范围不一致，需要离线修复')
    emit(checked_records=0, total_records=len(scope), reused_records=0, parsed_records=0,
         phase='checking_selected_ownership', reason='scoped')
    changed = False
    parsed = 0
    for i, (uid, jid) in enumerate(scope.items(), 1):
        if uid in index:
            if index[uid] != jid:
                raise ArchiveError('所选单元已有其他归属，停止重复归档')
            _check_selected(fd, uid, jid)
            parsed += 1
            if uid in pending:
                if pending[uid]['job_id'] != jid:
                    raise ArchiveError('所选单元待登记意图与索引冲突，需要离线修复')
                del pending[uid]
                changed = True
        elif uid in pending:
            if pending[uid]['job_id'] != jid:
                raise ArchiveError('所选单元待登记意图与本次计划冲突，需要离线修复')
            if not _check_selected(fd, uid, jid, allow_missing=uid in deferred):
                # direct_move reserves its complete journal scope before
                # writing per-unit intents. A missing job here is an
                # unstarted member of that exact durable scope; keep the
                # reservation so a retry cannot claim the UID elsewhere.
                emit(checked_records=i, total_records=len(scope), reused_records=0,
                     parsed_records=parsed, phase='checking_selected_ownership', reason='scoped')
                continue
            index[uid] = jid
            del pending[uid]
            changed = True
            parsed += 1
        else:
            # A job file with this exact digest was written without the new
            # reservation protocol. Do not adopt it or risk a duplicate.
            try:
                os.stat('job-' + jid + '.json', dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise ArchiveError('发现未登记归档意图，需要离线修复')
        emit(checked_records=i, total_records=len(scope), reused_records=0,
             parsed_records=parsed, phase='checking_selected_ownership', reason='scoped')
    if changed:
        atomic_json(fd, 'archive-index.json', index)
        write_ready(fd, index)
        if pending:
            _write_pending(fd, binding, pending)
        else:
            try:
                os.unlink(PENDING_NAME, dir_fd=fd)
                os.fsync(fd)
            except FileNotFoundError:
                pass
    emit(checked_records=len(scope), total_records=len(scope), reused_records=0,
         parsed_records=parsed, phase='complete', reason='scoped',
         elapsed_seconds=round(time.monotonic()-started, 3), cache_saved=False)
    return index


def recover_full(fd, *, progress=None):
    from .archive import record
    started = time.monotonic()
    emit = progress or (lambda **_: None)
    binding = persistent_identity(fd)
    reason = 'unchanged_or_incremental'
    try:
        cache = record(fd, NAME)
        if (not isinstance(cache, dict) or set(cache) != {'schema', 'binding', 'entries', 'digest'} or
                cache['schema'] != SCHEMA or cache['binding'] != binding or
                not isinstance(cache['entries'], dict) or
                cache['digest'] != digest({k:v for k,v in cache.items() if k != 'digest'})):
            raise ValueError('invalid cache')
        for name, row in cache['entries'].items():
            if (not PATTERN.fullmatch(name) or not isinstance(row, list) or len(row) != 2 or
                    not isinstance(row[1], str) or not row[1] or not isinstance(row[0], list) or
                    len(row[0]) != 6 or any(type(v) is not int or v < 0 for v in row[0])):
                raise ValueError('invalid cached record')
        previous = cache['entries']
    except (OSError, ValueError, KeyError, TypeError):
        previous = {}
        reason = 'cache_missing_or_invalid_full_read'
    # Read this on every call; the cache is never an alternate authority for it.
    index = record(fd, 'archive-index.json', {})
    if not isinstance(index, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k,v in index.items()):
        raise ArchiveError('归属索引格式异常')
    old_index = dict(index)
    emit(checked_records=0, total_records=None, reused_records=0, parsed_records=0,
         phase='checking_record_metadata', reason=reason)
    observed = inventory(fd)
    if set(previous) - set(observed):
        previous = {}
        reason = 'record_removed_full_read'
    updated = {}
    parsed = reused = 0
    emit(checked_records=0, total_records=len(observed), reused_records=0, parsed_records=0,
         phase='checking_record_ownership', reason=reason)
    for i, (name, sig) in enumerate(observed.items(), 1):
        prior = previous.get(name)
        if prior is not None and prior[0] == sig:
            uid, jid = prior[1], PATTERN.fullmatch(name)[1]
            reused += 1
        else:
            uid, jid = read_identity(fd, name, sig)
            parsed += 1
        if uid in index and index[uid] != jid:
            raise ArchiveError('已登记任务互相冲突或被修改')
        index[uid] = jid
        updated[name] = [sig, uid]
        # The caller throttles persistence. Counters do not cause media reads.
        emit(checked_records=i, total_records=len(observed), reused_records=reused,
             parsed_records=parsed, phase='checking_record_ownership', reason=reason)
    if inventory(fd) != observed:
        raise ArchiveError('核对期间归档记录范围或身份发生变化')
    if index != old_index:
        atomic_json(fd, 'archive-index.json', index)
    saved = True
    if updated != previous or reason != 'unchanged_or_incremental':
        data = {'schema':SCHEMA, 'binding':binding, 'entries':updated}
        try:
            atomic_json(fd, NAME, {**data, 'digest':digest(data)})
        except OSError:
            saved = False  # Validation passed; next request must rebuild.
    emit(checked_records=len(observed), total_records=len(observed), reused_records=reused,
         parsed_records=parsed, phase='complete', reason=reason,
         elapsed_seconds=round(time.monotonic()-started, 3), cache_saved=saved)
    write_ready(fd, index)
    return index


def job_progress(service, job):
    last = [0.0]
    def report(**value):
        if service.stop_event.is_set():
            raise ArchiveError('归档服务正在停止，尚未开始新的素材操作')
        job['ownership_check'] = value
        total = value.get('total_records')
        job['phase'] = ('核对本次归属记录身份' if total is None else
                        f"核对本次归属：{value['checked_records']} / {total}，"
                        f"重读 {value['parsed_records']}")
        now = time.monotonic()
        if value.get('phase') == 'complete' or now-last[0] >= 1:
            service._save(job)
            last[0] = now
    return report
