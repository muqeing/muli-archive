"""Regression coverage for target ctime drift in archive feedback."""
import json
import os
import stat
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from test_archive_jobs import Fixture
from muli_sorter import handoff_protocol as wire
from muli_sorter.archive_handoff import ArchiveHandoff
from muli_sorter.archive_layout import studio_target_rows
from muli_sorter.archive_target_metadata import MetadataProofCache


class TargetMetadataHandoffTests(Fixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.decisions['archive_options'] = {
            'mode': 'copy', 'existing': 'skip_identical',
        }
        self.outbox = self.root / 'feedback-out'
        self.acks = self.root / 'feedback-ack'
        self.outbox.mkdir()
        self.acks.mkdir()
        self.producer = ArchiveHandoff(
            self.service, self.outbox, self.acks, '2020-01-01T00:00:00+00:00')

    def tearDown(self):
        self.producer.close()
        super().tearDown()

    def complete(self):
        job = self.submit()
        done = self.service.run_job(job['job_id'])
        self.assertEqual(done['status'], 'completed', done)
        return done

    def receipt_rows(self, done):
        rows = []
        for outcome in done['outcomes']:
            path = self.service.state / 'units' / outcome['receipt']
            receipt = json.loads(path.read_text())
            rows.extend((path, receipt, row) for row in receipt['files'])
        return rows

    def build_with_hash_bytes(self, job_id):
        """Count bytes read by check_target without changing its result."""
        import muli_sorter.archive_handoff as handoff

        original = handoff.check_target
        read_bytes = {}

        def counted(fd, row, expected, *args, **kwargs):
            total = [0]

            def progress(amount):
                total[0] += amount

            kwargs['progress'] = progress
            result = original(fd, row, expected, *args, **kwargs)
            read_bytes[row['target_path']] = read_bytes.get(row['target_path'], 0) + total[0]
            return result

        clock = time.time()
        with patch.object(handoff, 'check_target', side_effect=counted):
            event = wire.seal({**self.producer.build(job_id, clock=clock), 'sequence': 1})
        wire.validate_event(event, clock)
        return event, read_bytes

    def assert_event_keeps_receipt_signatures(self, event, rows):
        receipt_signatures = {row['target_path']: row['target_signature'] for _, _, row in rows}
        event_rows = [row for batch in event['batches'] for row in batch['files']]
        self.assertTrue(event_rows)
        self.assertTrue(all(
            row['target_signature'] == receipt_signatures[row['target_path']]
            for row in event_rows))
        self.assertEqual(event, wire.seal(event))

    def test_copy_ctime_drift_hashes_only_changed_target_and_reuses_proof(self):
        done = self.complete()
        rows = self.receipt_rows(done)
        receipt_bytes = {path: path.read_bytes() for path, _, _ in rows}
        target = self.root / 'projects' / rows[0][2]['target_path']

        initial, initial_reads = self.build_with_hash_bytes(done['job_id'])
        self.assertFalse(any(initial_reads.values()))
        self.assert_event_keeps_receipt_signatures(initial, rows)

        original_mode = stat.S_IMODE(target.stat().st_mode)
        os.chmod(target, 0o640 if original_mode != 0o640 else 0o600)
        changed, changed_reads = self.build_with_hash_bytes(done['job_id'])
        self.assertGreater(changed_reads.get(rows[0][2]['target_path'], 0), 0)
        self.assertEqual(
            [path for path, amount in changed_reads.items() if amount > 0],
            [rows[0][2]['target_path']])
        self.assert_event_keeps_receipt_signatures(changed, rows)

        cached, cached_reads = self.build_with_hash_bytes(done['job_id'])
        self.assertFalse(any(cached_reads.values()))
        self.assert_event_keeps_receipt_signatures(cached, rows)

        os.chmod(target, original_mode)
        rechecked, rechecked_reads = self.build_with_hash_bytes(done['job_id'])
        self.assertGreater(rechecked_reads.get(rows[0][2]['target_path'], 0), 0)
        self.assertEqual(
            [path for path, amount in rechecked_reads.items() if amount > 0],
            [rows[0][2]['target_path']])
        self.assert_event_keeps_receipt_signatures(rechecked, rows)
        for path, data in receipt_bytes.items():
            self.assertEqual(path.read_bytes(), data)

    def test_metadata_proof_cache_is_bounded(self):
        cache = MetadataProofCache(limit=1)
        cache.remember(('first',))
        cache.remember(('second',))
        self.assertFalse(cache.contains(('first',)))
        self.assertTrue(cache.contains(('second',)))
        self.assertEqual(len(cache.values), 1)

    def test_same_size_content_tamper_with_restored_mtime_is_rejected(self):
        done = self.complete()
        _, _, row = self.receipt_rows(done)[0]
        target = self.root / 'projects' / row['target_path']
        before = target.stat()
        data = target.read_bytes()
        target.write_bytes(bytes([data[0] ^ 1]) + data[1:])
        os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))
        self.assertEqual(target.stat().st_mtime_ns, before.st_mtime_ns)
        with self.assertRaises(ValueError):
            self.producer.build(done['job_id'])

    def replace_target_and_reject(self, replacement):
        done = self.complete()
        _, _, row = self.receipt_rows(done)[0]
        target = self.root / 'projects' / row['target_path']
        source = self.root / 'staging' / row['source_path']
        original = target.read_bytes()
        target.unlink()
        replacement(target, source, original)
        with self.assertRaises((ValueError, OSError)):
            self.producer.build(done['job_id'])

    def test_inode_replacement_is_rejected(self):
        def replace(target, source, original):
            target.write_bytes(original)

        self.replace_target_and_reject(replace)

    def test_hardlink_target_is_rejected(self):
        def replace(target, source, original):
            os.link(source, target)

        self.replace_target_and_reject(replace)

    def test_symlink_target_is_rejected(self):
        def replace(target, source, original):
            target.symlink_to(source)

        self.replace_target_and_reject(replace)

    def test_mixed_direct_feedback_accepts_ctime_drift_and_preserves_receipts(self):
        self.service.move_enabled = True
        self.service.direct_move_view = {
            'root': str(self.root), 'staging': 'staging', 'projects': 'projects',
        }
        self.decisions['archive_options'] = {
            'mode': 'move', 'existing': 'skip_identical',
        }
        segment = next(item for item in self.decisions['segments'] if item['decision'] == 'confirmed')
        unit = next(item for item in self.model['units'] if item['unit_id'] == segment['unit_ids'][0])
        project = next(item for item in self.model['projects'] if item['project_id'] == segment['project_id'])
        planned = studio_target_rows(unit, project)[0]
        source = self.root / 'staging' / planned['source_path']
        target = self.root / 'projects' / planned['target_path']
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())

        done = self.complete()
        self.assertEqual(done['summary']['skipped_files'], 1)
        self.assertEqual(done['summary']['direct_moved_files'], 3)
        rows = self.receipt_rows(done)
        receipt_bytes = {path: path.read_bytes() for path, _, _ in rows}
        os.chmod(target, 0o640 if stat.S_IMODE(target.stat().st_mode) != 0o640 else 0o600)
        event, reads = self.build_with_hash_bytes(done['job_id'])
        self.assertGreater(reads.get(planned['target_path'], 0), 0)
        self.assert_event_keeps_receipt_signatures(event, rows)
        for path, data in receipt_bytes.items():
            self.assertEqual(path.read_bytes(), data)


if __name__ == '__main__':
    unittest.main()
