"""Read-only admission checks for independent post-copy verification receipts."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat

from blake3 import blake3


ENV_NAME = 'MULI_POSTCOPY_RECEIPTS'
MAX_RECEIPT_BYTES = 64 * 1024 * 1024
HEX = re.compile(r'[0-9a-f]{64}\Z')
BATCH = re.compile(r'BATCH_\d{8}_\d{6,}\Z')
_CACHE_MISS = object()


class PostcopyError(ValueError):
    pass


def configured_root():
    value = os.environ.get(ENV_NAME)
    if value is None or value == '':
        return None
    path = Path(value)
    if not path.is_absolute():
        raise PostcopyError('独立校验回执目录必须是绝对路径')
    if '..' in path.parts:
        raise PostcopyError('独立校验回执目录必须是规范绝对路径')
    return path


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise PostcopyError('独立校验回执存在重复 JSON 字段')
        result[key] = value
    return result


def _decode(raw):
    try:
        value = json.loads(raw, object_pairs_hook=_pairs)
    except (TypeError, ValueError) as exc:
        raise PostcopyError('独立校验回执不是有效 JSON') from exc
    if not isinstance(value, dict):
        raise PostcopyError('独立校验回执顶层必须是对象')
    return value


def _file_signature(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _read_file(root, batch_id, *, required, cache=None, suffix='.json'):
    if not BATCH.fullmatch(batch_id):
        raise PostcopyError('非法批次目录名')
    path = configured_root()
    if path is None:
        if required:
            raise PostcopyError('未配置独立校验回执目录')
        return None
    try:
        # Walk every ancestor, not only the final directory component.
        root_fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        for part in path.parts[1:]:
            try:
                next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd)
            except BaseException:
                os.close(root_fd)
                raise
            os.close(root_fd)
            root_fd = next_fd
    except OSError as exc:
        raise PostcopyError('独立校验回执根目录不可安全读取') from exc
    try:
        root_info = os.fstat(root_fd)
        if not stat.S_ISDIR(root_info.st_mode):
            raise PostcopyError('独立校验回执根目录不是目录')
        root_identity = (root_info.st_dev, root_info.st_ino)
        try:
            fd = os.open(batch_id + suffix, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                         dir_fd=root_fd)
        except FileNotFoundError:
            if required:
                raise PostcopyError('缺少独立校验回执')
            return None
        except OSError as exc:
            raise PostcopyError('独立校验回执不可安全读取') from exc
        try:
            before_info = os.fstat(fd)
            if not stat.S_ISREG(before_info.st_mode) or before_info.st_size > MAX_RECEIPT_BYTES:
                raise PostcopyError('独立校验回执不是普通文件或超过大小限制')
            before = _file_signature(before_info)
            cache_key = ('postcopy-bytes', str(path), batch_id, suffix, root_identity, before)
            if cache is not None and cache_key in cache:
                # The caller owns this cache for one validation pass. Always
                # reopen the live path and compare inode/size/mtime/ctime/root.
                if _file_signature(os.fstat(fd)) != before:
                    raise PostcopyError('复用独立校验回执期间文件发生变化')
                return cache[cache_key]
            chunks, total = [], 0
            while chunk := os.read(fd, min(1024 * 1024, MAX_RECEIPT_BYTES + 1 - total)):
                chunks.append(chunk)
                total += len(chunk)
                if total > MAX_RECEIPT_BYTES:
                    raise PostcopyError('独立校验回执超过大小限制')
            after_info = os.fstat(fd)
            after = _file_signature(after_info)
            if before != after:
                raise PostcopyError('读取独立校验回执期间文件发生变化')
            result = b''.join(chunks), before, root_identity
            if cache is not None:
                cache[cache_key] = result
            return result
        finally:
            os.close(fd)
    finally:
        os.close(root_fd)


def _decode_cached(root, batch_id, suffix, result, cache):
    """Decode one live receipt at most once per caller-owned validation cache."""
    raw, file_signature, root_identity = result
    if cache is None:
        return _decode(raw)
    receipt_root = configured_root()
    key = ('postcopy-decoded', str(Path(root).absolute()), str(receipt_root),
           batch_id, suffix, tuple(root_identity), tuple(file_signature))
    value = cache.get(key, _CACHE_MISS)
    if value is not _CACHE_MISS:
        return value
    value = _decode(raw)
    cache[key] = value
    return value


def _renewal_binding_key(root, batch_id, original, renewal):
    original_sig = None if original is None else (original[1], original[2])
    renewal_sig = None if renewal is None else (renewal[1], renewal[2])
    return ('postcopy-renewal-binding', str(Path(root).absolute()),
            str(configured_root()), batch_id, '.json', '.v2.json',
            original_sig, renewal_sig)


def _read(root, batch_id, *, required, cache=None):
    original = _read_file(root, batch_id, required=required, cache=cache)
    renewal = _read_file(root, batch_id, required=False, cache=cache, suffix='.v2.json')
    if renewal is None:
        original_payload = (None if original is None else
                            _decode_cached(root, batch_id, '.json', original, cache))
        if original_payload is not None and 'previous_receipt_blake3' in original_payload:
            raise PostcopyError('续验回执不能替代原始回执')
        return original
    payload = _decode_cached(root, batch_id, '.v2.json', renewal, cache)
    original_payload = (None if original is None else
                        _decode_cached(root, batch_id, '.json', original, cache))
    binding_key = _renewal_binding_key(root, batch_id, original, renewal)
    bound = cache.get(binding_key, _CACHE_MISS) if cache is not None else _CACHE_MISS
    if bound is _CACHE_MISS:
        bound = (original is not None and
                 original_payload.get('schema') == 'postcopy-verification/1' and
                 payload.get('schema') == 'postcopy-verification/2' and
                 payload.get('previous_receipt_blake3') == blake3(original[0]).hexdigest())
        if cache is not None:
            cache[binding_key] = bound
    if not bound:
        raise PostcopyError('续验回执与保留的原始回执不一致')
    return renewal


def filesystem_id(fd):
    fsid = getattr(os.fstatvfs(fd), 'f_fsid', None)
    if type(fsid) is not int or (fsid & ((1 << 64) - 1)) == 0:
        raise PostcopyError('文件系统未提供稳定标识，不能复用独立校验')
    return fsid & ((1 << 64) - 1)


def source_signature(fd, schema):
    """v1 is boot-local; v2 binds the filesystem ID and inode across reboot."""
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise PostcopyError('来源必须是单链接普通文件')
    if schema == 'postcopy-verification/1':
        identity = {'dev': info.st_dev, 'ino': info.st_ino}
    elif schema == 'postcopy-verification/2':
        identity = {'fsid': filesystem_id(fd), 'ino': info.st_ino}
    else:
        raise PostcopyError('独立校验回执版本无效')
    return {**identity, 'size': info.st_size,
            'mtime_ns': info.st_mtime_ns, 'ctime_ns': info.st_ctime_ns}


def receipt_signature(root, batch_id):
    """Return the complete receipt-file signature, or None when unconfigured/missing."""
    result = _read(root, batch_id, required=False)
    return None if result is None else _identity(result)


def _identity(result):
    raw, file_signature, root_identity = result
    return [list(root_identity), list(file_signature), blake3(raw).hexdigest()]


def candidate_signature(root, batch_id):
    """Read a receipt signature only for a size-only manifest candidate."""
    return _candidate_signature(root, batch_id)


def _candidate_signature(root, batch_id, *, manifest=None, cache=None):
    """Archive-source fast path after live manifest signatures were checked.

    A supplied manifest must come from validate_record's per-pass cache. This
    returns evidence identity, never admission; validation still reads the
    receipt in full. The public candidate_signature always reads the manifest.
    """
    if configured_root() is None:
        return None
    if manifest is None:
        from .intake import read_bytes, decode
        try:
            raw = read_bytes(root, batch_id + '/ingest_manifest.json')
        except Exception as exc:
            raise PostcopyError('无法读取批次清单') from exc
        try:
            manifest = decode(raw)
        except Exception as exc:
            raise PostcopyError(str(exc)) from exc
    elif manifest.get('batch', {}).get('batch_id') != batch_id:
        raise PostcopyError('独立校验回执候选批次不匹配')
    if manifest.get('batch', {}).get('result') != 'COPY_SIZE_VERIFIED':
        return None
    result = _read(root, batch_id, required=False, cache=cache)
    if result is None:
        return None
    key = ('postcopy-identity', result[1], result[2])
    if cache is not None:
        if key not in cache:
            cache[key] = _identity(result)
        return cache[key]
    return _identity(result)


def _path(value):
    if not isinstance(value, str) or not value or '\\' in value or '\0' in value:
        raise PostcopyError('独立校验回执路径无效')
    p = PurePosixPath(value)
    if p.is_absolute() or any(part in ('', '.', '..') for part in value.split('/')):
        raise PostcopyError('独立校验回执路径越界或非规范')
    return p


def _hex(value, label):
    if not isinstance(value, str) or not HEX.fullmatch(value):
        raise PostcopyError(label + '不是有效 BLAKE3 摘要')


def _utc(value):
    if not isinstance(value, str):
        raise PostcopyError('独立校验回执时间无效')
    try:
        timestamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError as exc:
        raise PostcopyError('独立校验回执时间无效') from exc
    if timestamp.tzinfo is None or timestamp.utcoffset() != timezone.utc.utcoffset(timestamp):
        raise PostcopyError('独立校验回执时间必须是 UTC')


def _signature(value, expected_size, schema):
    identity = 'fsid' if schema == 'postcopy-verification/2' else 'dev'
    if not isinstance(value, dict) or set(value) != {identity, 'ino', 'size', 'mtime_ns', 'ctime_ns'}:
        raise PostcopyError('独立校验回执来源签名字段不完整')
    if any(type(value[key]) is not int or value[key] < 0 for key in value):
        raise PostcopyError('独立校验回执来源签名无效')
    if value['size'] != expected_size:
        raise PostcopyError('独立校验回执来源签名大小不一致')
    if identity == 'fsid' and (value['fsid'] == 0 or value['fsid'] >= 2 ** 64):
        raise PostcopyError('独立校验回执文件系统身份无效')
    return value


def validate(root, batch_id, manifest, manifest_raw, ingest_raw):
    """Validate and return private evidence for a size-only completed batch."""
    result = _read(root, batch_id, required=True)
    return _validate_result(root, batch_id, manifest, manifest_raw, ingest_raw, result)


def validate_payload(root, batch_id, manifest, manifest_raw, ingest_raw, payload):
    """Writer preflight only; this never grants admission to an unpublished file."""
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
    _validate_result(root, batch_id, manifest, manifest_raw, ingest_raw,
                     (raw, (0, 0, len(raw), 0, 0), (0, 0)))


def _validate_result(root, batch_id, manifest, manifest_raw, ingest_raw, result):
    raw, file_sig, root_identity = result
    receipt = _decode(raw)
    required = {'schema', 'status', 'verified_at', 'batch_id', 'batch_uid', 'revision',
                'manifest_id', 'source_id', 'original_manifest_blake3',
                'original_ingest_complete_blake3', 'files'}
    if receipt.get('schema') == 'postcopy-verification/2' and 'previous_receipt_blake3' in receipt:
        required.add('previous_receipt_blake3')
        _hex(receipt['previous_receipt_blake3'], 'previous_receipt')
    if set(receipt) != required:
        raise PostcopyError('独立校验回执字段不完整或包含未知字段')
    if receipt['schema'] not in ('postcopy-verification/1', 'postcopy-verification/2') or receipt['status'] != 'completed':
        raise PostcopyError('独立校验回执状态或版本无效')
    _utc(receipt['verified_at'])
    if (receipt['batch_id'] != batch_id or receipt['batch_uid'] != manifest['batch']['batch_uid'] or
            receipt['revision'] != manifest['revision'] or receipt['manifest_id'] != manifest['manifest_id'] or
            receipt['source_id'] != manifest['batch'].get('source_id')):
        raise PostcopyError('独立校验回执批次身份不一致')
    _hex(receipt['original_manifest_blake3'], 'manifest')
    _hex(receipt['original_ingest_complete_blake3'], 'ingest_complete')
    if receipt['original_manifest_blake3'] != blake3(manifest_raw).hexdigest() or \
            receipt['original_ingest_complete_blake3'] != blake3(ingest_raw).hexdigest():
        raise PostcopyError('独立校验回执与原始清单摘要不一致')
    # A correction receipt would describe a different source identity and is
    # never an input to the independent post-copy admission path.
    correction = _read_optional_file(root, batch_id, 'time_correction_receipt.json')
    if correction:
        raise PostcopyError('存在时间校正回执，不能使用 size-only 独立准入')
    files = manifest.get('files')
    summary = manifest.get('summary')
    if not isinstance(files, list) or not isinstance(summary, dict) or len(files) > 100_000:
        raise PostcopyError('原始清单文件范围无效')
    if (summary.get('selected_file_count') != len(files) or
            summary.get('size_verified_file_count') != len(files) or
            summary.get('verified_file_count') != 0 or
            summary.get('copied_file_count') != len(files) or
            summary.get('awaiting_hash_file_count') != 0 or
            summary.get('pending_file_count') != 0 or summary.get('failed_file_count') != 0 or
            any(not isinstance(f, dict) for f in files) or
            summary.get('selected_bytes') != sum(f.get('size_bytes', -1) for f in files) or
            summary.get('copied_bytes') != summary.get('selected_bytes') or
            summary.get('previously_ingested_count') != 0):
        raise PostcopyError('size-only 清单汇总未闭合')
    expected = {}
    expected_ids = set()
    total = 0
    for item in files:
        if not isinstance(item, dict) or item.get('copy_status') != 'size_verified':
            raise PostcopyError('size-only 清单包含非直接 size_verified 文件')
        path = item.get('relative_path')
        _path(path)
        h = item.get('hash')
        if (not item.get('file_id') or not isinstance(item.get('size_bytes'), int) or
                item['size_bytes'] < 0 or not isinstance(h, dict) or
                h.get('algorithm') != 'blake3' or h.get('destination') is not None or
                h.get('existing_destination') is not None or item.get('hash_match') is not None or
                item.get('existing_copy') is not None or item.get('error') is not None or
                item.get('destination_relative_path') != 'SOURCE_DATA/' + path):
            raise PostcopyError('size-only 原始文件摘要不完整')
        _hex(h.get('source'), 'source')
        if path in expected or item['file_id'] in expected_ids:
            raise PostcopyError('原始文件范围存在重复')
        total += item['size_bytes']
        expected[path] = item
        expected_ids.add(item['file_id'])
    if not isinstance(receipt['files'], list) or len(receipt['files']) != len(expected):
        raise PostcopyError('独立校验回执文件范围未闭合')
    seen_ids, seen_paths, receipt_files = set(), set(), {}
    for item in receipt['files']:
        if not isinstance(item, dict) or set(item) != {'file_id', 'relative_path', 'resolved_path', 'size_bytes', 'blake3', 'source_signature'}:
            raise PostcopyError('独立校验回执文件字段不完整')
        path = item['relative_path']
        _path(path)
        resolved = _path(item['resolved_path'])
        expected_resolved = PurePosixPath(batch_id) / 'SOURCE_DATA' / path
        if resolved != expected_resolved or path in seen_paths or item['file_id'] in seen_ids:
            raise PostcopyError('独立校验回执文件路径或标识重复')
        source = expected.get(path)
        if (source is None or item['file_id'] != source['file_id'] or
                item['size_bytes'] != source['size_bytes'] or item['blake3'] != source['hash']['source']):
            raise PostcopyError('独立校验回执文件与原始清单不一致')
        _hex(item['blake3'], 'file')
        _signature(item['source_signature'], item['size_bytes'], receipt['schema'])
        seen_paths.add(path)
        seen_ids.add(item['file_id'])
        receipt_files[str(resolved)] = item
    if seen_paths != set(expected):
        raise PostcopyError('独立校验回执遗漏原始文件')
    evidence = {'schema': receipt['schema'], 'status': receipt['status'],
                'verified_at': receipt['verified_at'], 'batch_id': batch_id,
                'batch_uid': receipt['batch_uid'], 'revision': receipt['revision'],
                'manifest_id': receipt['manifest_id'], 'source_id': receipt['source_id'],
                'original_manifest_blake3': receipt['original_manifest_blake3'],
                'original_ingest_complete_blake3': receipt['original_ingest_complete_blake3'],
                'receipt_signature': _identity(result),
                'receipt_blake3': blake3(raw).hexdigest(),
                'receipt_root': {'path': str(configured_root()), 'dev': root_identity[0],
                                 'ino': root_identity[1]},
                'files': receipt_files}
    return evidence


def _read_optional_file(root, batch_id, name):
    from .intake import _open
    try:
        fd = _open(root, batch_id + '/' + name)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise PostcopyError('时间校正回执不可安全读取') from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise PostcopyError('时间校正回执不是普通文件')
        return True
    finally:
        os.close(fd)
