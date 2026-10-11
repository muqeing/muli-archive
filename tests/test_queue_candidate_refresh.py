"""Project edits must not rerun media classification or hide ready batches."""
import json
import unittest
from unittest.mock import patch
import test_discovery_queue as fixtures
from muli_sorter.queue_evidence import signal


class CandidateRefreshTests(unittest.TestCase):
    setUp = fixtures.QueueTests.setUp
    queue = fixtures.QueueTests.queue
    provider = fixtures.QueueTests.provider

    def model(self, q, job):
        return json.loads((q.root/job['artifact']/'确认模型.json').read_text())

    def add_project(self, q, name):
        folder = self.projects/'2026/9月'/name
        folder.mkdir()
        q.index_at = 0
        return folder

    def test_unrelated_project_does_not_requeue_or_rebuild(self):
        with self.queue() as q:
            old = q.tick(self.provider)['jobs'][0]
            self.add_project(q, '20260929_00999_另一日期')
            with patch('muli_sorter.discovery_queue.build_job', side_effect=AssertionError('media rebuild')):
                now = q.tick(self.provider)['jobs'][0]
            self.assertEqual(now['state'], 'awaiting_confirmation')
            self.assertEqual((now['artifact'], now['token']), (old['artifact'], old['token']))

    def test_matching_project_refreshes_candidates_preserving_media_grouping(self):
        with self.queue() as q:
            old = q.tick(self.provider)['jobs'][0]
            before = self.model(q, old)
            self.add_project(q, '20260928_00999_新候选')
            with patch('muli_sorter.discovery_queue.build_job', side_effect=AssertionError('media rebuild')), patch('muli_sorter.queue_evidence.media_stat', side_effect=AssertionError('media stat')):
                now = q.tick(self.provider)['jobs'][0]
            after = self.model(q, now)
            self.assertEqual(now['state'], 'awaiting_confirmation')
            self.assertNotEqual(before['report_id'], after['report_id'])
            self.assertEqual(before['initial_segments'], after['initial_segments'])
            for units in (before['units'], after['units']):
                for unit in units:
                    unit.pop('candidate_project_ids')
            self.assertEqual(before['units'], after['units'])
            self.assertEqual(len(after['projects']), 2)

    def test_project_remove_and_rename_refresh_metadata_only(self):
        with self.queue() as q:
            first = q.tick(self.provider)['jobs'][0]
            path = self.projects/'2026/9月/20260928_00888_合成项目'
            path.rename(path.with_name('20260928_00888_改名'))
            q.index_at = 0
            with patch('muli_sorter.discovery_queue.build_job', side_effect=AssertionError('media rebuild')):
                renamed = q.tick(self.provider)['jobs'][0]
                self.assertEqual(self.model(q, renamed)['projects'][0]['name'], '20260928_00888_改名')
                path.with_name('20260928_00888_改名').rmdir()
                q.index_at = 0
                removed = q.tick(self.provider)['jobs'][0]
            self.assertEqual(removed['state'], 'awaiting_confirmation')
            self.assertEqual(self.model(q, removed)['projects'], [])
            self.assertEqual(self.model(q, removed)['units'][0]['candidate_project_ids'], [])
            self.assertNotEqual(first['artifact'], renamed['artifact'])

    def test_project_change_during_processing_accepts_media_result(self):
        with self.queue() as q:
            snapshot, _ = q.discover(self.provider)
            job = q.claim_jobs()[0]
            result = q.build_claimed(job, snapshot)
            self.add_project(q, '20260928_00999_新候选')
            q.discover(self.provider)
            self.assertEqual(q.jobs()[0]['state'], 'processing')
            self.assertTrue(q.accept_result(job, result, self.provider))
            now = q.jobs()[0]
            self.assertEqual(now['state'], 'awaiting_confirmation')
            self.assertEqual(len(self.model(q, now)['projects']), 2)

    def test_media_change_during_processing_still_discards_result(self):
        with self.queue() as q:
            snapshot, _ = q.discover(self.provider)
            job = q.claim_jobs()[0]
            result = q.build_claimed(job, snapshot)
            self.rows[0]['state'] = 'INTERRUPTED'
            q.accept_result(job, result, self.provider)
            self.assertEqual(q.jobs()[0]['state'], 'retry_wait')

    def test_verified_legacy_ready_model_migrates_without_media_rebuild(self):
        with self.queue() as q:
            old = q.tick(self.provider)['jobs'][0]
            model = self.model(q, old)
            legacy = signal(self.staging, [old['batch_id']], self.provider(), model['projects'])
            q.db.execute('UPDATE jobs SET token=? WHERE id=?', (legacy, old['id']))
            q.db.commit()
            self.add_project(q, '20260929_00999_无关')
            with patch('muli_sorter.discovery_queue.build_job', side_effect=AssertionError('media rebuild')):
                now = q.tick(self.provider)['jobs'][0]
            self.assertEqual(now['state'], 'awaiting_confirmation')
            self.assertTrue(now['token'].startswith('media-evidence/1:'))
            self.assertEqual(now['artifact'], old['artifact'])

    def test_unknown_legacy_result_is_not_promoted(self):
        with self.queue() as q:
            old = q.tick(self.provider)['jobs'][0]
            q.db.execute("UPDATE jobs SET token=? WHERE id=?", ('0'*64, old['id']))
            q.db.commit()
            q.discover(self.provider)
            self.assertEqual(q.jobs()[0]['state'], 'queued')

    def test_legacy_missing_artifact_is_rebuilt_without_promoting_unknown_result(self):
        with self.queue() as q:
            old = q.tick(self.provider)['jobs'][0]
            legacy = signal(self.staging, [old['batch_id']], self.provider(), self.model(q, old)['projects'])
            q.db.execute('UPDATE jobs SET token=? WHERE id=?', (legacy, old['id']))
            q.db.commit()
            (q.root/old['artifact']/'确认模型.json').unlink()
            q.discover(self.provider)
            self.assertEqual(q.jobs()[0]['state'], 'queued')

    def test_candidate_refresh_does_not_assign_project_or_unknown_date(self):
        from muli_sorter.queue_candidates import refresh
        with self.queue() as q:
            before = self.model(q, q.tick(self.provider)['jobs'][0])
            before['units'][0]['capture_date'] = None
            before['units'][0]['capture_time'] = None
            updated = refresh(before, q.index)
            self.assertEqual(updated['units'][0]['candidate_project_ids'], [])
            self.assertTrue(all(s['project_id'] is None and s['decision']=='pending' for s in updated['initial_segments']))

    def test_source_receipt_change_invalidates_ready_model(self):
        with self.queue() as q:
            q.tick(self.provider)
            path = self.staging/self.rows[0]['batch_id']/'ingest_manifest.json'
            path.write_bytes(path.read_bytes()+b' ')
            q.discover(self.provider)
            self.assertNotEqual(q.jobs()[0]['state'], 'awaiting_confirmation')

    def test_restart_preserves_media_token_and_candidate_refresh(self):
        with self.queue() as q:
            old = q.tick(self.provider)['jobs'][0]
        (self.projects/'2026/9月/20260928_00999_新增').mkdir()
        with self.queue() as q:
            with patch('muli_sorter.discovery_queue.build_job', side_effect=AssertionError('media rebuild')):
                now = q.tick(self.provider)['jobs'][0]
            self.assertEqual(now['state'], 'awaiting_confirmation')
            self.assertEqual(now['token'], old['token'])
            self.assertNotEqual(now['artifact'], old['artifact'])
