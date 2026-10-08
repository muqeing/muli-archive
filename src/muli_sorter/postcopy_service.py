"""Independent content reader. Its only writes are receipts in its own state root."""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import signal
import stat
import threading
import time

from blake3 import blake3
from .archive_io import directory, exclusive_lock, open_file, subdirectory, atomic_json
from .intake import BATCH, EvidenceError, _open, decode, read_bytes, runtime_check, validate_record
from .postcopy_receipt import (ENV_NAME, validate_payload, source_signature,
                               _read_file, _validate_result)
from .queue_source import sqlite_snapshot, validate_snapshot
from .staging_coordination import batch_guard, staging_guard, NAME, PROTOCOL


def now():
    return datetime.now(timezone.utc).isoformat()


def attrs(fd):
    return source_signature(fd, 'postcopy-verification/2')


@contextmanager
def read_guard(staging, bid=None):
    """Read-only batch lock. Unscoped legacy callers cannot enter v2."""
    if bid is not None:
        with batch_guard(staging, [bid], readonly=True):
            yield
    else:
        with staging_guard(staging, readonly=True), directory(staging) as root:
            fd = open_file(root, NAME)
            try:
                if os.pread(fd, 128, 0) != PROTOCOL:
                    raise EvidenceError('按批次协调需要明确校验批次')
                yield
            finally:
                os.close(fd)


def controls(staging, bid):
    return {n: read_bytes(staging, bid + '/' + n) for n in
            ('ingest_manifest.json', 'ingest_complete.json', 'ingest_manifest.md')}


class Verifier:
    def __init__(self, staging, output, provider, *, allow_examples=False,
                 stop=None, checkpoint=None):
        self.staging, self.output = Path(staging).absolute(), Path(output).absolute()
        # Refuse media/state overlap without silently following symlinks.
        with directory(self.staging), directory(self.output):
            pass
        if (self.output == self.staging or self.staging in self.output.parents or
                self.output in self.staging.parents):
            raise EvidenceError('独立校验状态目录不能与中转区重叠')
        if Path(os.environ.get(ENV_NAME, '')).absolute() != self.output:
            raise EvidenceError('独立校验回执目录与输出目录不一致')
        self.provider, self.examples = provider, allow_examples
        self.stop = stop or threading.Event()
        self.checkpoint = checkpoint or (lambda event, row: None)

    def status(self, bid, row):
        with directory(self.output) as fd:
            atomic_json(fd, bid + '.status.json', {**row, 'batch_id': bid, 'updated_at': now()})

    def verify(self, bid, *, checkpoint=None, renew_legacy_digest=None):
        if not BATCH.fullmatch(bid):
            raise EvidenceError('非法批次编号')
        checkpoint = checkpoint or self.checkpoint
        with read_guard(self.staging, bid):
            initial = controls(self.staging, bid)
            m = decode(initial['ingest_manifest.json'])
            completion = decode(initial['ingest_complete.json'])
            if m.get('example_data') is not self.examples:
                raise EvidenceError('真实与合成校验模式不一致')
            if (m['batch'].get('state') != 'COMPLETED' or
                    m['batch'].get('result') != 'COPY_SIZE_VERIFIED'):
                raise EvidenceError('仅接收完成的大小校验批次')
            if (completion.get('algorithm') != 'blake3' or
                    completion.get('json_blake3') != blake3(initial['ingest_manifest.json']).hexdigest() or
                    completion.get('md_blake3') != blake3(initial['ingest_manifest.md']).hexdigest()):
                raise EvidenceError('原始完成回执与清单不一致')
            runtime_check(validate_snapshot(self.provider()), m)
            target = bid + '.json'
            if renew_legacy_digest is not None:
                original = _read_file(self.staging, bid, required=True)
                old = _validate_result(self.staging, bid, m, initial['ingest_manifest.json'],
                                       initial['ingest_complete.json'], original)
                if old['schema'] != 'postcopy-verification/1' or old['receipt_blake3'] != renew_legacy_digest:
                    raise EvidenceError('续验必须绑定明确的旧版完成回执摘要')
                target = bid + '.v2.json'
            with directory(self.output) as output:
                try:
                    existing = open_file(output, target)
                except FileNotFoundError:
                    existing = None
                if existing is not None:
                    os.close(existing)
                    raise EvidenceError('已存在独立完成回执，不覆盖或自动重放')
            rows = []
            for f in m['files']:
                path = bid + '/' + f['destination_relative_path']
                fd = _open(self.staging, path)
                try:
                    identity = attrs(fd)
                finally:
                    os.close(fd)
                rows.append({'file_id': f['file_id'], 'relative_path': f['relative_path'],
                             'resolved_path': path, 'size_bytes': f['size_bytes'],
                             'blake3': f['hash']['source'], 'source_signature': identity})
            receipt = dict(schema='postcopy-verification/2', status='completed', verified_at=now(),
                batch_id=bid, batch_uid=m['batch']['batch_uid'], revision=m['revision'],
                manifest_id=m['manifest_id'], source_id=m['batch'].get('source_id'),
                original_manifest_blake3=blake3(initial['ingest_manifest.json']).hexdigest(),
                original_ingest_complete_blake3=blake3(initial['ingest_complete.json']).hexdigest(), files=rows)
            if renew_legacy_digest is not None:
                receipt['previous_receipt_blake3'] = renew_legacy_digest
            validate_payload(self.staging, bid, m, initial['ingest_manifest.json'],
                             initial['ingest_complete.json'], receipt)
            progress = {'schema': 'postcopy-status/1', 'state': 'verifying',
                        'total_files': len(rows), 'verified_files': 0,
                        'total_bytes': sum(r['size_bytes'] for r in rows), 'read_bytes': 0}
            self.status(bid, progress)
            last = time.monotonic()
            for row in rows:
                fd = _open(self.staging, row['resolved_path'])
                try:
                    if attrs(fd) != row['source_signature']:
                        raise EvidenceError('校验开始前来源身份改变')
                    h = blake3()
                    while chunk := os.read(fd, 4 * 1024 * 1024):
                        if self.stop.is_set():
                            raise EvidenceError('独立校验停止；未发布完成回执')
                        h.update(chunk); progress['read_bytes'] += len(chunk)
                        checkpoint('hash_chunk', row)
                        if time.monotonic() - last >= 1:
                            self.status(bid, progress); last = time.monotonic()
                    if attrs(fd) != row['source_signature'] or h.hexdigest() != row['blake3']:
                        raise EvidenceError('来源内容或身份与原始摘要不一致：' + row['relative_path'])
                    current = _open(self.staging, row['resolved_path'])
                    try:
                        if attrs(current) != row['source_signature']:
                            raise EvidenceError('校验期间来源路径被替换')
                    finally:
                        os.close(current)
                finally:
                    os.close(fd)
                progress['verified_files'] += 1
                self.status(bid, progress)
            checkpoint('before_publish', receipt)
            if controls(self.staging, bid) != initial:
                raise EvidenceError('校验期间原始备份记录改变')
            runtime_check(validate_snapshot(self.provider()), m)
            for row in rows:
                fd = _open(self.staging, row['resolved_path'])
                try:
                    if attrs(fd) != row['source_signature']:
                        raise EvidenceError('完成回执发布前来源改变')
                finally:
                    os.close(fd)
            receipt['verified_at'] = now()
            if renew_legacy_digest is not None:
                original = _read_file(self.staging, bid, required=True)
                if blake3(original[0]).hexdigest() != renew_legacy_digest:
                    raise EvidenceError('续验期间旧版回执改变；不发布新回执')
            with directory(self.output) as fd:
                # The single-service lock is held in run(); never replace a
                # previously published receipt, including an unreadable one.
                try:
                    existing = open_file(fd, target)
                except FileNotFoundError:
                    existing = None
                if existing is not None:
                    os.close(existing)
                    raise EvidenceError('已存在独立完成回执，不覆盖或自动重放')
                atomic_json(fd, target, receipt)
            validate_record(self.staging, bid, allow_examples=self.examples)
            progress['state'] = 'completed'
            self.status(bid, progress)
            return receipt

    def cycle(self):
        snapshot = validate_snapshot(self.provider())
        for row in snapshot['batches']:
            if self.stop.is_set():
                break
            if row['state'] != 'COMPLETED' or row['result'] != 'COPY_SIZE_VERIFIED':
                continue
            bid = row['batch_id']
            if (self.output / (bid + '.json')).exists():
                # Completed evidence survives later MOVE cleanup. Admission
                # consumers revalidate it; this service never silently rewrites it.
                continue
            status_path = self.output / (bid + '.status.json')
            if status_path.exists():
                prior = decode(read_bytes(self.output, status_path.name))
                if prior.get('state') in ('blocked', 'failed', 'interrupted', 'manual_archive_verified'):
                    continue  # Explicit review, not blind automatic retries.
            try:
                self.verify(bid)
            except BlockingIOError:
                self.status(bid, {'schema': 'postcopy-status/1', 'state': 'waiting_lock',
                                  'error': '正在等待移动清理结束；未开始读取内容'})
            except Exception as exc:
                self.status(bid, {'schema': 'postcopy-status/1', 'state':
                    'interrupted' if self.stop.is_set() else 'blocked' if isinstance(exc, FileNotFoundError) else 'failed',
                    'error': str(exc)[:400]})

    def run(self, *, interval=2, cycles=0):
        with directory(self.output) as fd, exclusive_lock(fd):
            count = 0
            while not self.stop.is_set() and (not cycles or count < cycles):
                try:
                    self.cycle()
                except Exception as exc:
                    atomic_json(fd, 'service-status.json', {'state': 'source_unavailable',
                        'updated_at': now(), 'error': str(exc)[:400]})
                count += 1
                if not cycles or count < cycles:
                    self.stop.wait(interval)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--staging', required=True); p.add_argument('--output', required=True)
    p.add_argument('--state-db', required=True); p.add_argument('--cycles', type=int, default=0)
    p.add_argument('--renew-legacy-batch')
    p.add_argument('--legacy-receipt-blake3')
    args = p.parse_args()
    if args.cycles < 0:
        p.error('cycles 不能为负数')
    if bool(args.renew_legacy_batch) != bool(args.legacy_receipt_blake3):
        p.error('独立续验须同时给出批次与旧回执摘要')
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    os.environ[ENV_NAME] = args.output
    verifier = Verifier(args.staging, args.output, lambda: sqlite_snapshot(args.state_db), stop=stop)
    if args.renew_legacy_batch:
        with directory(verifier.output) as fd, exclusive_lock(fd):
            try:
                verifier.verify(args.renew_legacy_batch, renew_legacy_digest=args.legacy_receipt_blake3)
            except Exception:
                if BATCH.fullmatch(args.renew_legacy_batch):
                    verifier.status(args.renew_legacy_batch, {'schema': 'postcopy-status/1',
                        'state': 'interrupted' if stop.is_set() else 'failed',
                        'operation': 'explicit_legacy_renewal',
                        'error': '独立续验未通过；保留旧回执，不自动重放'})
                raise
    else:
        verifier.run(cycles=args.cycles)


if __name__ == '__main__':
    main()
