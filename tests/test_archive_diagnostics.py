import unittest
import time
from unittest.mock import patch
from test_archive_jobs import Fixture
from muli_sorter.archive_diagnostics import diagnostic_context, diagnostic_issues
from muli_sorter.archive_console import job_summary
from muli_sorter.archive_io import ArchiveError
from muli_sorter.archive_submission import Submissions
from muli_sorter.archive_io import atomic_json


class DiagnosticsTests(Fixture, unittest.TestCase):
    def confirmed_unit(self):
        segment = next(s for s in self.decisions['segments'] if s['decision'] == 'confirmed')
        unit = next(u for u in self.model['units'] if u['unit_id'] == segment['unit_ids'][0])
        return segment, unit

    def test_source_failure_names_exact_file_and_segment(self):
        segment, unit = self.confirmed_unit()
        file = unit['files'][-1]
        source = self.root/'staging'/file['source_path']
        source.write_bytes(source.read_bytes()+b'changed')
        result = self.service.preflight(self.decisions)
        self.assertEqual(result['status'], 'blocked', result)
        issue = result['issues'][0]
        self.assertEqual(issue['scope'], 'file')
        self.assertEqual(issue['segments'][0]['segment_id'], segment['segment_id'])
        self.assertEqual(issue['units'][0]['unit_id'], unit['unit_id'])
        self.assertEqual(issue['files'], [{'name':file['name'], 'source_path':file['source_path']}])
        self.assertFalse(list((self.root/'console-state/requests').glob('job-*.json')))

    def test_snapshot_mismatch_is_not_false_bad_files(self):
        self.decisions['report_id'] = 'sha256:'+'0'*64
        with patch.object(self.service, '_verify', side_effect=AssertionError('no media reads')):
            result = self.service.preflight(self.decisions)
        issue = result['issues'][0]
        self.assertEqual(issue['scope'], 'plan')
        self.assertEqual(issue['files'], [])
        self.assertEqual(issue['segments'], [])
        self.assertEqual(issue['report_id'], self.decisions['report_id'])
        self.assertEqual(issue['current_report_id'], self.model['report_id'])
        self.assertIn('尚未核实', issue['guidance'])

    def test_invalid_project_names_segment(self):
        segment, unit = self.confirmed_unit()
        segment['project_id'] = 'missing-project'
        issue = self.service.preflight(self.decisions)['issues'][0]
        self.assertEqual(issue['scope'], 'segment')
        self.assertEqual(issue['segments'][0]['segment_id'], segment['segment_id'])
        self.assertTrue(issue['files'])

    def test_nested_context_preserves_error_and_exact_file(self):
        segment, unit = self.confirmed_unit()
        file = unit['files'][0]
        failure = ArchiveError('synthetic failure')
        try:
            with diagnostic_context(unit_id=unit['unit_id']):
                with diagnostic_context(source_path=file['source_path']):
                    raise failure
        except ArchiveError as exc:
            self.assertIs(exc, failure)
            self.assertEqual(diagnostic_issues(exc, self.model, self.decisions)[0]['scope'], 'file')
        self.assertEqual(str(failure), 'synthetic failure')
        self.assertEqual(diagnostic_issues(ArchiveError('unrelated'))[0]['scope'], 'plan')

    def test_malformed_plan_does_not_mask_original_error(self):
        for plan in (None, [], {'segments':None}, {'segments':[{'unit_ids':[{}]}]}):
            self.assertEqual(diagnostic_issues(ArchiveError('bad'), self.model, plan)[0]['reason'], 'bad')

    def test_source_identity_difference_survives_file_location_context(self):
        segment, unit = self.confirmed_unit()
        file = unit['files'][0]
        failed = ArchiveError('source identity changed')
        differences = [{'field':'mtime_ns', 'expected':10, 'actual':20}]
        failed.archive_location = {'changed_signature_fields':differences}
        try:
            with diagnostic_context(source_path=file['source_path']):
                raise failed
        except ArchiveError as exc:
            issue = diagnostic_issues(exc,self.model,self.decisions)[0]
        self.assertEqual(issue['changed_signature_fields'],differences)
        self.assertEqual(issue['segments'][0]['segment_id'],segment['segment_id'])

    def test_move_failure_persists_file_and_recovery_clears_issue(self):
        self.service.move_enabled = True
        self.service.direct_move_view = {'root':str(self.root), 'staging':'staging', 'projects':'projects'}
        self.decisions['archive_options'] = {'mode':'move', 'existing':'skip_identical'}
        job = self.submit()
        def fail(phase, row):
            if phase == 'direct_move_renamed':
                raise ArchiveError('synthetic interrupted rename')
        self.service.checkpoint = fail
        done = self.service.run_job(job['job_id'])
        self.assertEqual(done['status'], 'failed', done)
        issue = job_summary(done)['issues'][0]
        self.assertEqual(issue['scope'], 'file')
        self.assertTrue(issue['files'][0]['source_path'])
        self.assertTrue(issue['segments'])
        self.assertTrue(issue['target_path'])
        self.service.checkpoint = lambda *args: None
        completed = self.service.run_job(job['job_id'])
        self.assertEqual(completed['status'], 'completed', completed)
        self.assertEqual(job_summary(completed)['issues'], [])

    def test_submit_reservation_failure_survives_durable_acknowledgement(self):
        preview = self.service.preflight(self.decisions)
        self.assertEqual(preview['status'], 'ready', preview)
        token = preview['preview_id']
        ticket = self.service.previews.get(token)
        file = next(iter(ticket['prepared']['file_plans'].values()))
        atomic_json(self.service.state_fd, 'reservations.json', {
            file['source_path']:{'target_path':'other/project/file', 'blake3':file['blake3']}})
        submissions = Submissions(self.service)
        try:
            submissions.start(token, self.decisions, True)
            deadline = time.monotonic()+5
            while submissions.active and time.monotonic() < deadline:
                time.sleep(.01)
            result = submissions.get(token)
            self.assertEqual(result['status'], 'rejected', result)
            self.assertEqual(result['issues'][0]['scope'], 'file')
            self.assertEqual(result['issues'][0]['files'][0]['source_path'], file['source_path'])
            self.assertTrue(result['issues'][0]['segments'])
            self.assertFalse(list((self.root/'console-state/requests').glob('job-*.json')))
        finally:
            submissions.close()
