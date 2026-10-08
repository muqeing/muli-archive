from copy import deepcopy
import json
import threading
import unittest
from unittest.mock import patch
from urllib.request import urlopen
from urllib.parse import quote

from test_archive_jobs import Fixture
from muli_sorter.archive_console import make_server
from muli_sorter.archive_io import ArchiveError
from muli_sorter.order_feed_io import atomic_json


class HistoryTests(Fixture, unittest.TestCase):
    def snapshot(self):
        return self.service.history.snapshot(self.model)

    def complete(self, move=False):
        self.service.move_enabled = True
        self.decisions['archive_options'] = {'mode': 'move' if move else 'copy', 'existing': 'skip_identical'}
        job = self.submit()
        result = self.service.run_job(job['job_id'])
        self.assertEqual(result['status'], 'completed', result)
        return result

    def test_old_copy_receipts_are_projected_without_modifying_snapshot_or_sources(self):
        before = deepcopy(self.model)
        done = self.complete()
        # Pre-options tasks use copy semantics.
        done.pop('archive_options')
        self.service._save(done)
        state = self.snapshot()
        self.assertEqual(len(state['archived_units']), 2)
        self.assertEqual(sum(r['file_count'] for r in state['archived_units']), 4)
        self.assertTrue(all(r['in_current_model'] and r['mode'] == 'copy' for r in state['archived_units']))
        self.assertFalse(state['warnings'])
        self.assertEqual(self.model, before)
        for unit in self.model['units']:
            for file in unit['files']:
                self.assertTrue((self.root / 'staging' / file['source_path']).exists())
        for item in state['archived_units']:
            self.assertTrue(item['completed_at'])
            for file in item['files']:
                self.assertTrue(file['target_path'].startswith(item['project']['path'] + '/'))

    def test_partial_copy_removes_only_successful_units(self):
        job = self.submit()
        tripped = [False]
        def checkpoint(phase, row):
            if phase == 'copy_chunk' and not tripped[0]:
                tripped[0] = True
                raise ArchiveError('synthetic interruption')
        self.service.checkpoint = checkpoint
        done = self.service.run_job(job['job_id'])
        self.assertEqual(done['status'], 'partial')
        expected = {r['unit_id'] for r in done['outcomes'] if r['status'] == 'completed'}
        self.assertEqual({r['unit_id'] for r in self.snapshot()['archived_units']}, expected)
        self.assertEqual(len(expected), 1)

    def test_move_waits_for_source_cleanup_and_survives_restart(self):
        done = self.complete(move=True)
        self.service.close()
        self.service = self.make_service(move_enabled=True)
        result = self.snapshot()
        self.assertEqual(len(result['archived_units']), 2)
        self.assertTrue(all(r['mode'] == 'move' and r['in_current_model'] for r in result['archived_units']))
        self.assertEqual(done['summary']['removed_sources'], 4)

    def test_partial_move_copy_is_not_complete_until_cleanup(self):
        self.service.move_enabled = True
        self.decisions['archive_options'] = {'mode': 'move', 'existing': 'skip_identical'}
        job = self.submit()
        def checkpoint(phase, row):
            if phase == 'copy_chunk' and row['name'].endswith('.JPG'):
                raise ArchiveError('synthetic interruption')
        self.service.checkpoint = checkpoint
        done = self.service.run_job(job['job_id'])
        self.assertEqual(done['status'], 'partial')
        self.assertGreater(done['summary']['completed_units'], 0)
        self.assertEqual(self.snapshot()['archived_units'], [])

    def test_move_journal_is_indexed_once_per_unchanged_snapshot(self):
        done = self.complete(move=True)
        journal = self.service.state / 'requests' / ('move-' + done['job_id'] + '.json')
        history = self.service.history
        with patch.object(history, 'read', wraps=history.read) as reader:
            self.assertEqual(len(self.snapshot()['archived_units']), 2)
        self.assertEqual(sum(call.args[0] == journal for call in reader.call_args_list), 1)

    def test_changed_move_journal_invalidates_index_within_snapshot(self):
        done = self.complete(move=True)
        journal_path = self.service.state / 'requests' / ('move-' + done['job_id'] + '.json')
        history = self.service.history
        original = history._item
        first = done['outcomes'][0]['unit_id']
        calls = [0]
        def project(*args, **kwargs):
            item = original(*args, **kwargs)
            calls[0] += 1
            if calls[0] == 1:
                journal = json.loads(journal_path.read_text())
                for row in journal['files']:
                    row['state'] = 'pending'
                atomic_json(journal_path, journal)
            return item
        with patch.object(history, '_item', side_effect=project):
            self.assertEqual([r['unit_id'] for r in self.snapshot()['archived_units']], [first])

    def test_partial_cleanup_projects_only_whole_removed_units(self):
        self.service.move_enabled = True
        self.decisions['archive_options'] = {'mode': 'move', 'existing': 'skip_identical'}
        job = self.submit()
        removed = [0]
        first_uid = self.decisions['segments'][0]['unit_ids'][0]
        count = len(next(u for u in self.model['units'] if u['unit_id'] == first_uid)['files'])
        def checkpoint(phase, row):
            if phase == 'source_removed':
                removed[0] += 1
            if phase == 'move_intent' and removed[0] == count:
                raise ArchiveError('synthetic cleanup interruption')
        self.service.checkpoint = checkpoint
        done = self.service.run_job(job['job_id'])
        self.assertEqual(done['status'], 'failed')
        state = self.snapshot()
        self.assertEqual([r['unit_id'] for r in state['archived_units']], [first_uid])

    def test_completed_counter_without_receipt_does_not_hide_anything(self):
        job = self.submit()
        job.update(status='completed', summary={'completed_files': 4, 'completed_units': 2})
        self.service._save(job)
        self.assertEqual(self.snapshot()['archived_units'], [])

    def test_missing_receipt_and_changed_file_identity_keep_items_pending(self):
        done = self.complete()
        uid = done['outcomes'][0]['unit_id']
        unit = next(u for u in self.model['units'] if u['unit_id'] == uid)
        unit['files'][0]['blake3'] = '0' * 64
        result = self.snapshot()
        self.assertFalse(next(r for r in result['archived_units'] if r['unit_id'] == uid)['in_current_model'])
        self.assertTrue(result['warnings'])
        (self.service.state / 'units' / done['outcomes'][1]['receipt']).unlink()
        result = self.snapshot()
        self.assertFalse(any(r['in_current_model'] for r in result['archived_units']))
        self.assertTrue(result['warnings'])

    def test_history_is_not_limited_to_recent_twenty_jobs_and_cache_invalidates(self):
        done = self.complete()
        for i in range(21):
            self.service._save({'job_id': f'{i:064x}', 'status': 'failed', 'example_data': True, 'outcomes': []})
        self.assertNotIn(done['job_id'], {j['job_id'] for j in self.service.list_jobs()})
        self.assertEqual(len(self.snapshot()['archived_units']), 2)
        receipt_path = self.service.state / 'units' / done['outcomes'][0]['receipt']
        receipt = json.loads(receipt_path.read_text())
        receipt['status'] = 'incomplete'
        atomic_json(receipt_path, receipt)
        self.assertEqual(len(self.snapshot()['archived_units']), 1)

    def test_current_report_change_does_not_erase_archive_history(self):
        self.complete()
        self.model['report_id'] = 'new-report'
        self.model['units'] = []
        result = self.snapshot()
        self.assertEqual(result['report_id'], 'new-report')
        self.assertEqual(len(result['archived_units']), 2)
        self.assertFalse(any(r['in_current_model'] for r in result['archived_units']))

    def test_large_receipt_set_stays_cached_with_bounded_size_and_invalidates(self):
        history = self.service.history
        folder = self.service.state / 'cache-fixture'
        folder.mkdir()
        paths = []
        for index in range(1100):
            path = folder / f'{index}.json'
            atomic_json(path, {'index': index})
            paths.append(path)
        from muli_sorter.archive_history import read_json
        with patch('muli_sorter.archive_history.read_json', wraps=read_json) as reader:
            for path in paths:
                history.read(path)
            for path in paths:
                history.read(path)
            self.assertEqual(reader.call_count, len(paths))
            atomic_json(paths[0], {'index': 'changed'})
            self.assertEqual(history.read(paths[0]), {'index': 'changed'})
            self.assertEqual(reader.call_count, len(paths) + 1)
        with patch('muli_sorter.archive_history.HISTORY_CACHE_BYTES', 128):
            atomic_json(paths[-1], {'changed': 'x' * 160})
            history.read(paths[-1])
        self.assertLessEqual(history.cache_bytes, 128)

    def test_readonly_http_archive_state(self):
        self.complete()
        server = make_server(self.service, self.root / 'queue')
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            with urlopen(server.public_origin + '/api/archive-state') as response:
                result = json.load(response)
            self.assertEqual(len(result['archived_units']), 2)
            self.assertEqual(result['report_id'], self.model['report_id'])
            with urlopen(server.public_origin + '/api/archive-state?report_id=' + quote(self.model['report_id'])) as response:
                self.assertEqual(json.load(response), result)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_open_page_receives_completion_after_project_scan_changes_report(self):
        self.snapshot()  # The page was opened before this archive started.
        old_id = self.model['report_id']
        self.complete()
        self.model['report_id'] = 'new-report-after-directory-created'
        self.model['units'] = []
        self.snapshot()
        old = self.service.history.for_report(old_id)
        self.assertEqual(old['report_id'], old_id)
        self.assertEqual(sum(r['in_current_model'] for r in old['archived_units']), 2)
        with self.assertRaisesRegex(ValueError, '过期'):
            self.service.history.for_report('never-opened-report')
