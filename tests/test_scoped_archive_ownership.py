import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

SRC = Path(__file__).resolve().parents[2] / 'media-preview-flow' / 'src'
sys.path.insert(0, str(SRC))

from muli_sorter.archive import record
from muli_sorter.archive_io import (ArchiveError, atomic_json, directory,
                                    persistent_identity)
from muli_sorter.archive_ownership_cache import (PENDING_NAME, READY_NAME,
                                                  initialize_ready_from_cache,
                                                  recover, reserve_intent,
                                                  reserve_many,
                                                  write_ready)
from muli_sorter.review import digest


class ScopedOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.ctx = directory(self.root)
        self.fd = self.ctx.__enter__()
        atomic_json(self.fd, 'archive-index.json', {})
        write_ready(self.fd, {})

    def tearDown(self):
        self.ctx.__exit__(None, None, None)
        self.tmp.cleanup()

    @staticmethod
    def intent(uid, **extra):
        identity = {'unit_id': uid, 'files': [], **extra}
        return identity, digest(identity)

    def write_job(self, uid, **extra):
        identity, jid = self.intent(uid, **extra)
        atomic_json(self.fd, 'job-' + jid + '.json', {'identity': identity, 'state': 'in_progress'})
        return jid

    def test_scoped_recovery_never_inventories_history(self):
        identity, jid = self.intent('new')
        reserve_intent(self.fd, 'new', jid)
        atomic_json(self.fd, 'job-' + jid + '.json', {'identity': identity, 'state': 'in_progress'})
        with patch('muli_sorter.archive_ownership_cache.inventory', side_effect=AssertionError('full scan')):
            result = recover(self.fd, {'new': jid})
        self.assertEqual(result, {'new': jid})
        self.assertIsNone(record(self.fd, PENDING_NAME))

    def test_selected_existing_record_is_the_only_job_record_opened(self):
        first = self.write_job('first')
        second = self.write_job('second')
        atomic_json(self.fd, 'archive-index.json', {'first': first, 'second': second})
        write_ready(self.fd, {'first': first, 'second': second})
        with patch('muli_sorter.archive_ownership_cache.read_identity', wraps=__import__(
                'muli_sorter.archive_ownership_cache', fromlist=['read_identity']).read_identity) as read:
            recover(self.fd, {'first': first})
        self.assertEqual(read.call_count, 1)

    def test_unreserved_candidate_fails_closed(self):
        jid = self.write_job('new')
        with self.assertRaises(ArchiveError):
            recover(self.fd, {'new': jid})

    def test_crash_after_pending_reservation_before_job_is_fail_closed(self):
        _, jid = self.intent('new')
        reserve_intent(self.fd, 'new', jid)
        with self.assertRaises(ArchiveError):
            recover(self.fd, {'new': jid})

    def test_direct_journal_scope_keeps_unwritten_batch_reservations(self):
        first_identity, first = self.intent('first')
        _, second = self.intent('second')
        reserve_many(self.fd, {'first': first, 'second': second}, {})
        atomic_json(self.fd, 'job-' + first + '.json', {'identity': first_identity, 'state': 'completed'})
        result = recover(self.fd, {'first': first, 'second': second},
                         allow_pending_without_job={'first', 'second'})
        self.assertEqual(result, {'first': first})
        pending = record(self.fd, PENDING_NAME)
        self.assertIn('second', pending['entries'])
        self.assertNotIn('first', pending['entries'])

    def test_two_crashes_during_partial_direct_receipt_recovery(self):
        from muli_sorter.archive_ownership_cache import reserve_many
        a, aj = self.intent('a'); b, bj = self.intent('b')
        scope = {'a': aj, 'b': bj}
        reserve_many(self.fd, scope, {})
        atomic_json(self.fd, 'job-' + aj + '.json', {'identity': a})
        # Crash during recovery after only the first member reaches the index.
        atomic_json(self.fd, 'archive-index.json', {'a': aj})
        result = recover(self.fd, scope, allow_pending_without_job=scope)
        self.assertEqual(result, {'a': aj})
        # Resume the exact journal; then crash before the final marker update.
        reserve_many(self.fd, {'b': bj}, result)
        atomic_json(self.fd, 'job-' + bj + '.json', {'identity': b})
        atomic_json(self.fd, 'archive-index.json', scope)
        self.assertEqual(recover(self.fd, scope, allow_pending_without_job=scope), scope)
        self.assertIsNone(record(self.fd, PENDING_NAME))

    def test_pending_does_not_hide_unrelated_index_change(self):
        identity, jid = self.intent('new')
        reserve_intent(self.fd, 'new', jid)
        atomic_json(self.fd, 'job-' + jid + '.json', {'identity': identity})
        atomic_json(self.fd, 'archive-index.json', {'new': jid, 'unrelated': 'a' * 64})
        with self.assertRaises(ArchiveError):
            recover(self.fd, {'new': jid})

    def test_missing_ready_marker_requires_explicit_maintenance(self):
        os.unlink(self.root / READY_NAME)
        with self.assertRaises(ArchiveError):
            recover(self.fd, {})

    def test_marker_drift_is_reconciled_only_for_scoped_pending_intent(self):
        identity, jid = self.intent('new')
        reserve_intent(self.fd, 'new', jid)
        atomic_json(self.fd, 'job-' + jid + '.json', {'identity': identity, 'state': 'in_progress'})
        # Simulate index durable, marker refresh interrupted.
        atomic_json(self.fd, 'archive-index.json', {'new': jid})
        recover(self.fd, {'new': jid})
        self.assertEqual(record(self.fd, READY_NAME)['index_digest'], digest({'new': jid}))

    def test_legacy_full_cache_can_initialize_marker_without_job_scan(self):
        jid = self.write_job('legacy')
        index = {'legacy': jid}
        atomic_json(self.fd, 'archive-index.json', index)
        cache_data = {'schema': 'ownership-metadata-cache/2',
                      'binding': persistent_identity(self.fd),
                      'entries': {'job-' + jid + '.json': [[1, 2, 3, 4, 5, 1], 'legacy']}}
        atomic_json(self.fd, 'ownership-metadata-cache-v2.json',
                    {**cache_data, 'digest': digest(cache_data)})
        os.unlink(self.root / READY_NAME)
        with patch('muli_sorter.archive_ownership_cache.inventory', side_effect=AssertionError('full scan')):
            self.assertEqual(initialize_ready_from_cache(self.fd), index)
        self.assertEqual(record(self.fd, READY_NAME)['entry_count'], 1)


if __name__ == '__main__':
    unittest.main()
