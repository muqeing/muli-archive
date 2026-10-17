"""Bounded discovery hints, never archive or media-write authorization.

Only small validated candidates and signal digests survive a discovery pass.
Every reuse reopens all relevant control paths without following links. Missing
optional files are part of the key, so publication/removal invalidates a hint.
"""
from collections import OrderedDict
from copy import deepcopy
import os
from pathlib import Path
import stat

from .intake import BATCH, EvidenceError, MAX_RECORD_BYTES
from .order_feed_io import directory, digest
from .postcopy_receipt import configured_root


CONTROL_FILES = ('ingest_complete.json', 'ingest_manifest.json',
                 'ingest_manifest.md', 'time_correction_receipt.json',
                 'photo_time_correction.json')


def _identity(fd):
    info = os.fstat(fd)
    return info.st_dev, info.st_ino


def _file(fd, name):
    try:
        child = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                        dir_fd=fd)
    except FileNotFoundError:
        return None
    try:
        info = os.fstat(child)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_RECORD_BYTES:
            raise EvidenceError('分类证据类型或大小不符')
        return (info.st_dev, info.st_ino, info.st_size,
                info.st_mtime_ns, info.st_ctime_ns, info.st_nlink)
    finally:
        os.close(child)


class DiscoveryEvidenceCache:
    def __init__(self, staging, *, allow_examples=False, maximum_entries=512):
        if type(maximum_entries) is not int or not 1 <= maximum_entries <= 1024:
            raise ValueError('invalid discovery cache bound')
        self.staging = Path(staging).absolute()
        self.examples = allow_examples
        self.maximum_entries = maximum_entries
        self.entries = OrderedDict()

    def fingerprint(self, row):
        bid = row['batch_id']
        if not isinstance(bid, str) or not BATCH.fullmatch(bid):
            raise EvidenceError('非法批次目录名')
        with directory(self.staging) as root:
            root_id = _identity(root)
        with directory(self.staging / bid) as batch:
            controls = (_identity(batch), tuple(_file(batch, n) for n in CONTROL_FILES))
        receipt_root = configured_root()
        receipts = None
        if receipt_root is not None:
            with directory(receipt_root) as receipt:
                receipts = (str(receipt_root), _identity(receipt),
                            _file(receipt, bid + '.json'),
                            _file(receipt, bid + '.v2.json'))
        # Lease renewals are workflow observations, not new media evidence.
        values = {k: v for k, v in row.items() if k not in ('archive_handoff', 'archive_completion')}
        return digest([str(self.staging), root_id, controls, receipts,
                       values, self.examples])

    def _get(self, bid, key):
        entry = self.entries.get(bid)
        if entry is not None and entry['key'] == key:
            self.entries.move_to_end(bid)
            return entry
        self.entries.pop(bid, None)
        return None

    def _put(self, bid, key, candidate, part=None):
        self.entries.pop(bid, None)
        self.entries[bid] = {'key': key, 'candidate': deepcopy(candidate), 'part': part}
        while len(self.entries) > self.maximum_entries:
            self.entries.popitem(last=False)

    def candidate(self, row):
        from .queue_evidence import size_verified_candidate
        if (not isinstance(row, dict) or row.get('state') != 'COMPLETED'
                or row.get('result') != 'COPY_SIZE_VERIFIED'):
            return None
        bid = row['batch_id']
        try:
            before = self.fingerprint(row)
            entry = self._get(bid, before)
            if entry is not None:
                if self.fingerprint(row) != before:
                    raise EvidenceError('复用分类证据期间文件发生变化')
                return deepcopy(entry['candidate'])
            evidence = size_verified_candidate(self.staging, row, allow_examples=self.examples)
            if evidence is None:
                return None  # Unknown/failed proof is never cached as success.
            candidate = {'receipt_signature': deepcopy(evidence['receipt_signature'])}
            if self.fingerprint(row) != before:
                raise EvidenceError('分类核对期间完成证据发生变化')
            self._put(bid, before, candidate)
            return candidate
        except (EvidenceError, OSError, KeyError, TypeError, ValueError):
            self.entries.pop(bid, None)
            return None

    def parts(self, batch_ids, snapshot):
        from .queue_evidence import _signal_part, completed
        states = {s['batch_id']: s for s in snapshot['batches']}
        parts = {}
        for bid in sorted(set(batch_ids)):
            row = states.get(bid)
            if row is None:
                raise EvidenceError('当前批次或被引用批次尚未最终完成')
            before = self.fingerprint(row)
            entry = self._get(bid, before)
            if entry is not None and entry['part'] is not None:
                if self.fingerprint(row) != before:
                    self.entries.pop(bid, None)
                    raise EvidenceError('复用分类证据期间文件发生变化')
                parts[bid] = entry['part']
                continue
            candidate = self.candidate(row)
            if not completed(row) and candidate is None:
                raise EvidenceError('当前批次或被引用批次尚未最终完成')
            value = _signal_part(self.staging, bid, row, candidate)
            if self.fingerprint(row) != before:
                self.entries.pop(bid, None)
                raise EvidenceError('分类核对期间完成证据发生变化')
            self._put(bid, before, candidate, value)
            parts[bid] = value
        return parts

    def prune(self, batch_ids):
        keep = set(batch_ids)
        for bid in list(self.entries):
            if bid not in keep:
                self.entries.pop(bid, None)
