"""Long-running read-only discovery service with an isolated build worker."""
from __future__ import annotations

import argparse
import json
import math
import multiprocessing as mp
from pathlib import Path
import queue
import signal
import threading
import time

from .cli import load_json
from .discovery_queue import DiscoveryQueue
from .queue_files import publish
from .queue_source import sqlite_snapshot, ssh_snapshot
from .queue_evidence import build_job
from .queue_view import export


def _build_response(request, staging, allow_examples, delay):
    """Keep build temporaries scoped to one request, never across idle waits."""
    try:
        if delay:
            time.sleep(delay)
        model, deps, before = build_job(
            Path(staging), request['batch_id'], request['snapshot'],
            request['project_index'], allow_examples=allow_examples,
        )
        return {'job_id': request['job_id'], 'ok': True, 'result': (model, deps, before)}
    except BaseException as exc:
        return {'job_id': request['job_id'], 'ok': False, 'error': f'{type(exc).__name__}: {exc}'}


def _worker_main(requests, results, staging, projects, allow_examples, delay):
    """Build only; this process never opens the queue database."""
    while True:
        request = requests.get()
        if request is None:
            return
        response = _build_response(request, staging, allow_examples, delay)
        results.put(response)
        # Queue's feeder owns the response until sent. Do not retain another
        # copy of the last model/request while idle or building the next batch.
        del response, request


class QueueService:
    """Own the queue DB in the parent and delegate only pure build work."""

    def __init__(
        self,
        staging,
        projects,
        state,
        provider,
        *,
        allow_examples=False,
        interval=2.0,
        cycles=0,
        worker_delay=0.0,
        context=None,
    ):
        if type(cycles) is not int or cycles < 0:
            raise ValueError('cycles 不能为负数')
        if not isinstance(interval, (int, float)) or not math.isfinite(interval) or interval <= 0:
            raise ValueError('interval 必须是有限的正数')
        if interval < 2 and cycles == 0:
            raise ValueError('常驻观察间隔不能小于 2 秒')
        if not isinstance(worker_delay, (int, float)) or not math.isfinite(worker_delay) or worker_delay < 0:
            raise ValueError('worker_delay 必须是有限的非负数')
        self.staging = Path(staging)
        self.projects = Path(projects)
        self.state = Path(state)
        self.provider = provider
        self.interval = interval
        self.cycles = cycles
        self.worker_delay = worker_delay
        self.ctx = context or mp.get_context('spawn')
        self.queue = None
        self.requests = None
        self.results = None
        self.worker = None
        self.pending = None
        self.stop_event = threading.Event()
        self.allow_examples = allow_examples

    def _start_worker(self):
        if self.worker is not None and self.worker.is_alive():
            return
        if self.worker is not None:
            self._close_ipc()
            self.worker = None
        if self.requests is None:
            self.requests = self.ctx.Queue()
            self.results = self.ctx.Queue()
        self.worker = self.ctx.Process(
            target=_worker_main,
            args=(
                self.requests,
                self.results,
                str(self.staging),
                str(self.projects),
                self.allow_examples,
                self.worker_delay,
            ),
            name='muli-sorter-build-worker',
        )
        self.worker.daemon = True
        self.worker.start()

    def _close_ipc(self):
        for channel in (self.requests, self.results):
            if channel is not None:
                # A broken or terminated child may leave a feeder thread with
                # an unwritable pipe. Never wait indefinitely while stopping.
                channel.cancel_join_thread()
                channel.close()
        self.requests = None
        self.results = None

    def _stop_worker(self):
        worker = self.worker
        if worker is not None and worker.is_alive():
            try:
                self.requests.put(None)
                worker.join(timeout=2)
            except (BrokenPipeError, OSError):
                pass
        if worker is not None and worker.is_alive():
            worker.terminate()
            worker.join(timeout=2)
        if worker is not None and worker.is_alive() and hasattr(worker, 'kill'):
            worker.kill()
            worker.join(timeout=2)
        self.worker = None
        self._close_ipc()

    def _discard_pending_worker(self):
        self._stop_worker()
        self.pending = None
        self._start_worker()

    def _worker_result(self):
        if self.results is None:
            return None
        try:
            return self.results.get_nowait()
        except queue.Empty:
            return None

    def _health(self, *, last_discovery_epoch, observer_stopped, source_ok):
        return {
            'last_discovery_epoch': last_discovery_epoch,
            'observer_stopped': observer_stopped,
            'source_ok': source_ok,
            'worker_running': bool(self.worker and self.worker.is_alive()),
        }

    def _write_health(self, health):
        payload = (json.dumps(health, ensure_ascii=False, sort_keys=True) + '\n').encode()
        publish(self.queue.root, '', {'service-health.json': payload})

    def _submit_one(self, snapshot):
        if self.pending is not None or snapshot is None:
            return
        claimed = self.queue.claim_jobs(1)
        if not claimed:
            return
        job = claimed[0]
        self.pending = {'job': job, 'snapshot': snapshot}
        self.requests.put(
            {
                'job_id': job['id'],
                'batch_id': job['batch_id'],
                'snapshot': snapshot,
                'project_index': self.queue.index,
            }
        )

    def _reap_one(self, snapshot):
        if self.pending is None:
            return
        job = self.pending['job']
        row = next((item for item in self.queue.jobs() if item['id'] == job['id']), None)
        if row is None or row['state'] != 'processing':
            self._discard_pending_worker()
            return
        result = self._worker_result()
        if result is None:
            if self.worker is not None and not self.worker.is_alive():
                self.queue.fail_claimed(job, '分类工作进程意外退出')
                self.pending = None
                self._stop_worker()
                self._start_worker()
            return
        if result.get('job_id') != job['id']:
            return
        self.pending = None
        if not result.get('ok'):
            self.queue.fail_claimed(job, result.get('error', '分类工作进程失败'))
            self._start_worker()
            return
        if snapshot is None:
            self.pending = {'job': job, 'result': result['result']}
            return
        self.queue.accept_result(job, result['result'], self.provider)

    def _accept_held(self, snapshot):
        if self.pending is None or snapshot is None or 'result' not in self.pending:
            return
        job = self.pending['job']
        self.queue.accept_result(job, self.pending['result'], self.provider)
        self.pending = None

    def run(self):
        old_handlers = {}

        def stop(signum, _frame):
            self.stop_event.set()

        for signum in (signal.SIGINT, signal.SIGTERM):
            try:
                old_handlers[signum] = signal.getsignal(signum)
                signal.signal(signum, stop)
            except ValueError:
                pass

        cycles = 0
        status = None
        try:
            self.queue = DiscoveryQueue(
                self.staging,
                self.projects,
                self.state,
                allow_examples=self.allow_examples,
            )
            self._start_worker()
            while not self.stop_event.is_set() and (not self.cycles or cycles < self.cycles):
                snapshot, status = self.queue.discover(self.provider)
                last_discovery = time.time()
                self._reap_one(snapshot)
                self._accept_held(snapshot)
                row = next(
                    (item for item in self.queue.jobs() if self.pending and item['id'] == self.pending['job']['id']),
                    None,
                )
                if self.pending is not None and row is None:
                    self._discard_pending_worker()
                self._submit_one(snapshot)
                status = self.queue.status()
                export(self.queue, status)
                self._write_health(
                    self._health(
                        last_discovery_epoch=last_discovery,
                        observer_stopped=False,
                        source_ok=bool((status.get('source') or {}).get('ok')),
                    )
                )
                cycles += 1
                if self.cycles and cycles >= self.cycles:
                    break
                self.stop_event.wait(self.interval)
        finally:
            try:
                self._stop_worker()
                if self.queue is not None:
                    try:
                        status = self.queue.status()
                        export(self.queue, status, stopped=True)
                        source_ok = bool((status.get('source') or {}).get('ok'))
                        self._write_health(
                            self._health(
                                last_discovery_epoch=time.time(),
                                observer_stopped=True,
                                source_ok=source_ok,
                            )
                        )
                    finally:
                        self.queue.__exit__(None, None, None)
            finally:
                for signum, handler in old_handlers.items():
                    signal.signal(signum, handler)
        return status


def _provider(args):
    if args.snapshot:
        return lambda: load_json(args.snapshot)
    if args.ssh_host:
        return lambda: ssh_snapshot(args.ssh_host, args.remote_db)
    return lambda: sqlite_snapshot(args.state_db)


def main(argv=None):
    parser = argparse.ArgumentParser(description='木梨只读素材分类服务')
    parser.add_argument('--staging', required=True)
    parser.add_argument('--projects', required=True)
    parser.add_argument('--state-db', required=False)
    parser.add_argument('--output', required=True)
    parser.add_argument('--snapshot')
    parser.add_argument('--ssh-host')
    parser.add_argument('--remote-db')
    parser.add_argument('--interval', type=float, default=2.0)
    parser.add_argument('--cycles', type=int, default=0)
    parser.add_argument('--synthetic', action='store_true')
    args = parser.parse_args(argv)
    if (not math.isfinite(args.interval) or args.interval <= 0) or (args.interval < 2 and args.cycles == 0) or args.cycles < 0:
        parser.error('常驻 interval 至少为 2 秒，cycles 不能为负数')
    choices = sum(bool(x) for x in (args.state_db, args.snapshot, args.ssh_host))
    if choices != 1:
        parser.error('state-db、snapshot、ssh-host 必须且只能选择一个')
    if bool(args.ssh_host) != bool(args.remote_db):
        parser.error('SSH 主机和远程数据库路径必须同时提供')
    if args.synthetic and not args.snapshot:
        parser.error('合成模式只接受显式快照文件')
    if args.state_db:
        source = Path(args.state_db).resolve(strict=True)
        output = Path(args.output).resolve()
        if output == source.parent or output.is_relative_to(source.parent) or source.is_relative_to(output):
            parser.error('分类状态输出必须与 Ingest 状态目录分离')
    QueueService(
        args.staging,
        args.projects,
        args.output,
        _provider(args),
        allow_examples=args.synthetic,
        interval=args.interval,
        cycles=args.cycles,
    ).run()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
