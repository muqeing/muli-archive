"""Recovery contract exercised through the real worker with synthetic files."""
import os
import unittest
from unittest.mock import patch
from test_archive_direct_move import DirectMoveTests
from muli_sorter.archive_io import ArchiveError, hash_fd


class FastRecoveryIntegrationTests(DirectMoveTests):
    def interrupt(self, phase):
        job=self.submit()
        def crash(name, row):
            if name==phase:raise RuntimeError('simulated crash')
        self.service.checkpoint=crash
        with self.assertRaises(RuntimeError):self.service.run_job(job['job_id'])
        self.restart()
        return job

    def test_all_moved_before_receipt_crash_reuses_every_target_without_read(self):
        job=self.interrupt('direct_move_receipt')
        with patch('muli_sorter.archive_direct_move.hash_fd', side_effect=AssertionError('media reread forbidden')):
            done=self.complete(job)
        self.assertEqual(done['recovery_verification']['reused_files'],4)
        self.assertEqual(done['recovery_verification']['hashed_files'],0)
        self.assertEqual(done['recovery_verification']['checked_files'],4)
        self.assertEqual(done['finalization'],{'completed_units':2,'total_units':2})

    def test_crash_before_rename_reuses_unchanged_verified_sources(self):
        job=self.interrupt('direct_move_intent')
        with patch('muli_sorter.archive_direct_move.hash_fd', side_effect=AssertionError('media reread forbidden')):
            done=self.complete(job)
        self.assertEqual(done['recovery_verification']['reused_files'],4)

    def test_uncertain_rename_hashes_only_one_file(self):
        job=self.interrupt('direct_move_renamed')
        with patch('muli_sorter.archive_direct_move.hash_fd', wraps=hash_fd) as reader:
            done=self.complete(job)
        self.assertEqual(reader.call_count,1)
        self.assertEqual(done['recovery_verification']['hashed_files'],1)
        self.assertEqual(done['recovery_verification']['reused_files'],3)

    def test_removed_target_changed_same_size_and_mtime_fails_closed(self):
        job=self.interrupt('direct_move_receipt')
        target=self.root/'projects'/next(iter(job['file_plans'].values()))['target_path']
        before=target.stat();target.write_bytes(b'X'*before.st_size)
        os.utime(target,ns=(before.st_atime_ns,before.st_mtime_ns))
        with patch('muli_sorter.archive_direct_move.hash_fd',side_effect=AssertionError('changed identity cannot be adopted')):
            done=self.service.run_job(job['job_id'])
        self.assertEqual(done['status'],'failed')
        self.assertTrue(done['direct_move_recovery_required'])

    def test_ownership_verification_failure_precedes_any_rename(self):
        job=self.submit()
        with patch('muli_sorter.archive_index_cache.recover_index_cached',side_effect=ArchiveError('identity conflict')), patch('muli_sorter.archive_direct_move.rename_noreplace',side_effect=AssertionError('must not move')):
            done=self.service.run_job(job['job_id'])
        self.assertEqual(done['status'],'failed')
        self.assertTrue(all((self.root/'staging'/p).exists() for p in job['file_plans']))
