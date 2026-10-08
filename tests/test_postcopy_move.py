import json
import os
from pathlib import Path
import threading
import unittest
from unittest.mock import patch

from blake3 import blake3

from test_archive_jobs import Fixture
from muli_sorter.archive_fixture import save_manifest
from muli_sorter.postcopy_service import read_guard
from muli_sorter.staging_coordination import staging_guard


class PostcopyMoveTests(Fixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.postcopy = self.root / 'postcopy-verifications'
        self.postcopy.mkdir()
        self._make_size_only()
        self.service.close()
        self.receipt_env = patch.dict(os.environ, {'MULI_POSTCOPY_RECEIPTS': str(self.postcopy)})
        self.receipt_env.start()
        self.addCleanup(self.receipt_env.stop)
        self.service = self.make_service(move_enabled=True)

    def _make_size_only(self):
        batch_id = self.model['units'][0]['provenance'][0]['batch_id']
        batch = self.root / 'staging' / batch_id
        manifest = json.loads((batch / 'ingest_manifest.json').read_text())
        manifest['batch']['result'] = 'COPY_SIZE_VERIFIED'
        files = manifest['files']
        for item in files:
            item['copy_status'] = 'size_verified'
            item['hash']['destination'] = None
            item['hash']['existing_destination'] = None
            item['hash_match'] = None
            item['existing_copy'] = None
            item['error'] = None
        manifest['summary'].update(
            copied_file_count=len(files), verified_file_count=0,
            size_verified_file_count=len(files), awaiting_hash_file_count=0,
            pending_file_count=0, failed_file_count=0,
            copied_bytes=manifest['summary']['selected_bytes'],
            previously_ingested_count=0)
        save_manifest(self.root, manifest)
        runtime = json.loads((self.root / 'runtime-state.json').read_text())
        runtime['batches'][0].update(result='COPY_SIZE_VERIFIED', revision=manifest['revision'])
        (self.root / 'runtime-state.json').write_text(json.dumps(runtime))
        rows = []
        for item in files:
            source = batch / 'SOURCE_DATA' / item['relative_path']
            stat = source.stat()
            rows.append({
                'file_id': item['file_id'], 'relative_path': item['relative_path'],
                'resolved_path': f'{batch_id}/SOURCE_DATA/{item["relative_path"]}',
                'size_bytes': item['size_bytes'], 'blake3': item['hash']['source'],
                'source_signature': {
                    'dev': stat.st_dev, 'ino': stat.st_ino, 'size': stat.st_size,
                    'mtime_ns': stat.st_mtime_ns, 'ctime_ns': stat.st_ctime_ns,
                },
            })
        receipt = {
            'schema': 'postcopy-verification/1', 'status': 'completed',
            'verified_at': '2026-10-02T00:00:00+00:00', 'batch_id': batch_id,
            'batch_uid': manifest['batch']['batch_uid'], 'revision': manifest['revision'],
            'manifest_id': manifest['manifest_id'], 'source_id': manifest['batch']['source_id'],
            'original_manifest_blake3': blake3((batch / 'ingest_manifest.json').read_bytes()).hexdigest(),
            'original_ingest_complete_blake3': blake3((batch / 'ingest_complete.json').read_bytes()).hexdigest(),
            'files': rows,
        }
        (self.postcopy / f'{batch_id}.json').write_text(json.dumps(receipt))

    def _source_files(self):
        return sorted(p for p in (self.root / 'staging').rglob('*')
                      if p.is_file() and 'SOURCE_DATA' in p.parts)

    def _run(self, mode):
        self.decisions['archive_options'] = {'mode': mode, 'existing': 'skip_identical'}
        job = self.submit()
        result = self.service.run_job(job['job_id'])
        self.assertEqual(result['status'], 'completed', result)
        return result

    def test_size_only_normal_copy_keeps_sources(self):
        before = {path: path.read_bytes() for path in self._source_files()}
        self._run('copy')
        self.assertEqual(before, {path: path.read_bytes() for path in before})

    def test_size_only_normal_move_removes_sources_after_receipt_check(self):
        selected = {uid for segment in self.decisions['segments']
                    if segment['decision'] == 'confirmed' for uid in segment['unit_ids']}
        sources = [self.root / 'staging' / file['source_path']
                   for unit in self.model['units'] if unit['unit_id'] in selected
                   for file in unit['files']]
        self._run('move')
        self.assertTrue(all(not path.exists() for path in sources))

    def _tampered_move_keeps_all_sources(self, *, replace):
        sources = self._source_files()
        self.decisions['archive_options'] = {'mode': 'move', 'existing': 'skip_identical'}
        touched = [False]
        receipt_path = next(self.postcopy.glob('BATCH_*.json'))

        def tamper(phase, row):
            if phase != 'move_intent' or touched[0]:
                return
            touched[0] = True
            changed = json.loads(receipt_path.read_text())
            changed['verified_at'] = '2026-10-02T00:00:01+00:00'
            if replace:
                temporary = receipt_path.with_suffix('.replacement')
                temporary.write_text(json.dumps(changed))
                os.replace(temporary, receipt_path)
            else:
                receipt_path.write_text(json.dumps(changed))

        self.service.checkpoint = tamper
        job = self.submit()
        result = self.service.run_job(job['job_id'])
        self.assertEqual(result['status'], 'failed', result)
        self.assertTrue(touched[0])
        self.assertTrue(all(path.exists() for path in sources))

    def test_receipt_modified_after_move_intent_preserves_all_sources(self):
        self._tampered_move_keeps_all_sources(replace=False)

    def test_receipt_replaced_after_move_intent_preserves_all_sources(self):
        self._tampered_move_keeps_all_sources(replace=True)

    def _move_while_verifier_holds_read_lock(self, *, cancel):
        self.decisions['archive_options'] = {'mode': 'move', 'existing': 'skip_identical'}
        job = self.submit()
        sources = self._source_files()
        with staging_guard(self.service.staging, create=True):
            pass
        waiting = threading.Event()
        results = []
        save = self.service._save
        def mark_waiting(value):
            save(value)
            if value.get('phase', '').startswith('等待内容校验'):
                waiting.set()
        thread = threading.Thread(target=lambda: results.append(self.service.run_job(job['job_id'])))
        with patch.object(self.service, '_save', side_effect=mark_waiting):
            with read_guard(self.service.staging):
                thread.start()
                try:
                    self.assertTrue(waiting.wait(5), 'MOVE never reached cleanup coordination')
                    self.assertTrue(thread.is_alive())
                    self.assertTrue(all(p.exists() for p in sources))
                    if cancel:
                        self.service.stop_event.set()
                        thread.join(5)
                        self.assertFalse(thread.is_alive())
                        self.assertTrue(all(p.exists() for p in sources))
                except BaseException:
                    self.service.stop_event.set()
                    raise
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(results), 1)
        return results[0]

    def test_move_waits_for_independent_content_reader_then_finishes(self):
        result = self._move_while_verifier_holds_read_lock(cancel=False)
        self.assertEqual(result['status'], 'completed', result)

    def test_stop_during_reader_wait_preserves_every_source(self):
        result = self._move_while_verifier_holds_read_lock(cancel=True)
        self.assertEqual(result['status'], 'failed', result)
        self.assertIn('停止请求', str(result['errors']))


if __name__ == '__main__':
    unittest.main()
