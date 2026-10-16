"""Run the established archive contract against scoped reservation storage."""
from unittest.mock import patch
import unittest
import test_archive_jobs as baseline
from muli_sorter.archive_io import ArchiveError
from muli_sorter.archive_admission import recover
from muli_sorter.order_feed_io import read_json
from muli_sorter.archive_submission import Submissions


class IndexedJobTests(baseline.JobTests):
    def make_service(self, **kwargs):
        kwargs.setdefault('indexed_reservations', True)
        return super().make_service(**kwargs)

    def test_submit_never_reads_or_rewrites_legacy_table_after_migration(self):
        with patch('muli_sorter.archive_reservations.read_bytes', side_effect=AssertionError('full history read')):
            job = self.submit()
            done = self.service.run_job(job['job_id'])
        self.assertEqual(done['status'], 'completed', done)
        self.assertFalse((self.service.state/'reservations.json').exists())
        self.assertEqual(len(self.service.reservations.subset(job['file_plans'])), 4)

    def test_interrupted_reservation_transaction_recovers_original_request(self):
        preview = self.service.preflight(self.decisions)
        token = preview['preview_id']
        submissions = Submissions(self.service)
        with patch.object(self.service.reservations, 'reserve', side_effect=ArchiveError('synthetic interruption')):
            submissions.start(token, self.decisions, True)
            submissions.close()
        row = read_json(submissions.path(token))
        self.assertEqual(row['status'], 'unknown')
        request_path = self.service.state/'requests'/('request-'+row['expected_job_id']+'.json')
        original = request_path.read_bytes()
        self.service.close(); self.service = self.make_service()
        with patch.object(self.service, '_prepare', side_effect=AssertionError('second preflight')):
            job = recover(self.service, token, row)
        self.assertEqual(job['job_id'], row['expected_job_id'])
        self.assertEqual(request_path.read_bytes(), original)
        self.assertEqual(self.service.run_job(job['job_id'])['status'], 'completed')

    def test_old_json_mode_cannot_ignore_new_reservations(self):
        self.submit(); self.service.close()
        with self.assertRaisesRegex(ArchiveError, '禁止回退'):
            super().make_service(indexed_reservations=False)
        self.service = self.make_service()
