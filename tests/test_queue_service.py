import copy
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from test_intake import fixture, write
from muli_sorter.queue_service import QueueService


class QueueServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.staging = self.root / 'staging'
        self.projects = self.root / 'projects'
        self.staging.mkdir()
        (self.projects / '2026/9月/20260928_00888_合成项目').mkdir(parents=True)
        first = fixture(self.staging)
        second = fixture(self.staging, 'BATCH_20260928_000002', 'uid2')
        write(self.staging, second)
        self.first_row = {**first['batch'], 'revision': 1}
        self.second_row = {**second['batch'], 'revision': 1}
        self.rows = [self.first_row]

    def provider(self):
        return {
            'generated_at': datetime.now(timezone.utc).isoformat(),
            'complete_listing': True,
            'batches': copy.deepcopy(self.rows),
        }

    def service(self, provider=None, **kwargs):
        options = {'interval': 0.05}
        options.update(kwargs)
        return QueueService(
            self.staging,
            self.projects,
            self.root / 'queue',
            provider or self.provider,
            **options,
        )

    def read_jobs(self):
        with sqlite3.connect(self.root / 'queue' / 'queue.sqlite3') as db:
            return db.execute('SELECT batch_id,state FROM jobs ORDER BY batch_id').fetchall()

    def test_discovery_continues_while_worker_is_busy_and_health_is_written(self):
        calls = 0

        def second_batch_appears():
            nonlocal calls
            calls += 1
            result = self.provider()
            if calls >= 2:
                result['batches'].append(copy.deepcopy(self.second_row))
            return result

        self.service(provider=second_batch_appears, cycles=8, worker_delay=0.7).run()
        jobs = self.read_jobs()
        self.assertCountEqual([state for _, state in jobs], ['processing', 'queued'])
        health = json.loads((self.root / 'queue' / 'service-health.json').read_text())
        self.assertEqual(set(health), {'last_discovery_epoch', 'observer_stopped', 'source_ok', 'worker_running'})
        self.assertTrue(health['observer_stopped'])
        self.assertFalse(health['worker_running'])
        self.assertTrue(time.time() - health['last_discovery_epoch'] < 5)

    def test_state_change_during_build_never_publishes_stale_result_then_recovers(self):
        calls = 0

        def changing_provider():
            nonlocal calls
            calls += 1
            result = self.provider()
            if calls >= 2:
                result['batches'][0]['state'] = 'INTERRUPTED'
                result['batches'][0]['result'] = 'NOT_VERIFIED'
                result['batches'][0]['completed_at'] = None
            return result

        self.service(provider=changing_provider, cycles=4, worker_delay=0.6).run()
        jobs = self.read_jobs()
        first = next(state for batch_id, state in jobs if batch_id == 'BATCH_20260928_000001')
        self.assertEqual(first, 'waiting_completion')
        self.assertFalse((self.root / 'queue' / 'reports').exists())

        recovered = self.service(cycles=60, worker_delay=0.0)
        recovered.run()
        jobs = self.read_jobs()
        self.assertIn(('BATCH_20260928_000001', 'awaiting_confirmation'), jobs)

    def test_interval_rejects_non_finite_or_non_positive_values(self):
        for interval in (0, -1, float('nan'), float('inf'), float('-inf')):
            with self.subTest(interval=interval):
                with self.assertRaises(ValueError):
                    self.service(interval=interval, cycles=1)
        with self.assertRaises(ValueError):
            self.service(interval=1, cycles=0)

    def test_initialization_failure_restores_signals_and_stop_without_worker(self):
        service=self.service(cycles=1)
        service._stop_worker()
        handlers={s:signal.getsignal(s) for s in (signal.SIGINT,signal.SIGTERM)}
        with patch('muli_sorter.queue_service.DiscoveryQueue',side_effect=RuntimeError('synthetic init failure')):
            with self.assertRaisesRegex(RuntimeError,'synthetic init failure'):
                service.run()
        self.assertEqual(handlers,{s:signal.getsignal(s) for s in handlers})

    def test_cli_finite_observation_exits_successfully_without_dumping_status(self):
        snapshot=self.root/'snapshot.json'
        snapshot.write_text(json.dumps(self.provider()))
        result=subprocess.run([sys.executable,'-m','muli_sorter.queue_service',
            '--staging',str(self.staging),'--projects',str(self.projects),'--output',str(self.root/'queue'),
            '--snapshot',str(snapshot),'--cycles','1','--interval','0.05'],capture_output=True,text=True,timeout=15)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(result.stdout,'')


if __name__ == '__main__':
    unittest.main()
