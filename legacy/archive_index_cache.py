"""Derived ownership lookup; unchanged intent files need no JSON reparse.

Caller must hold the existing archive/staging locks. Every intent is still
opened no-follow and its complete file signature checked on every call.
Intents and the original ownership index remain authoritative.
"""
import os
import re
import stat

from .archive import record
from .archive_io import ArchiveError, atomic_json, open_file, persistent_identity
from .intake import decode
from .review import digest

NAME = 'ownership-read-cache.json'
SCHEMA = 'ownership-read-cache/1'
PATTERN = re.compile(r'job-([0-9a-f]{64})\.json')


def sig(value):
    if not stat.S_ISREG(value.st_mode) or value.st_nlink != 1:
        raise ArchiveError('归档执行记录不是唯一链接的普通文件')
    return [value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns,
            value.st_ctime_ns, value.st_nlink]


def recover_index_cached(state, roots, *, progress=lambda **value: None):
    binding = {'state': persistent_identity(state), 'roots': roots}
    try:
        cache = record(state, NAME)
        if (not isinstance(cache, dict) or set(cache) != {'schema','binding','entries','digest'}
                or cache['schema'] != SCHEMA or cache['binding'] != binding
                or cache['digest'] != digest({k:v for k,v in cache.items() if k != 'digest'})
                or not isinstance(cache['entries'], dict)):
            cache = None
        for name, row in (cache or {}).get('entries', {}).items():
            match = PATTERN.fullmatch(name)
            if (not match or not isinstance(row, dict) or set(row) != {'signature','unit_id','job_id'}
                    or row['job_id'] != match[1] or not isinstance(row['unit_id'], str)
                    or not row['unit_id'] or not isinstance(row['signature'], list)
                    or len(row['signature']) != 6 or any(type(v) is not int or v < 0 for v in row['signature'])):
                cache = None
                break
    except (OSError, ValueError, KeyError, TypeError):
        cache = None
    previous = (cache or {}).get('entries', {})
    index = record(state, 'archive-index.json', {})
    old_index = dict(index)
    names = os.listdir(state)
    if len(names) > 100_000:
        raise ArchiveError('状态目录过大，停止自动恢复')
    names = sorted(n for n in names if PATTERN.fullmatch(n))
    entries, reused, parsed = {}, 0, 0
    for i, name in enumerate(names):
        fd = open_file(state, name)
        try:
            before = sig(os.fstat(fd))
            old = previous.get(name)
            if old is not None and old['signature'] == before:
                uid, jid = old['unit_id'], old['job_id']
                reused += 1
            else:
                chunks, size = [], 0
                while chunk := os.read(fd, min(1024*1024, 64*1024*1024 + 1 - size)):
                    chunks.append(chunk); size += len(chunk)
                    if size > 64*1024*1024:
                        raise ArchiveError('状态记录过大')
                identity = decode(b''.join(chunks))['identity']
                jid, uid = digest(identity), identity['unit_id']
                parsed += 1
            if sig(os.fstat(fd)) != before or sig(os.stat(name, dir_fd=state, follow_symlinks=False)) != before:
                raise ArchiveError('核对期间归档记录发生变化')
        finally:
            os.close(fd)
        if name != 'job-' + jid + '.json' or (uid in index and index[uid] != jid):
            raise ArchiveError('已登记任务互相冲突或被修改')
        index[uid] = jid
        entries[name] = {'signature': before, 'unit_id': uid, 'job_id': jid}
        progress(checked_records=i+1, total_records=len(names), reused_records=reused, parsed_records=parsed)
    # A prefix of a changing directory must never become a complete cache.
    after_names = {n for n in os.listdir(state) if PATTERN.fullmatch(n)}
    if after_names != set(entries) or any(sig(os.stat(n, dir_fd=state, follow_symlinks=False)) != r['signature']
                                         for n,r in entries.items()):
        raise ArchiveError('核对期间归档记录范围发生变化')
    if index != old_index:
        atomic_json(state, 'archive-index.json', index)
    if entries != previous:
        data = {'schema': SCHEMA, 'binding': binding, 'entries': entries}
        try:
            atomic_json(state, NAME, {**data, 'digest': digest(data)})
        except OSError:
            pass  # Rebuild next time; cache write failure is not media failure.
    return index
