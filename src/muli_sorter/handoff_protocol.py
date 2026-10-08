"""Bounded, credential-free local file exchange; never opens media contents.

This file is vendored identically by both independent services.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import uuid

MAX_BYTES = 16 * 1024 * 1024
HEX = re.compile(r'[a-f0-9]{64}\Z')
BATCH = re.compile(r'BATCH_\d{8}_\d{6,}\Z')
SCHEMA = 'muli-archive-handoff/1'
LEASE_SECONDS = 1800


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def timestamp(value):
    d = datetime.fromisoformat(value)
    if d.tzinfo is None:
        raise ValueError('timezone_required')
    return d.timestamp()


def iso(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def relative(value):
    if not isinstance(value, str) or not value or '\\' in value or '\0' in value or PurePosixPath(value).is_absolute() or any(p in ('', '.', '..') for p in value.split('/')):
        raise ValueError('invalid_relative_path')
    return value.split('/')


@contextmanager
def directory(path):
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in relative(str(Path(path).absolute())[1:]):
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd); fd = child
        yield fd
    finally:
        os.close(fd)


def identity(path):
    with directory(path) as fd:
        fsid = getattr(os.fstatvfs(fd), 'f_fsid', 0)
        if not fsid:
            raise ValueError('missing_filesystem_identity')
        return [fsid & ((1 << 64) - 1), os.fstat(fd).st_ino]


def signature(info):
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]


def read_raw(root, name):
    parts = relative(name)
    with directory(root) as fd:
        parent = os.dup(fd)
        try:
            for part in parts[:-1]:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                os.close(parent); parent = child
            f = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            try:
                before = os.fstat(f)
                if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_BYTES or before.st_nlink != 1:
                    raise ValueError('invalid_record')
                chunks = []; count = 0
                while chunk := os.read(f, min(1024 * 1024, MAX_BYTES + 1 - count)):
                    count += len(chunk)
                    if count > MAX_BYTES:
                        raise ValueError('record_too_large')
                    chunks.append(chunk)
                if signature(before) != signature(os.fstat(f)):
                    raise ValueError('record_changed')
                return b''.join(chunks)
            finally:
                os.close(f)
        finally:
            os.close(parent)


def decode(raw):
    def unique(pairs):
        obj = {}
        for key, value in pairs:
            if key in obj:
                raise ValueError('duplicate_json_key')
            obj[key] = value
        return obj
    data = json.loads(raw, object_pairs_hook=unique, parse_constant=lambda _: (_ for _ in ()).throw(ValueError('nonfinite_json')))
    if not isinstance(data, dict):
        raise ValueError('object_required')
    return data


def read(root, name):
    return decode(read_raw(root, name))


def write(root, name, value):
    if len(relative(name)) != 1:
        raise ValueError('leaf_required')
    data = canonical(value)
    if len(data) > MAX_BYTES:
        raise ValueError('record_too_large')
    with directory(root) as parent:
        try:
            old = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if not stat.S_ISREG(old.st_mode) or old.st_nlink != 1:
                raise ValueError('refuse_record_replacement')
        except FileNotFoundError:
            pass
        tmp = '.handoff-' + uuid.uuid4().hex
        f = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
        try:
            with os.fdopen(f, 'wb') as stream:
                stream.write(data); stream.flush(); os.fsync(stream.fileno())
            os.replace(tmp, name, src_dir_fd=parent, dst_dir_fd=parent)
            os.fsync(parent)
        finally:
            try:
                os.unlink(tmp, dir_fd=parent)
            except FileNotFoundError:
                pass


def seal(event):
    value = {k: v for k, v in event.items() if k != 'event_id'}
    return {**value, 'event_id': digest(value)}


def validate_event(event, clock):
    if event.get('schema') != SCHEMA or not HEX.fullmatch(event.get('job_id', '')) or event != seal(event):
        raise ValueError('event_identity_mismatch')
    if type(event.get('sequence')) is not int or event['sequence'] < 1 or type(event.get('example_data')) is not bool:
        raise ValueError('invalid_event_identity')
    start, end = timestamp(event['checked_at']), timestamp(event['expires_at'])
    if start > clock + 5 or end <= clock or not 0 < end - start <= LEASE_SECONDS:
        raise ValueError('stale_event')
    if event.get('status') not in ('verified', 'needs_review'):
        raise ValueError('invalid_event_status')
    batches = event.get('batches')
    if not isinstance(batches, list) or not 0 < len(batches) <= 1024:
        raise ValueError('invalid_batches')
    if not HEX.fullmatch(event.get('request_digest','')):
        raise ValueError('invalid_request_binding')
    for key in ('source_identity','target_identity'):
        if not isinstance(event.get(key),list) or len(event[key])!=2 or any(type(x) is not int or x<=0 for x in event[key]):
            raise ValueError('invalid_root_identity')
    seen = set(); total = 0
    for item in batches:
        binding = item['binding']; bid = binding['batch_id']
        if not BATCH.fullmatch(bid) or bid in seen or type(binding['revision']) is not int or binding['revision'] < 1:
            raise ValueError('invalid_batch_binding')
        seen.add(bid)
        if binding['manifest_id'] != f"{binding['batch_uid']}-r{binding['revision']}" or not all(HEX.fullmatch(binding[k]) for k in ('manifest_blake3', 'completion_blake3')):
            raise ValueError('invalid_manifest_binding')
        rows = item['files']
        if not isinstance(rows,list) or event['status']=='needs_review' and rows:
            raise ValueError('invalid_coverage')
        paths = set(); ids = set(); total += len(rows)
        if total > 100000:
            raise ValueError('too_many_files')
        for row in rows:
            relative(row['relative_path']); relative(row['physical_source_path']); relative(row['target_path'])
            if row['file_id'] in ids or row['relative_path'] in paths:
                raise ValueError('duplicate_file')
            ids.add(row['file_id']); paths.add(row['relative_path'])
            if type(row['size_bytes']) is not int or row['size_bytes'] < 0 or not all(HEX.fullmatch(row[k]) for k in ('original_blake3', 'archived_blake3')) or row['mode'] not in ('copy', 'move'):
                raise ValueError('invalid_file_evidence')
            sig = row['target_signature']
            if not isinstance(sig, list) or len(sig) != 5 or any(type(x) is not int for x in sig) or sig[2] != row['size_bytes']:
                raise ValueError('invalid_target_signature')
    return event


def configuration(path):
    p=Path(path)
    cfg=read(p.parent,p.name)
    if cfg.get('schema')!='muli-handoff-channel/1' or cfg.get('include_history') is not False:
        raise ValueError('unsupported_handoff_configuration')
    timestamp(cfg['since'])
    paths=[Path(cfg[k]) for k in ('outbox','acknowledgements')]
    if any(not p.is_absolute() for p in paths) or paths[0]==paths[1] or any(a in b.parents for a,b in ((paths[0],paths[1]),(paths[1],paths[0]))):
        raise ValueError('overlapping_handoff_directories')
    if [identity(p) for p in paths]!=cfg['identities']:
        raise ValueError('handoff_channel_identity_changed')
    return cfg


def separate_channels(channels, protected):
    paths=[Path(p).absolute() for p in channels]
    other=[Path(p).absolute() for p in protected]
    for a in paths:
        for b in paths+other:
            if a==b and b in other or a!=b and (a in b.parents or b in a.parents):
                raise ValueError('handoff_overlaps_protected_path')
    if len(set(paths))!=len(paths):
        raise ValueError('handoff_directories_not_separate')
