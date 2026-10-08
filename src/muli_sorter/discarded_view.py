"""Read-only projection of independently verified recoverable discards."""
import json
import os
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
import re
import stat

from blake3 import blake3

from .archive_io import ArchiveError, directory, open_file, persistent_identity, signature, subdirectory
from .intake import _open, media_stat, relative


ENV_NAME = 'MULI_DISCARDED_INDEX'
SCHEMA = 'discarded-projection/1'
RECEIPT_SCHEMAS = {'recoverable-discard/1', 'user-discard/1'}
MAX_BYTES = 64 * 1024 * 1024


class DiscardedViewError(ValueError):
    pass


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise DiscardedViewError('JSON 存在重复字段')
        result[key] = value
    return result


def _decode(raw):
    try:
        value = json.loads(raw, object_pairs_hook=_pairs)
    except (TypeError, ValueError) as exc:
        raise DiscardedViewError('弃用投影不是有效 JSON') from exc
    if not isinstance(value, dict):
        raise DiscardedViewError('弃用投影顶层必须是对象')
    return value


def _read_fd(fd):
    before = signature(fd)
    if not stat.S_ISREG(os.fstat(fd).st_mode) or before[2] > MAX_BYTES:
        raise DiscardedViewError('弃用记录不是普通文件或超过大小限制')
    raw = b''
    while chunk := os.read(fd, MAX_BYTES + 1 - len(raw)):
        raw += chunk
        if len(raw) > MAX_BYTES:
            raise DiscardedViewError('弃用记录超过大小限制')
    if signature(fd) != before:
        raise DiscardedViewError('弃用记录读取期间发生变化')
    return raw


def _read_path(root, path):
    parts = _relative(path)
    directory_fd = os.dup(root)
    try:
        for part in parts[:-1]:
            nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = nxt
        fd = open_file(directory_fd, parts[-1])
        try:
            before = signature(fd)
            raw = _read_fd(fd)
            if signature(fd) != before:
                raise DiscardedViewError('弃用记录读取期间发生变化')
            return raw
        finally:
            os.close(fd)
    finally:
        os.close(directory_fd)


def _relative(value):
    if not isinstance(value, str) or not value or '\\' in value or '\0' in value:
        raise DiscardedViewError('弃用路径无效')
    p = PurePosixPath(value)
    if p.is_absolute() or any(part in ('', '.', '..') for part in value.split('/')):
        raise DiscardedViewError('弃用路径越界或非规范')
    return p.parts


def _identity(value, name):
    if (not isinstance(value, list) or len(value) != 2 or
            any(type(item) is not int or item < 0 for item in value)):
        raise DiscardedViewError(name + '身份无效')
    return value


def _signature(value):
    if (not isinstance(value, list) or len(value) != 5 or
            any(type(item) is not int or item < 0 for item in value)):
        raise DiscardedViewError('弃用目标签名无效')
    return value


def _file_key(row, *, target=True):
    required = {'source_path', 'name', 'size_bytes', 'blake3'}
    if target:
        required |= {'target_path', 'target_signature'}
    if not isinstance(row, dict) or not required <= set(row):
        raise DiscardedViewError('弃用文件证据不完整')
    source_path = '/'.join(_relative(row['source_path']))
    name = '/'.join(_relative(row['name']))
    target_path = '/'.join(_relative(row['target_path'])) if target else None
    if type(row['size_bytes']) is not int or row['size_bytes'] < 0:
        raise DiscardedViewError('弃用文件大小无效')
    if (not isinstance(row['blake3'], str) or len(row['blake3']) != 64 or
            not re.fullmatch(r'[0-9a-f]{64}', row['blake3'])):
        raise DiscardedViewError('弃用文件摘要无效')
    if target:
        _signature(row['target_signature'])
    return (source_path, name, row['size_bytes'], row['blake3'], target_path,
            tuple(row['target_signature']) if target else None)


def _receipt_target_path(receipt, row):
    if 'target_path' in row:
        return '/'.join(_relative(row['target_path']))
    destination = receipt.get('destination')
    if not isinstance(destination, str):
        raise DiscardedViewError('原弃用回执缺少目标目录')
    return '/'.join(_relative(destination + '/' + row['source_path']))


def _receipt_files(receipt):
    if receipt.get('schema') not in RECEIPT_SCHEMAS or receipt.get('status') != 'completed':
        raise DiscardedViewError('原弃用回执 schema 或状态无效')
    files = receipt.get('files')
    if not isinstance(files, list) or not files:
        raise DiscardedViewError('原弃用回执缺少文件证据')
    result = {}
    for row in files:
        key = _file_key({**row, 'target_path': _receipt_target_path(receipt, row)})
        if key[0] in result:
            raise DiscardedViewError('原弃用回执存在重复来源')
        result[key[0]] = (key, row)
    return result


def _read_receipt(archive_state, relative_path):
    parts = _relative(relative_path)
    if len(parts) != 3 or parts[0] != 'discarded-media' or parts[2] != 'discard-receipt.json':
        raise DiscardedViewError('原弃用回执路径必须固定在 discarded-media 下')
    with directory(archive_state) as root:
        raw = _read_path(root, relative_path)
    return raw, _decode(raw)


def _load_projection(archive_state, path):
    with directory(archive_state) as root:
        raw = _read_path(root, path)
    projection = _decode(raw)
    required = {'schema', 'status', 'checked_at', 'source_root_identity',
                'target_root_identity', 'original_receipt_blake3',
                'original_receipt_path', 'units', 'scope_files'}
    if set(projection) != required or projection['schema'] != SCHEMA or projection['status'] != 'completed':
        raise DiscardedViewError('弃用投影 schema 或字段无效')
    if not isinstance(projection['checked_at'], str):
        raise DiscardedViewError('弃用投影时间必须是 UTC')
    try:
        checked_at = datetime.fromisoformat(projection['checked_at'].replace('Z', '+00:00'))
    except ValueError as exc:
        raise DiscardedViewError('弃用投影时间必须是 UTC') from exc
    if checked_at.tzinfo is None or checked_at.utcoffset() != timezone.utc.utcoffset(checked_at):
        raise DiscardedViewError('弃用投影时间必须是 UTC')
    _identity(projection['source_root_identity'], '来源根')
    _identity(projection['target_root_identity'], '目标根')
    if (not isinstance(projection['original_receipt_blake3'], str) or
            len(projection['original_receipt_blake3']) != 64):
        raise DiscardedViewError('原弃用回执摘要无效')
    return projection


def _safe_target(archive_state, path, expected):
    with directory(archive_state) as root:
        parts = _relative(path)
        with subdirectory(root, '/'.join(parts[:-1])) as parent:
            fd = open_file(parent, parts[-1])
            try:
                if os.fstat(fd).st_nlink != 1 or signature(fd) != list(expected):
                    raise DiscardedViewError('弃用目标身份或内容发生变化')
            finally:
                os.close(fd)


def _unit_files(units):
    result = {}
    for unit in units:
        if not isinstance(unit, dict) or not isinstance(unit.get('unit_id'), str):
            raise DiscardedViewError('弃用单元证据不完整')
        files = unit.get('files')
        if not isinstance(files, list) or unit['unit_id'] in result:
            raise DiscardedViewError('弃用单元重复或缺少文件')
        rows = {}
        for row in files:
            key = _file_key(row)
            if key[0] in rows:
                raise DiscardedViewError('弃用单元存在重复来源')
            rows[key[0]] = key
        result[unit['unit_id']] = rows
    return result


def _validate_one(staging, archive_state, projection, receipt_raw, receipt, model):
    if blake3(receipt_raw).hexdigest() != projection['original_receipt_blake3']:
        raise DiscardedViewError('原弃用回执摘要不一致')
    receipt_files = _receipt_files(receipt)
    scope = projection.get('scope_files')
    scope_map = {}
    for row in scope if isinstance(scope, list) else ():
        key = _file_key(row)
        if key[0] in scope_map:
            raise DiscardedViewError('弃用保护范围存在重复来源')
        scope_map[key[0]] = key
    if set(scope_map) != set(receipt_files):
        raise DiscardedViewError('弃用保护范围与原回执不闭合')
    for path, (key, _) in receipt_files.items():
        if key != scope_map[path]:
            raise DiscardedViewError('弃用保护范围与原回执证据不一致')
        _safe_target(archive_state, key[4], key[5])
    expected_units = {}
    for unit in model.get('units', ()):
        expected = {}
        for file in unit.get('files', ()):
            source_path = '/'.join(_relative(file['source_path']))
            key = scope_map.get(source_path)
            expected[source_path] = None if key is None else _file_key(
                {**file, 'target_path': key[4], 'target_signature': list(key[5])})
        expected_units[unit['unit_id']] = expected
    projection_units = _unit_files(projection['units'])
    accepted = {}
    for uid, files in projection_units.items():
        if uid not in expected_units or files != expected_units[uid]:
            raise DiscardedViewError('弃用单元与当前确认模型不一致')
        accepted[uid] = files
    if not accepted:
        raise DiscardedViewError('弃用投影没有可匹配单元')
    result = {}
    for uid, files in accepted.items():
        missing = True
        for source_path, key in files.items():
            try:
                media_stat(staging, source_path, key[2])
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise DiscardedViewError('来源文件访问状态异常') from exc
            missing = False
            break
        if missing:
            result[uid] = True
    return result


def project(model, staging, *, skip_source_ids=()):
    """Overlay only independently validated discarded units onto material state."""
    from .material_triage import build_material_state
    state = build_material_state(model, staging, skip_source_ids=skip_source_ids)
    warnings = list(state.get('warnings', []))
    value = os.environ.get(ENV_NAME)
    if not value:
        state['warnings'] = warnings
        return state
    index = Path(value)
    try:
        if not index.is_absolute() or index.is_symlink():
            raise DiscardedViewError('弃用索引目录路径无效')
        archive_state = index.parent
        with directory(index) as index_fd:
            names = sorted(os.listdir(index_fd))
        records = {}
        with directory(staging) as source_root, directory(archive_state) as target_root:
            source_identity = persistent_identity(source_root)
            target_identity = persistent_identity(target_root)
            for name in names:
                if not name.endswith('.json'):
                    continue
                path = name
                try:
                    projection = _load_projection(archive_state, path if index == archive_state else 'discarded-index/' + path)
                    if projection['source_root_identity'] != source_identity or projection['target_root_identity'] != target_identity:
                        raise DiscardedViewError('弃用根身份发生变化')
                    receipt_rel = projection['original_receipt_path']
                    receipt_raw, receipt = _read_receipt(archive_state, receipt_rel)
                    accepted = _validate_one(staging, archive_state, projection, receipt_raw, receipt, model)
                    for uid in accepted:
                        if uid in records:
                            raise DiscardedViewError('弃用投影重复覆盖同一素材单元')
                        records[uid] = True
                except (DiscardedViewError, ArchiveError, OSError, KeyError, TypeError, ValueError) as exc:
                    warnings.append({'path': str(index / path), 'reason': str(exc)})
    except (DiscardedViewError, ArchiveError, OSError, ValueError) as exc:
        warnings.append({'path': str(index), 'reason': str(exc)})
        state['warnings'] = warnings
        return state
    for uid in records:
        row = state['units'].get(uid)
        if row is not None and row.get('reason_code') == 'source_missing':
            state['units'][uid] = {'category': 'discarded', 'reason_code': 'recoverable_discard',
                                    'reason': '来源已按独立弃用回执移入可恢复弃用区',
                                    'action': '可从弃用区恢复；不计入归档历史。'}
    state['warnings'] = warnings
    return state
