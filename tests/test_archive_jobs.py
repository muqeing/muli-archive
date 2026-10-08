from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from muli_sorter.archive_fixture import prepare, save_manifest
from muli_sorter.archive_jobs import ArchiveJobs
from muli_sorter.archive_console import make_server
from muli_sorter.archive_io import ArchiveError
from muli_sorter.archive_layout import STANDARD_FOLDERS
from muli_sorter.review import digest


class Fixture:
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.root=Path(self.tmp.name).resolve()/'fixture'
        self.model,self.decisions=prepare(self.root)
        from muli_sorter.staging_coordination import staging_guard
        with staging_guard(self.root/'staging', create=True):
            pass  # Real Ingest initializes coordination before any completed batch.
        self.service=self.make_service()

    def tearDown(self):
        self.service.close()
        self.tmp.cleanup()

    def runtime(self):
        data=json.loads((self.root/'runtime-state.json').read_text())
        data['generated_at']=datetime.now(timezone.utc).isoformat()
        return data

    def make_service(self,**kwargs):
        return ArchiveJobs(self.root/'staging',self.root/'projects',self.root/'console-state',
                           lambda:self.model,self.runtime,enabled=True,**kwargs)

    def submit(self):
        before=self.service.preflight(self.decisions)
        self.assertEqual(before['status'],'ready',before)
        return self.service.submit(before['preview_id'],self.decisions,True)

    def manual(self):
        self.decisions['schema_version']='0.3'
        s=self.decisions['segments'][0]
        self.decisions['manual_projects']=[{'project_id':'manual-'+'a'*32,'name':'合成新项目',
                                            'shoot_date':'2026-07-09','unit_ids':s['unit_ids'][:]}]
        s['project_id']=self.decisions['manual_projects'][0]['project_id']



class JobTests(Fixture, unittest.TestCase):
    def test_one_submit_creates_missing_folder_copies_hashes_and_keeps_sources(self):
        self.manual()
        before={p.relative_to(self.root/'staging').as_posix():p.read_bytes() for p in (self.root/'staging').rglob('*') if p.is_file()}
        preview=self.service.preflight(self.decisions)
        self.assertEqual(preview['summary']['files'],4)
        self.assertEqual(preview['summary']['pending_units'],1)
        path=self.root/'projects/2026/7月/20260709_自建_合成新项目'
        self.assertFalse(path.exists())
        job=self.service.submit(preview['preview_id'],self.decisions,True)
        self.assertEqual(job['status'],'queued')
        self.assertFalse(path.exists())
        done=self.service.run_job(job['job_id'])
        self.assertEqual(done['status'],'completed',done)
        self.assertEqual(done['summary']['completed_files'],4)
        self.assertEqual(len(done['outcomes']),2)
        self.assertTrue(path.is_dir())
        self.assertTrue(all((path/name).is_dir() for name in STANDARD_FOLDERS))
        self.assertTrue((path/'4选片用JPG原片/A.JPG').is_file())
        self.assertFalse(list(path.rglob('独立归档')))
        for file in (self.root/'console-state/units').glob('receipt-*.json'):
            receipt=json.loads(file.read_text())
            for row in receipt['files']:
                src=self.root/'staging'/row['source_path'];dst=self.root/'projects'/row['target_path']
                self.assertEqual(src.read_bytes(),dst.read_bytes())
                self.assertNotEqual(src.stat().st_ino,dst.stat().st_ino)
                self.assertEqual(dst.stat().st_nlink,1)
        self.assertEqual(before,{p.relative_to(self.root/'staging').as_posix():p.read_bytes() for p in (self.root/'staging').rglob('*') if p.is_file()})

    def test_standard_subfolder_conflict_blocks_before_copy(self):
        project=next(p for p in self.model['projects'] if p['project_id']==self.decisions['segments'][0]['project_id'])
        conflict=self.root/'projects'/project['path']/'视频成片'
        conflict.write_bytes(b'UNRELATED')
        self.assertEqual(self.service.preflight(self.decisions)['status'],'blocked')
        self.assertEqual(conflict.read_bytes(),b'UNRELATED')

    def test_two_units_cannot_publish_the_same_basename(self):
        extra=deepcopy(self.model['units'][0])
        for f in extra['files']: f['source_path']='OTHER/'+f['source_path']
        extra['unit_id']='unit-'+digest(sorted(extra['files'],key=lambda f:f['source_path']))[:24]
        self.model['units'].append(extra)
        self.decisions['segments'][0]['unit_ids'].append(extra['unit_id'])
        self.model['report_id']='sha256:'+digest({k:v for k,v in self.model.items() if k!='report_id'})
        self.decisions['report_id']=self.model['report_id']
        preview=self.service.preflight(self.decisions)
        self.assertEqual(preview['status'],'blocked')
        self.assertIn('同名素材',preview['errors'][0])

    def test_old_layout_state_cannot_be_replayed_into_new_destinations(self):
        self.submit()
        self.service.close()
        path=self.root/'console-state/service-identity.json'
        identity=json.loads(path.read_text());identity.pop('layout_version')
        path.write_text(json.dumps(identity))
        with self.assertRaises(ArchiveError): self.make_service()

    def test_duplicate_submit_reuses_one_task_before_and_after_restart(self):
        before=self.service.preflight(self.decisions)
        jobs=[]
        def submit():
            jobs.append(self.service.submit(before['preview_id'],self.decisions,True))
        a,b=threading.Thread(target=submit),threading.Thread(target=submit)
        a.start();b.start();a.join();b.join()
        self.assertEqual(len(jobs),2)
        self.assertEqual(jobs[0]['job_id'],jobs[1]['job_id'])
        done=self.service.run_job(jobs[0]['job_id'])
        self.assertEqual(done['status'],'completed')
        self.service.close();self.service=self.make_service()
        repeated=self.submit()
        self.assertEqual(repeated['job_id'],done['job_id'])
        self.assertEqual(repeated['status'],'completed')
        self.assertEqual(len(self.service.list_jobs()),1)

    def test_expired_or_changed_plan_cannot_submit(self):
        preview=self.service.preflight(self.decisions)
        changed=deepcopy(self.decisions);changed['segments'][0]['label']='改动'
        with self.assertRaises(ArchiveError):
            self.service.submit(preview['preview_id'],changed,True)
        self.service.previews[preview['preview_id']]['expires_at']=0
        with self.assertRaises(ArchiveError):
            self.service.submit(preview['preview_id'],self.decisions,True)
        self.assertEqual(self.service.list_jobs(),[])

    def test_unrelated_report_refresh_does_not_invalidate_confirmed_plan(self):
        preview=self.service.preflight(self.decisions)
        self.model=deepcopy(self.model);self.model['snapshot_at']=datetime.now(timezone.utc).isoformat()
        self.model['report_id']='sha256:'+digest({k:v for k,v in self.model.items() if k!='report_id'})
        job=self.service.submit(preview['preview_id'],self.decisions,True)
        self.assertEqual(job['status'],'queued')
        self.assertEqual(job['report_id'],self.decisions['report_id'])
        self.assertNotEqual(job['report_id'],self.model['report_id'])

    def test_conflicting_new_directory_blocks_before_any_copy(self):
        self.manual();preview=self.service.preflight(self.decisions)
        target=self.root/'projects/2026/7月/20260709_自建_合成新项目'
        target.mkdir()
        job=self.service.submit(preview['preview_id'],self.decisions,True)
        done=self.service.run_job(job['job_id'])
        self.assertEqual(done['status'],'failed',done)
        self.assertEqual(done['summary']['completed_files'],0)
        self.assertEqual(list(target.iterdir()),[])

    def test_same_size_corruption_blocks_publication(self):
        job=self.submit()
        unit=self.model['units'][0]
        source=self.root/'staging'/unit['files'][0]['source_path']
        original=source.read_bytes();source.write_bytes(b'X'*len(original))
        done=self.service.run_job(job['job_id'])
        self.assertEqual(done['status'],'partial',done)
        self.assertEqual(done['summary']['completed_units'],1)
        self.assertIn('内容',done['errors'][0])

    def test_failure_can_resume_and_completed_units_are_not_copied_again(self):
        self.manual();job=self.submit()
        tripped=[False]
        def stop(phase,row):
            if phase=='copy_chunk' and not tripped[0]:
                tripped[0]=True
                raise ArchiveError('合成中断')
        self.service.checkpoint=stop
        partial=self.service.run_job(job['job_id'])
        self.assertEqual(partial['status'],'partial',partial)
        self.assertEqual(partial['summary']['completed_units'],1)
        self.service.close();self.service=self.make_service()
        self.service.retry(job['job_id'],True)
        done=self.service.run_job(job['job_id'])
        self.assertEqual(done['status'],'completed',done)
        self.assertGreater(sum(o['resumed_bytes'] for o in done['outcomes']),0)
        self.assertGreater(sum(o['reused_files'] for o in done['outcomes']),0)

    def test_pending_work_recovers_after_restart(self):
        job=self.submit()
        self.service.close();self.service=self.make_service()
        self.service.start()
        for _ in range(100):
            result=self.service.get(job['job_id'])
            if result['status'] in ('completed','partial','failed'):break
            self.service.wake.wait(0.02)
        if result['status']!='completed':
            self.service.thread.join(1)
            result=self.service.get(job['job_id'])
        self.assertEqual(result['status'],'completed',result)

    def test_second_destination_for_same_source_blocked(self):
        self.submit()
        self.manual()
        preview=self.service.preflight(self.decisions)
        self.assertEqual(preview['status'],'blocked')
        self.assertIn('其他归档归属',preview['errors'][0])

    def test_segment_candidate_union_allows_shared_assignment(self):
        photos = [u for u in self.model['units'] if u['kind'] == 'photo']
        chosen = photos[0]['candidate_project_ids'][0]
        photos[1]['candidate_project_ids'] = []
        self.model['report_id'] = 'sha256:' + digest({k:v for k,v in self.model.items() if k != 'report_id'})
        self.decisions['report_id'] = self.model['report_id']
        photo_ids = {u['unit_id'] for u in photos}
        segments = [s for s in self.decisions['segments'] if set(s['unit_ids']) & photo_ids]
        merged = {**segments[0], 'unit_ids': [u['unit_id'] for u in photos],
                  'decision': 'confirmed', 'project_id': chosen, 'acknowledge_date_mismatch': True}
        self.decisions['segments'] = [merged] + [s for s in self.decisions['segments'] if not set(s['unit_ids']) & photo_ids]
        _, selected, _, _ = self.service._compile(self.model, self.decisions)
        self.assertEqual({i['unit']['unit_id'] for i in selected if i['unit']['kind'] == 'photo'}, photo_ids)
        self.assertEqual(len({i['project']['path'] for i in selected if i['unit']['kind'] == 'photo'}), 1)

    def test_candidate_union_does_not_leak_between_segments(self):
        photo = next(u for u in self.model['units'] if u['kind'] == 'photo')
        chosen = photo['candidate_project_ids'][0]
        pending = next(s for s in self.decisions['segments'] if s['decision'] == 'pending')
        pending.update(decision='confirmed', project_id=chosen, acknowledge_date_mismatch=True)
        with self.assertRaisesRegex(ArchiveError, '不属于该拍摄段候选'):
            self.service._compile(self.model, self.decisions)
        self.assertEqual(self.service.list_jobs(), [])

    def test_disabled_service_cannot_accept(self):
        self.service.enabled=False
        self.assertEqual(self.service.preflight(self.decisions)['status'],'blocked')
        with self.assertRaises(ArchiveError):
            self.service.submit('x',self.decisions,True)

    def test_real_model_cannot_run_in_synthetic_service(self):
        self.model['example_data']=False
        self.model['report_id']='sha256:'+digest({k:v for k,v in self.model.items() if k!='report_id'})
        self.assertEqual(self.service.preflight(self.decisions)['status'],'blocked')

    def test_production_branch_on_generated_fixture_bytes_only(self):
        # Exercise production validation and receipt schema without customer data.
        self.service.close()
        batch=self.root/'staging/BATCH_20260928_000001'
        manifest=json.loads((batch/'ingest_manifest.json').read_text())
        manifest['example_data']=False
        save_manifest(self.root,manifest)
        receipt=json.loads((batch/'ingest_complete.json').read_text())
        receipt['example_data']=False
        (batch/'ingest_complete.json').write_text(json.dumps(receipt))
        self.model['example_data']=False
        self.model['report_id']='sha256:'+digest({k:v for k,v in self.model.items() if k!='report_id'})
        self.decisions['report_id']=self.model['report_id']
        self.service=ArchiveJobs(self.root/'staging',self.root/'projects',self.root/'production-branch-test-state',
                                 lambda:self.model,self.runtime,production=True,enabled=True)
        # Production startup requires an explicit ownership migration barrier.
        from muli_sorter.archive_io import directory
        from muli_sorter.archive_ownership_cache import recover_full
        with directory(self.service.state/'units') as fd:
            recover_full(fd)
        self.manual()
        job=self.submit()
        done=self.service.run_job(job['job_id'])
        self.assertEqual(done['status'],'completed',done)
        for path in (self.service.state/'units').glob('receipt-*.json'):
            receipt=json.loads(path.read_text())
            self.assertEqual(receipt['schema_version'],'archive/0.4')
            self.assertIs(receipt['example_data'],False)
            self.assertIs(receipt['real_media_write_authorized'],True)

    def test_stopped_worker_blocks_new_preflight(self):
        self.service.start()
        self.service.stop_event.set();self.service.wake.set()
        self.service.thread.join(2)
        self.assertEqual(self.service.preflight(self.decisions)['status'],'blocked')


class HTTPTests(Fixture, unittest.TestCase):
    # Use only the HTTP tests below rather than re-running inherited workflow cases.
    def setUp(self):
        super().setUp()
        self.server=make_server(self.service,self.root)
        self.origin=self.server.public_origin
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join(2)
        super().tearDown()

    def call(self,route,body=None,**headers):
        if body is None:
            request=Request(self.origin+route,headers=headers)
        else:
            request=Request(self.origin+route,data=json.dumps(body).encode(),headers={
                'Content-Type':'application/json','Origin':self.origin,'X-Muli-Request':'1',**headers})
        with urlopen(request,timeout=10) as response:
            return response.status,response.read()

    def test_http_one_click_contract_and_reload_status(self):
        status,body=self.call('/')
        self.assertEqual(status,200)
        self.assertIn('/api/review-page',body.decode())
        import time
        for _ in range(100):
            status,body=self.call('/api/review-page')
            if status == 200: break
            self.assertEqual(status,202)
            time.sleep(.01)
        self.assertIn(b'muli-review-change',body)
        _,raw=self.call('/api/preflight',{'decisions':self.decisions})
        preview=json.loads(raw)
        status,raw=self.call('/api/submit',{'preview_id':preview['preview_id'],'decisions':self.decisions,'confirmed':True})
        self.assertEqual(status,202)
        job=json.loads(raw)
        self.service.run_job(job['job_id'])
        _,raw=self.call('/api/jobs/'+job['job_id'])
        self.assertEqual(json.loads(raw)['status'],'completed')
        _,raw=self.call('/api/jobs')
        self.assertEqual(len(json.loads(raw)['jobs']),1)

    def test_cross_site_null_origin_foreign_host_cannot_read_or_write(self):
        for headers in ({'Origin':'https://other.example'}, {'Origin':'null'}, {'X-Muli-Request':'0'}, {'Sec-Fetch-Site':'cross-site'}):
            with self.subTest(headers=headers),self.assertRaises(HTTPError) as err:
                self.call('/api/preflight',{'decisions':self.decisions},**headers)
            self.assertEqual(err.exception.code,403)
        with self.assertRaises(HTTPError) as err:
            self.call('/api/jobs',Host='other.example')
        self.assertEqual(err.exception.code,403)
        with self.assertRaises(HTTPError) as err:
            self.call('/video-previews/../../model.json')
        self.assertEqual(err.exception.code,404)

    def test_single_job_summary_preserves_progress_without_file_plan(self):
        job=self.submit()
        job['verification']={'checked_files':1,'total_files':4,'checked_bytes':1,'total_bytes':100,'reused_files':1,'hashed_files':0}
        job['errors']=['direct error'];job['outcomes']=[{'error':'unit error'}]
        self.service._save(job)
        _,raw=self.call('/api/jobs/'+job['job_id']+'?view=summary')
        summary=json.loads(raw)
        self.assertEqual(summary['verification'],job['verification'])
        self.assertEqual(summary['summary'],job['summary'])
        self.assertEqual(summary['errors'],['direct error','unit error'])
        self.assertNotIn('file_plans',summary)
        self.assertNotIn('outcomes',summary)
        _,raw=self.call('/api/jobs/'+job['job_id'])
        self.assertEqual(json.loads(raw)['file_plans'],job['file_plans'])
