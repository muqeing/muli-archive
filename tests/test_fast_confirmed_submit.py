import json
import time
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import build_opener, ProxyHandler

from test_archive_jobs import Fixture
from muli_sorter.archive_io import ArchiveError


class FastConfirmedSubmitTests(Fixture, unittest.TestCase):
    def direct(self):
        self.service.move_enabled = True
        self.service.direct_move_view = {'root':str(self.root),'staging':'staging','projects':'projects'}
        self.decisions['archive_options'] = {'mode':'move','existing':'skip_identical'}

    def test_submit_persists_exact_plan_without_preflight_or_media_or_model(self):
        ready = self.service.preflight(self.decisions)
        with patch.object(self.service,'_prepare',side_effect=AssertionError('second preflight')), \
             patch.object(self.service,'model',side_effect=AssertionError('reload model')), \
             patch.object(self.service,'runtime',side_effect=AssertionError('runtime scan')):
            job = self.service.submit(ready['preview_id'],self.decisions,True)
        self.assertEqual(job['status'],'queued')
        request=json.loads((self.service.state/'requests'/('request-'+job['job_id']+'.json')).read_text())
        self.assertEqual(request['admission_policy'],'confirmed-plan/v1')
        self.assertEqual(request['file_plans'],job['file_plans'])

    def test_confirmation_survives_restart_and_keeps_same_expiry(self):
        ready=self.service.preflight(self.decisions)
        self.service.close();self.service=self.make_service()
        self.assertEqual(self.service.previews.get(ready['preview_id'])['expires_at'],ready['expires_at'])
        with patch.object(self.service,'model',side_effect=AssertionError('reload model')):
            self.assertEqual(self.service.confirmation(ready['preview_id']),ready)
        with patch.object(self.service,'_prepare',side_effect=AssertionError('repeat')):
            job=self.service.submit(ready['preview_id'],self.decisions,True)
        self.assertEqual(job['status'],'queued')

    def test_direct_executor_does_not_repeat_whole_preflight(self):
        self.direct();ready=self.service.preflight(self.decisions)
        job=self.service.submit(ready['preview_id'],self.decisions,True)
        with patch.object(self.service,'_prepare',side_effect=AssertionError('third preflight')):
            done=self.service.run_job(job['job_id'])
        self.assertEqual(done['status'],'completed',done)
        self.assertEqual(done['summary']['copy_files'],0)

    def test_source_change_after_confirmation_is_stopped_before_move(self):
        self.direct();ready=self.service.preflight(self.decisions)
        p=self.root/'staging'/self.model['units'][0]['files'][0]['source_path']
        p.write_bytes(b'changed')
        job=self.service.submit(ready['preview_id'],self.decisions,True)
        done=self.service.run_job(job['job_id'])
        self.assertEqual(done['status'],'failed',done)
        self.assertEqual(done['summary']['direct_moved_files'],0)
        self.assertTrue(all((self.root/'staging'/p).exists() for p in job['file_plans']))

    def test_directory_conflict_is_stopped_without_overwriting(self):
        self.direct();self.manual();ready=self.service.preflight(self.decisions)
        p=self.root/'projects/2026/7月/20260709_自建_合成新项目'
        p.mkdir();(p/'foreign').write_bytes(b'keep')
        job=self.service.submit(ready['preview_id'],self.decisions,True)
        done=self.service.run_job(job['job_id'])
        self.assertEqual(done['status'],'failed',done)
        self.assertEqual((p/'foreign').read_bytes(),b'keep')
        self.assertEqual(done['summary']['direct_moved_files'],0)

    def test_prepared_source_metadata_is_reused_after_restart(self):
        compiled=self.service._compile(self.model,self.decisions)
        for item in compiled[1]:self.service._verify(item['unit'],{})
        self.service.close();self.service=self.make_service()
        with patch('muli_sorter.archive_source.media_stat',side_effect=AssertionError('media reopened')):
            ready=self.service.preflight(self.decisions)
        self.assertEqual(ready['status'],'ready',ready)

    def test_replaced_target_subdirectory_stops_before_any_rename(self):
        self.direct()
        selected=self.service._compile(self.model,self.decisions)[1]
        from pathlib import PurePosixPath
        target=self.root/'projects'/str(PurePosixPath(selected[0]['rows'][0]['target_path']).parent)
        target.mkdir(parents=True,exist_ok=True)
        ready=self.service.preflight(self.decisions)
        target.rename(target.with_name(target.name+'-original'))
        target.mkdir()
        job=self.service.submit(ready['preview_id'],self.decisions,True)
        done=self.service.run_job(job['job_id'])
        self.assertEqual(done['status'],'failed',done)
        self.assertEqual(done['summary']['direct_moved_files'],0)
        self.assertIn('目标子目录',done['errors'][0])

    def test_new_case_alias_stops_before_any_rename(self):
        self.direct()
        selected=self.service._compile(self.model,self.decisions)[1]
        target=self.root/'projects'/selected[0]['rows'][0]['target_path']
        target.parent.mkdir(parents=True,exist_ok=True)
        ready=self.service.preflight(self.decisions)
        alias=target.with_name(target.name.swapcase());alias.write_bytes(b'foreign')
        job=self.service.submit(ready['preview_id'],self.decisions,True)
        done=self.service.run_job(job['job_id'])
        self.assertEqual(done['status'],'failed',done)
        self.assertEqual(done['summary']['direct_moved_files'],0)
        self.assertEqual(alias.read_bytes(),b'foreign')

    def test_bad_background_unit_does_not_kill_preparation(self):
        self.service.source_preparation.schedule([{}])
        until=time.monotonic()+2
        while time.monotonic()<until and self.service.source_preparation.last_error is None:
            time.sleep(.01)
        self.assertIsNotNone(self.service.source_preparation.last_error)
        self.assertTrue(self.service.source_preparation.thread.is_alive())
        self.test_background_preparation_finishes_without_submission()

    def test_corrupt_preparation_database_falls_back_to_selected_sources(self):
        self.service.close()
        (self.root/'console-state/source-preparation-v1.sqlite3').write_bytes(b'broken cache')
        self.service=self.make_service()
        self.assertIsNone(self.service.source_preparation.db)
        ready=self.service.preflight(self.decisions)
        self.assertEqual(ready['status'],'ready',ready)
        self.assertEqual(self.service.list_jobs(),[])

    def test_confirmation_read_rejects_expiry_without_renewing_or_submitting(self):
        ready=self.service.preflight(self.decisions)
        self.service.previews[ready['preview_id']]['expires_at']=0
        with self.assertRaises(ArchiveError):self.service.confirmation(ready['preview_id'])
        self.assertEqual(self.service.list_jobs(),[])

    def test_confirmation_http_reads_one_ticket_without_preflight_or_submission(self):
        from muli_sorter.archive_console import make_server
        ready=self.service.preflight(self.decisions)
        server=make_server(self.service,self.root)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        url=server.public_origin+'/api/confirmation-tickets/'+ready['preview_id']
        opener=build_opener(ProxyHandler({}))
        try:
            with patch.object(self.service,'model',side_effect=AssertionError('model')), \
                 patch.object(self.service,'_prepare',side_effect=AssertionError('preflight')):
                with opener.open(url,timeout=5) as response:
                    self.assertEqual(json.load(response),ready)
                self.service.previews[ready['preview_id']]['expires_at']=0
                with self.assertRaises(HTTPError) as result:opener.open(url,timeout=5)
                self.assertEqual(result.exception.code,409)
            self.assertEqual(self.service.list_jobs(),[])
        finally:
            server.shutdown();server.server_close();thread.join(3)

    def test_background_preparation_finishes_without_submission(self):
        self.service.source_preparation.schedule(self.model['units'])
        until=time.monotonic()+5
        count=0
        while time.monotonic()<until:
            with self.service.source_preparation.lock:
                count=self.service.source_preparation.db.execute('SELECT count(*) FROM prepared').fetchone()[0]
            if count>=3:break
            time.sleep(.01)
        self.assertGreaterEqual(count,3)
        self.assertEqual(self.service.list_jobs(),[])
        self.assertTrue(all((self.root/'staging'/f['source_path']).exists() for u in self.model['units'] for f in u['files']))


if __name__=='__main__':unittest.main()
