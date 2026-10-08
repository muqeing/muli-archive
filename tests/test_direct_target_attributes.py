"""Real chmod/content races against the direct-MOVE transaction boundaries."""
import json
import os
import time
import unittest
from unittest.mock import patch

from test_archive_jobs import Fixture
from muli_sorter.archive_direct_journal import DirectJournal
from muli_sorter.archive_io import verified_target_hash


def attributes(path):
    before = path.stat()
    time.sleep(.002)
    path.chmod((before.st_mode & 0o777) ^ 0o100)
    after = path.stat()
    assert (before.st_ino, before.st_size, before.st_mtime_ns) == (after.st_ino, after.st_size, after.st_mtime_ns)
    assert before.st_ctime_ns != after.st_ctime_ns


class DirectTargetAttributeTests(Fixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.service.move_enabled = True
        self.service.direct_move_view = {'root':str(self.root),'staging':'staging','projects':'projects'}
        self.decisions['archive_options'] = {'mode':'move','existing':'skip_identical'}

    def target(self, row):
        return self.root/'projects'/row['target_path']

    def restart(self):
        self.service.close()
        self.service = self.make_service(move_enabled=True, direct_move_view={
            'root':str(self.root),'staging':'staging','projects':'projects'})

    def done(self, job):
        result = self.service.run_job(job['job_id'])
        self.assertEqual(result['status'], 'completed', result)
        self.assertEqual(result['summary']['copy_files'], 0)
        self.assertEqual(result['summary']['removed_sources'], 4)
        return result

    def after_barrier(self, action):
        original = DirectJournal.save
        injected = []
        def save(journal):
            result = original(journal)
            if (not injected and 'completed_at' not in journal.journal and
                    all(e['state'] == 'removed' for e in journal.journal['files'])):
                injected.append(True)
                action()
            return result
        return patch.object(DirectJournal, 'save', save)

    def test_unchanged_targets_do_not_read_contents_again(self):
        job = self.submit()
        inodes = {p:(self.root/'staging'/p).stat().st_ino for p in job['file_plans']}
        with patch('muli_sorter.archive_target_metadata.verified_target_hash',
                   side_effect=AssertionError('unchanged target was re-read')):
            self.done(job)
        for path, row in job['file_plans'].items():
            self.assertEqual(self.target(row).stat().st_ino, inodes[path])

    def test_final_readback_chmod_rehashes_only_changed_target_and_recovers(self):
        job = self.submit(); row = next(iter(job['file_plans'].values()))
        with self.after_barrier(lambda: attributes(self.target(row))), patch(
                'muli_sorter.archive_target_metadata.verified_target_hash', wraps=verified_target_hash) as hashed:
            self.done(job)
        self.assertEqual(hashed.call_count, 1)
        self.restart()
        self.assertEqual(self.service.run_job(job['job_id'])['status'], 'completed')
        self.assertEqual(len(self.service.history.snapshot(self.model)['archived_units']), 2)

    def test_final_readback_attribute_update_completes(self):
        # Also run unchanged against the old image source to reproduce the
        # actual final-readback failure, without mocking a new helper.
        job = self.submit(); row = next(iter(job['file_plans'].values()))
        with self.after_barrier(lambda: attributes(self.target(row))):
            self.done(job)

    def test_final_readback_restored_mtime_content_change_is_not_accepted(self):
        job = self.submit(); row = next(iter(job['file_plans'].values())); dst = self.target(row)
        def mutate():
            before = dst.stat(); data = dst.read_bytes()
            dst.write_bytes(bytes([data[0] ^ 1]) + data[1:])
            os.utime(dst, ns=(before.st_atime_ns, before.st_mtime_ns))
        with self.after_barrier(mutate):
            result = self.service.run_job(job['job_id'])
        self.assertEqual(result['status'], 'failed', result)
        self.assertFalse(list((self.service.state/'units').glob('receipt-*.json')))
        self.assertFalse((self.service.state/'requests'/('move-'+job['job_id']+'.json')).exists())

    def test_recovered_recorded_target_chmod_requires_content_proof(self):
        job = self.submit(); moved = []
        def checkpoint(phase, row):
            if phase == 'direct_move_renamed':
                moved.append(row)
                if len(moved) == 2:
                    raise RuntimeError('crash after first target signature was durable')
        self.service.checkpoint = checkpoint
        with self.assertRaises(RuntimeError): self.service.run_job(job['job_id'])
        attributes(self.target(moved[0])); self.restart()
        with patch('muli_sorter.archive_target_metadata.verified_target_hash', wraps=verified_target_hash) as hashed:
            self.done(job)
        self.assertEqual(hashed.call_count, 1)

    def test_duplicate_target_chmod_keeps_original_journal_binding_across_crash(self):
        from muli_sorter.archive_layout import studio_target_rows
        segment = next(s for s in self.decisions['segments'] if s['decision']=='confirmed')
        unit = next(u for u in self.model['units'] if u['unit_id']==segment['unit_ids'][0])
        project = next(p for p in self.model['projects'] if p['project_id']==segment['project_id'])
        row = studio_target_rows(unit,project)[0]
        dst = self.target(row); dst.parent.mkdir(parents=True,exist_ok=True)
        dst.write_bytes((self.root/'staging'/row['source_path']).read_bytes())
        job = self.submit(); changed = []
        def checkpoint(phase, item):
            if phase == 'direct_move_renamed' and not changed:
                attributes(dst); changed.append(True)
            if phase == 'direct_move_removed':
                raise RuntimeError('crash after duplicate unlink')
        self.service.checkpoint = checkpoint
        with self.assertRaises(RuntimeError): self.service.run_job(job['job_id'])
        journal_path = self.service.state/'requests'/('direct-move-'+job['job_id']+'.json')
        old = json.loads(journal_path.read_text())
        expected = next(e['target_signature'] for e in old['files'] if e.get('target_preexisting'))
        self.assertNotEqual(dst.stat().st_ctime_ns, expected[-1])
        self.restart(); result = self.done(job)
        new = json.loads(journal_path.read_text())
        self.assertEqual(next(e['target_signature'] for e in new['files'] if e.get('target_preexisting')), expected)
        self.assertEqual(result['summary']['skipped_files'], 1)

    def test_source_attribute_drift_still_blocks_before_rename(self):
        job = self.submit()
        def checkpoint(phase, row):
            if phase == 'direct_move_intent': attributes(self.root/'staging'/row['source_path'])
        self.service.checkpoint = checkpoint
        result = self.service.run_job(job['job_id'])
        self.assertEqual(result['status'], 'failed', result)
        self.assertTrue(all((self.root/'staging'/p).exists() for p in job['file_plans']))
