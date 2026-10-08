"""Focused recovery tests for mixed same-volume rename and duplicate cleanup."""
import unittest
from unittest.mock import patch

from test_archive_jobs import Fixture
from muli_sorter.archive_layout import studio_target_rows
from muli_sorter.archive_rename_io import STRATEGY


class MixedDirectMoveTests(Fixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.service.move_enabled = True
        self.service.direct_move_view = {
            'root': str(self.root), 'staging': 'staging', 'projects': 'projects'}
        self.decisions['archive_options'] = {
            'mode': 'move', 'existing': 'skip_identical'}

    def _first_target(self):
        segment = next(s for s in self.decisions['segments']
                       if s['decision'] == 'confirmed')
        unit = next(u for u in self.model['units']
                    if u['unit_id'] == segment['unit_ids'][0])
        project = next(p for p in self.model['projects']
                       if p['project_id'] == segment['project_id'])
        row = studio_target_rows(unit, project)[0]
        return (self.root / 'staging' / row['source_path'],
                self.root / 'projects' / row['target_path'])

    def _submit_with_existing_first(self):
        source, target = self._first_target()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())
        preview = self.service.preflight(self.decisions)
        self.assertEqual(preview['status'], 'ready', preview)
        job = self.service.submit(preview['preview_id'], self.decisions, True)
        self.assertEqual(job['execution_strategy'], STRATEGY)
        return job, source, target

    def _restart(self):
        self.service.close()
        self.service = self.make_service(
            move_enabled=True,
            direct_move_view={'root': str(self.root), 'staging': 'staging',
                              'projects': 'projects'})

    def test_shared_target_renames_first_source_and_skips_second(self):
        from muli_sorter import archive_jobs
        original = archive_jobs.studio_target_rows

        def alias_same_target(unit, project):
            rows = original(unit, project)
            main = next((row for row in rows if row['target_name'] == 'B.MP4'), None)
            if main:
                for row in rows:
                    if row['target_name'] == 'B.LRF':
                        row['target_path'] = main['target_path']
                        row['target_name'] = main['target_name']
            return rows

        with patch.object(archive_jobs, 'studio_target_rows', side_effect=alias_same_target):
            preview = self.service.preflight(self.decisions)
            self.assertEqual(preview['status'], 'ready', preview)
            job = self.service.submit(preview['preview_id'], self.decisions, True)
            done = self.service.run_job(job['job_id'])
        self.assertEqual(done['status'], 'completed', done)
        self.assertEqual(done['summary']['direct_moved_files'], 3)
        self.assertEqual(done['summary']['skipped_files'], 1)
        self.assertEqual(done['summary']['copy_files'], 0)

    def test_target_replaced_before_cleanup_barrier_preserves_duplicate_source(self):
        job, source, target = self._submit_with_existing_first()
        def checkpoint(phase, row):
            if phase == 'direct_move_renamed':
                target.write_bytes(b'changed target')

        self.service.checkpoint = checkpoint
        done = self.service.run_job(job['job_id'])
        self.assertEqual(done['status'], 'failed', done)
        self.assertTrue(source.exists())
        self.assertEqual(target.read_bytes(), b'changed target')

    def test_target_replaced_after_unlink_intent_preserves_duplicate_source(self):
        job, source, target = self._submit_with_existing_first()
        source_path = str(source.relative_to(self.root / 'staging'))

        def checkpoint(phase, row):
            if phase == 'direct_move_intent' and row['source_path'] == source_path:
                target.write_bytes(b'changed target')

        self.service.checkpoint = checkpoint
        done = self.service.run_job(job['job_id'])
        self.assertEqual(done['status'], 'failed', done)
        self.assertTrue(source.exists())

    def test_crash_after_duplicate_unlink_recovers_deleted_state(self):
        job, source, _ = self._submit_with_existing_first()
        source_path = str(source.relative_to(self.root / 'staging'))
        fired = []

        def checkpoint(phase, row):
            if phase == 'direct_move_removed' and row['source_path'] == source_path and not fired:
                fired.append(True)
                raise RuntimeError('simulated process exit')

        self.service.checkpoint = checkpoint
        with self.assertRaises(RuntimeError):
            self.service.run_job(job['job_id'])
        self.assertFalse(source.exists())
        self._restart()
        done = self.service.run_job(job['job_id'])
        self.assertEqual(done['status'], 'completed', done)
        self.assertEqual(done['summary']['skipped_files'], 1)
        self.assertFalse(source.exists())

    def test_identical_target_checks_are_linear(self):
        from muli_sorter.archive_direct_move import require_identical_target
        job, source, _ = self._submit_with_existing_first()
        with patch('muli_sorter.archive_direct_move.require_identical_target',
                   wraps=require_identical_target) as checked:
            done = self.service.run_job(job['job_id'])
        self.assertEqual(done['status'], 'completed', done)
        # One existing-target check during preparation, one barrier check and
        # one intent-window check. There is no per-file full-batch rescan.
        self.assertEqual(checked.call_count, 3)
        self.assertFalse(source.exists())


if __name__ == '__main__':
    unittest.main()
