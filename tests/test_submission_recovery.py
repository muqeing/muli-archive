"""Crash-point recovery uses only the originally confirmed immutable request."""
from copy import deepcopy
import json
import time
import unittest
from unittest.mock import patch
from urllib.request import Request, build_opener, ProxyHandler
from urllib.error import HTTPError
import threading

from test_archive_jobs import Fixture
from muli_sorter.archive_io import ArchiveError, directory, atomic_json
from muli_sorter.archive_submission import Submissions
from muli_sorter.archive_console import make_server
from muli_sorter.order_feed_io import read_json, atomic_json as save_json


class RecoveryTests(Fixture, unittest.TestCase):
    def interrupted(self, direct=False):
        if direct:
            self.service.move_enabled = True
            self.service.direct_move_view = {'root': str(self.root), 'staging': 'staging', 'projects': 'projects'}
            self.decisions['archive_options'] = {'mode': 'move', 'existing': 'skip_identical'}
        preview = self.service.preflight(self.decisions)
        self.assertEqual(preview['status'], 'ready', preview)
        token = preview['preview_id']
        submissions = Submissions(self.service)
        # Exact production crash point: request saved, reservations/job absent.
        with patch('muli_sorter.archive_admission.atomic_json', side_effect=ArchiveError('synthetic interruption')):
            submissions.start(token, self.decisions, True)
            submissions.close()
        self.assertEqual(submissions.get(token)['status'], 'unknown')
        row = read_json(submissions.path(token))
        self.jid = row['expected_job_id']
        self.request_path = self.service.state/'requests'/('request-'+self.jid+'.json')
        self.assertTrue(self.request_path.exists())
        self.assertFalse(self.service._job_path(self.jid).exists())
        return token

    def recover_and_wait(self, token):
        submissions = Submissions(self.service)
        submissions.recover(token, True)
        submissions.close()
        return submissions.get(token)

    def test_copy_recovery_preserves_id_request_and_sources_after_restart_and_expiry(self):
        token = self.interrupted()
        before = self.request_path.read_bytes()
        self.service.close(); self.service = self.make_service()
        with patch('muli_sorter.archive_admission.time.time', return_value=time.time()+3600), \
             patch.object(self.service, '_prepare', side_effect=AssertionError('repeated preflight')), \
             patch.object(self.service, 'model', side_effect=AssertionError('page reload')):
            result = self.recover_and_wait(token)
        self.assertEqual(result['status'], 'accepted', result)
        self.assertEqual(result['job_id'], self.jid)
        self.assertEqual(before, self.request_path.read_bytes())
        done = self.service.run_job(self.jid)
        self.assertEqual(done['status'], 'completed', done)
        self.assertEqual(done['summary']['completed_files'], 4)
        self.assertTrue(all((self.root/'staging'/p).is_file() for p in done['file_plans']))
        again = self.recover_and_wait(token)
        self.assertEqual(again['job_id'], self.jid)
        self.assertEqual(len(self.service.list_jobs()), 1)

    def test_move_recovery_executes_original_plan_once(self):
        token = self.interrupted(direct=True)
        before = self.request_path.read_bytes()
        result = self.recover_and_wait(token)
        self.assertEqual(result['status'], 'accepted', result)
        done = self.service.run_job(self.jid)
        self.assertEqual(done['status'], 'completed', done)
        self.assertEqual(done['summary']['direct_moved_files'], 4)
        self.assertEqual(before, self.request_path.read_bytes())
        self.assertEqual(self.service.run_job(self.jid), done)
        self.assertEqual(self.recover_and_wait(token)['job_id'], self.jid)

    def test_recovery_does_not_bypass_source_checks(self):
        token = self.interrupted(direct=True)
        request = read_json(self.request_path)
        source = self.root/'staging'/next(iter(request['file_plans']))
        source.write_bytes(b'changed')
        self.assertEqual(self.recover_and_wait(token)['status'], 'accepted')
        done = self.service.run_job(self.jid)
        self.assertEqual(done['status'], 'failed', done)
        self.assertEqual(done['summary']['direct_moved_files'], 0)
        self.assertTrue(source.exists())

    def test_tampered_request_cannot_be_recovered(self):
        token = self.interrupted()
        value = read_json(self.request_path)
        next(iter(value['file_plans'].values()))['target_path'] = 'foreign/changed.JPG'
        save_json(self.request_path, value)
        result = self.recover_and_wait(token)
        self.assertEqual(result['status'], 'unknown')
        self.assertIn('原确认计划', result['error'])
        self.assertFalse(self.service._job_path(self.jid).exists())

    def test_reservation_conflict_keeps_original_task_and_does_not_publish(self):
        token = self.interrupted()
        path = next(iter(read_json(self.request_path)['file_plans']))
        with directory(self.service.state) as fd:
            atomic_json(fd, 'reservations.json', {path: {'target_path': 'different', 'blake3': 'a'*64}})
        before = (self.service.state/'reservations.json').read_bytes()
        result = self.recover_and_wait(token)
        self.assertEqual(result['status'], 'unknown')
        self.assertIn('另一份归档计划', result['error'])
        self.assertFalse(self.service._job_path(self.jid).exists())
        self.assertEqual(before, (self.service.state/'reservations.json').read_bytes())

    def test_existing_execution_record_is_not_rebuilt(self):
        token = self.interrupted()
        (self.service.state/'requests'/('direct-entry-'+self.jid+'-'+'a'*64+'.json')).write_text('{}')
        result = self.recover_and_wait(token)
        self.assertEqual(result['status'], 'unknown')
        self.assertIn('已有移动执行记录', result['error'])
        self.assertFalse(self.service._job_path(self.jid).exists())

    def test_missing_ticket_is_not_recovered(self):
        token = self.interrupted()
        self.service.previews.pop(token)
        (self.service.state/'confirmation-tickets'/(token+'.json')).unlink()
        result = self.recover_and_wait(token)
        self.assertEqual(result['status'], 'unknown')
        self.assertFalse(self.service._job_path(self.jid).exists())

    def test_get_and_ordinary_submit_never_auto_recover(self):
        token = self.interrupted()
        submissions = Submissions(self.service)
        row = read_json(submissions.path(token)); row['status'] = 'checking';save_json(submissions.path(token), row)
        with patch('muli_sorter.archive_admission.recover', side_effect=AssertionError('automatic recovery')):
            result = submissions.get(token)
            self.assertTrue(result['recovery_available'])
            self.assertEqual(submissions.start(token, self.decisions, True)['status'], 'unknown')
        self.assertFalse(self.service._job_path(self.jid).exists())
        with self.assertRaises(ArchiveError): submissions.recover(token, False)
        submissions.close()

    def test_http_requires_origin_and_explicit_confirmation(self):
        token = self.interrupted()
        server = make_server(self.service, self.root)
        thread = threading.Thread(target=server.serve_forever, daemon=True);thread.start()
        opener = build_opener(ProxyHandler({}))
        url = server.public_origin+'/api/submissions/'+token+'/recover'
        try:
            req = Request(url, data=b'{"confirmed":true}', headers={'Content-Type': 'application/json'})
            with self.assertRaises(HTTPError) as result: opener.open(req, timeout=5)
            self.assertEqual(result.exception.code, 403)
            req = Request(url, data=b'{"confirmed":false}', headers={'Content-Type': 'application/json', 'Origin': server.public_origin, 'X-Muli-Request': '1'})
            with self.assertRaises(HTTPError) as result: opener.open(req, timeout=5)
            self.assertEqual(result.exception.code, 409)
            req = Request(url, data=b'{"confirmed":true}', headers={'Content-Type': 'application/json', 'Origin': server.public_origin, 'X-Muli-Request': '1'})
            with opener.open(req, timeout=5) as response: value = json.load(response)
            self.assertIn(value['status'], ('checking', 'accepted'))
        finally:
            server.shutdown();server.server_close();thread.join(3)
        self.assertTrue(self.service._job_path(self.jid).exists())


if __name__ == '__main__': unittest.main()
