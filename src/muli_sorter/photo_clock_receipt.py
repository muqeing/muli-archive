"""Bind completed ARW date edits to immutable ingest and post-copy proofs.

This reader never modifies media or the original verification receipt. A
completed correction provides a separate proof of the new bytes and identity.
"""
from copy import deepcopy
from datetime import datetime, timedelta
import json
import os
import stat

from blake3 import blake3

from .time_correction_receipt import _open, _unique, BATCH, HEX, ID, ReceiptError
from .postcopy_receipt import _signature, PostcopyError

NAME = 'photo_time_correction.json'
SCHEMA = 'photo-clock-correction/1'
MAX_BYTES = 16 * 1024 * 1024
TAGS = {'IFD0:ModifyDate', 'ExifIFD:DateTimeOriginal', 'ExifIFD:CreateDate',
        'IFD1:ModifyDate'}
REQUIRED_TAGS = TAGS - {'IFD1:ModifyDate'}


def signature(root, batch_id):
    if not isinstance(batch_id, str) or not BATCH.fullmatch(batch_id):
        raise ReceiptError('非法照片校正批次')
    try:
        fd = _open(root, batch_id + '/' + NAME)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ReceiptError('照片校正回执路径不安全') from exc
    try:
        s = os.fstat(fd)
        if not stat.S_ISREG(s.st_mode) or s.st_size > MAX_BYTES:
            raise ReceiptError('照片校正回执不是普通文件或超过限制')
        return (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
    finally:
        os.close(fd)


def read(root, batch_id):
    expected = signature(root, batch_id)
    if expected is None:
        return None
    fd = _open(root, batch_id + '/' + NAME)
    try:
        before = os.fstat(fd)
        before = (before.st_dev, before.st_ino, before.st_size,
                  before.st_mtime_ns, before.st_ctime_ns)
        if before != expected:
            raise ReceiptError('照片校正回执读取前发生变化')
        chunks, total = [], 0
        while chunk := os.read(fd, min(1024 * 1024, MAX_BYTES + 1 - total)):
            chunks.append(chunk)
            total += len(chunk)
            if total > MAX_BYTES:
                raise ReceiptError('照片校正回执过大')
        s = os.fstat(fd)
        if before != (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns):
            raise ReceiptError('照片校正回执读取期间发生变化')
    finally:
        os.close(fd)
    try:
        raw = b''.join(chunks)
        value = json.loads(raw, object_pairs_hook=_unique)
    except (ValueError, UnicodeError) as exc:
        raise ReceiptError('照片校正回执 JSON 无效') from exc
    return value, blake3(raw).hexdigest()


def _time(value, *, local=False):
    try:
        dt = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ReceiptError('照片校正时间无效') from exc
    if dt.tzinfo is None or (local and dt.utcoffset() != timedelta(hours=8)):
        raise ReceiptError('照片校正时间缺少明确时区')
    if local and (value != dt.isoformat() or dt.microsecond):
        raise ReceiptError('照片校正时间必须是北京时间整秒')
    return dt


def _hash(value):
    if not isinstance(value, str) or not HEX.fullmatch(value):
        raise ReceiptError('照片校正摘要无效')


def validate_data(manifest, raw, postcopy, receipt):
    fields = {'schema', 'status', 'operator_confirmed', 'batch_id', 'batch_uid',
              'revision', 'manifest_id', 'original_manifest_blake3',
              'original_postcopy_blake3', 'correction_id', 'offset_seconds',
              'completed_at', 'files'}
    if not isinstance(receipt, dict) or set(receipt) != fields or receipt['schema'] != SCHEMA:
        raise ReceiptError('照片校正回执字段不符')
    if receipt['status'] != 'completed' or receipt['operator_confirmed'] is not True:
        raise ReceiptError('照片校正未完成或未确认，禁止消费')
    if manifest.get('_amendment'):
        raise ReceiptError('照片校正与视频校正回执冲突')
    batch = manifest['batch']
    for key in ('batch_id', 'batch_uid'):
        if receipt[key] != batch[key]:
            raise ReceiptError('照片校正批次身份不符')
    if (type(receipt['revision']) is not int or receipt['revision'] != manifest['revision']
            or receipt['manifest_id'] != manifest['manifest_id']):
        raise ReceiptError('照片校正清单身份不符')
    _hash(receipt['original_manifest_blake3'])
    if receipt['original_manifest_blake3'] != blake3(raw).hexdigest():
        raise ReceiptError('照片校正引用的原始清单改变')
    if (batch.get('result') != 'COPY_SIZE_VERIFIED' or postcopy is None
            or receipt['original_postcopy_blake3'] != postcopy['receipt_blake3']):
        raise ReceiptError('照片校正缺少原始独立内容校验证据')
    _hash(receipt['original_postcopy_blake3'])
    if not isinstance(receipt['correction_id'], str) or not ID.fullmatch(receipt['correction_id']):
        raise ReceiptError('照片校正标识无效')
    offset = receipt['offset_seconds']
    if type(offset) is not int or not 0 < abs(offset) <= 7 * 86400:
        raise ReceiptError('照片校正偏移无效')
    _time(receipt['completed_at'])
    rows = receipt['files']
    if not isinstance(rows, list) or not 1 <= len(rows) <= 20000:
        raise ReceiptError('照片校正范围为空或过大')
    by_id = {f['file_id']: f for f in manifest['files']}
    result, seen_paths = {}, set()
    expected = {'file_id', 'relative_path', 'size_bytes', 'original_blake3',
                'corrected_blake3', 'original_capture_time', 'corrected_capture_time',
                'original_source_signature', 'corrected_source_signature',
                'patches', 'readback_verified_at'}
    for row in rows:
        if not isinstance(row, dict) or set(row) != expected:
            raise ReceiptError('照片校正文件字段不符')
        fid, path = row['file_id'], row['relative_path']
        f = by_id.get(fid) if isinstance(fid, str) else None
        if (f is None or fid in result or not isinstance(path, str) or path in seen_paths
                or not path.lower().endswith('.arw') or f['relative_path'] != path
                or f['copy_status'] != 'size_verified' or f.get('existing_copy')
                or f.get('destination_relative_path') != 'SOURCE_DATA/' + path
                or type(row['size_bytes']) is not int or row['size_bytes'] != f['size_bytes']):
            raise ReceiptError('照片校正不是本批次自有 ARW 或范围不符')
        proof = postcopy['files'].get(batch['batch_id'] + '/SOURCE_DATA/' + path)
        _hash(row['original_blake3']); _hash(row['corrected_blake3'])
        if (proof is None or row['original_blake3'] != f['hash']['source']
                or row['original_blake3'] != proof['blake3']
                or row['original_blake3'] == row['corrected_blake3']
                or row['original_source_signature'] != proof['source_signature']):
            raise ReceiptError('照片校正原始内容或身份校验不符')
        old_sig, new_sig = row['original_source_signature'], row['corrected_source_signature']
        try:
            _signature(new_sig, row['size_bytes'], postcopy['schema'])
        except PostcopyError as exc:
            raise ReceiptError(str(exc)) from exc
        identity = 'fsid' if postcopy['schema'] == 'postcopy-verification/2' else 'dev'
        if (any(old_sig[k] != new_sig[k] for k in (identity, 'ino', 'size'))
                or old_sig == new_sig):
            raise ReceiptError('照片校正改变了文件身份或缺少新签名')
        info = (f.get('metadata') or {}).get('capture_time') or {}
        if row['original_capture_time'] != info.get('normalized'):
            raise ReceiptError('照片校正原始拍摄时间不符')
        old, new = _time(row['original_capture_time'], local=True), _time(row['corrected_capture_time'], local=True)
        if new != old + timedelta(seconds=offset):
            raise ReceiptError('照片校正不是已确认的固定偏移')
        _time(row['readback_verified_at'])
        patches = row['patches']
        if not isinstance(patches, list) or not 3 <= len(patches) <= 4:
            raise ReceiptError('照片校正时间字段不完整')
        tags, ranges = set(), []
        before = (old.strftime('%Y:%m:%d %H:%M:%S') + '\0').encode().hex()
        after = (new.strftime('%Y:%m:%d %H:%M:%S') + '\0').encode().hex()
        for p in patches:
            if (not isinstance(p, dict) or set(p) != {'tag', 'offset', 'before_hex', 'after_hex'}
                    or p['tag'] not in TAGS or p['tag'] in tags
                    or type(p['offset']) is not int or p['offset'] < 8
                    or p['offset'] + 20 > row['size_bytes']
                    or p['before_hex'] != before or p['after_hex'] != after):
                raise ReceiptError('照片校正补丁不是允许的等长 EXIF 时间字段')
            tags.add(p['tag']); ranges.append((p['offset'], p['offset'] + 20))
        ranges.sort()
        if not REQUIRED_TAGS <= tags or any(a[1] > b[0] for a,b in zip(ranges, ranges[1:])):
            raise ReceiptError('照片校正字段缺失或重叠')
        result[fid] = deepcopy(row)
        seen_paths.add(path)
    return {'correction_id': receipt['correction_id'], 'status': 'completed',
            'original_manifest_blake3': receipt['original_manifest_blake3'],
            'original_postcopy_blake3': receipt['original_postcopy_blake3'], 'files': result}


def validate(root, batch_id, manifest, raw):
    found = read(root, batch_id)
    if found is None:
        return None
    receipt, receipt_hash = found
    result = validate_data(manifest, raw, manifest.get('_postcopy_evidence'), receipt)
    result['receipt_blake3'] = receipt_hash
    return result


def amended_postcopy(postcopy, correction):
    value = deepcopy(postcopy)
    for row in correction['files'].values():
        path = value['batch_id'] + '/SOURCE_DATA/' + row['relative_path']
        value['files'][path]['blake3'] = row['corrected_blake3']
        value['files'][path]['source_signature'] = deepcopy(row['corrected_source_signature'])
    return value
