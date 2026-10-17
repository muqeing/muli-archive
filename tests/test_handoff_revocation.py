"""A withdrawn archive confirmation must say why it was withdrawn.

The copy service only reports "needs_review"; without a local reason the archive
page cannot tell an expired credential from a receipt that went stale because the
batch gained a newer version. The reason is archive-local: the handoff event
itself is sealed and may not carry extra keys.
"""
import time
import unittest
from pathlib import Path
import tempfile

from muli_sorter.archive_handoff import (ArchiveHandoff, REVOCATIONS_NAME,
                                         revocation_reason)


class Service:
    def __init__(self, root):
        self.staging = root / 'staging'
        self.projects = root / 'projects'
        self.state = root / 'state'
        for path in (self.staging, self.projects, self.state):
            path.mkdir(parents=True, exist_ok=True)


class RevocationReasonTests(unittest.TestCase):
    def test_known_codes_get_a_plain_explanation(self):
        self.assertEqual(revocation_reason(ValueError('archive_record_changed')),
                         '归档清单或回执在生成反馈后发生变化')
        self.assertIn('other_code', revocation_reason(ValueError('other_code')))

    def test_reason_text_is_single_line_and_bounded(self):
        text = revocation_reason(ValueError('line one\nline two ' + 'x' * 200))
        self.assertNotIn('\n', text)
        self.assertLessEqual(len(text), 90)


class RevocationStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.service = Service(self.root)
        for name in ('outbox', 'acks'):
            (self.root / name).mkdir()
        self.worker = ArchiveHandoff(self.service, self.root / 'outbox', self.root / 'acks',
                                     '2026-10-01T00:00:00+00:00')

    def tearDown(self):
        self.tmp.cleanup()

    def previous(self):
        return {'batches': [{'binding': {'batch_id': 'BATCH_20261007_000038',
                                         'batch_uid': 'uid-038'}, 'files': []},
                            {'binding': {'batch_id': 'BATCH_20261007_000039',
                                         'batch_uid': 'uid-039'}, 'files': []}]}

    def test_withdrawal_is_recorded_per_batch_and_kept_out_of_the_event(self):
        clock = time.time()
        self.worker.record_revocations('job-1', self.previous(), '归档清单或回执在生成反馈后发生变化', clock)
        stored = self.service.state / REVOCATIONS_NAME
        self.assertTrue(stored.is_file())
        rows = [{'batch_uid': 'uid-038', 'batch_id': 'BATCH_20261007_000038'},
                {'batch_uid': 'uid-other', 'batch_id': 'BATCH_20261009_000099'}]
        revoked = self.worker.revoked_for(rows)
        self.assertEqual(list(revoked), ['uid-038'])
        self.assertEqual(revoked['uid-038']['job_id'], 'job-1')
        self.assertIn('发生变化', revoked['uid-038']['reason'])
        self.assertTrue(revoked['uid-038']['at'])

    def test_a_later_withdrawal_replaces_the_earlier_reason(self):
        self.worker.record_revocations('job-1', self.previous(), 'first', time.time())
        self.worker.record_revocations('job-2', self.previous(), 'second', time.time() + 5)
        revoked = self.worker.revoked_for([{'batch_uid': 'uid-039'}])
        self.assertEqual(revoked['uid-039']['job_id'], 'job-2')
        self.assertEqual(revoked['uid-039']['reason'], 'second')

    def test_missing_or_broken_store_never_raises(self):
        (self.service.state / REVOCATIONS_NAME).write_text('{ not json')
        self.assertEqual(self.worker.revoked_for([{'batch_uid': 'uid-038'}]), {})

    def confirmed_row(self):
        return {'batch_uid':'uid-038','batch_id':'BATCH_20261007_000038','status':'confirmed',
                'total_file_count':5,'confirmed_file_count':5,'missing_file_count':0}

    def test_new_complete_ack_hides_active_warning_but_preserves_audit(self):
        from muli_sorter.handoff_protocol import iso
        now=time.time();self.worker.record_revocations('job-1',self.previous(),'old',now-10)
        p=self.service.state/REVOCATIONS_NAME;before=p.read_bytes()
        self.assertEqual(self.worker.revoked_for([self.confirmed_row()],confirmed_at=iso(now)),{})
        self.assertEqual(p.read_bytes(),before)
        self.assertTrue(self.worker.revoked_for([self.confirmed_row()]))

    def test_partial_counts_or_unknown_status_cannot_hide_warning(self):
        from muli_sorter.handoff_protocol import iso
        now=time.time();self.worker.record_revocations('job-1',self.previous(),'old',now-10)
        for field,value in [('status','partial'),('confirmed_file_count',4),('missing_file_count',1),
                            ('total_file_count',0),('missing_file_count',False),('batch_id','another')]:
            with self.subTest(field=field):
                row=self.confirmed_row();row[field]=value
                self.assertTrue(self.worker.revoked_for([row],confirmed_at=iso(now)))

    def test_old_or_future_ack_cannot_hide_warning(self):
        from muli_sorter.handoff_protocol import iso
        now=time.time();self.worker.record_revocations('job-1',self.previous(),'old',now-10)
        for stamp in (now-20,now+60):
            self.assertTrue(self.worker.revoked_for([self.confirmed_row()],confirmed_at=iso(stamp)))

    def test_new_failure_after_ack_remains_visible(self):
        from muli_sorter.handoff_protocol import iso
        now=time.time();self.worker.record_revocations('job-2',self.previous(),'new',now)
        result=self.worker.revoked_for([self.confirmed_row()],confirmed_at=iso(now-1))
        self.assertEqual(result['uid-038']['job_id'],'job-2')

    def test_invalid_timestamps_keep_warning(self):
        now=time.time();self.worker.record_revocations('job-1',self.previous(),'old',now-10)
        self.assertTrue(self.worker.revoked_for([self.confirmed_row()],confirmed_at='invalid'))

    def test_malformed_per_batch_audit_value_does_not_crash(self):
        import json
        p=self.service.state/REVOCATIONS_NAME;p.write_text(json.dumps({'uid-038':'unknown'}))
        self.assertEqual(self.worker.revoked_for([self.confirmed_row()],confirmed_at='invalid'),{'uid-038':'unknown'})


if __name__ == '__main__':
    unittest.main()
