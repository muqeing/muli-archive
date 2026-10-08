import copy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
import subprocess
import sys
from unittest.mock import patch
from test_intake import fixture, write
from muli_sorter.discovery_queue import DiscoveryQueue
from muli_sorter.queue_source import sqlite_snapshot, validate_snapshot
from muli_sorter.intake import EvidenceError
from muli_sorter.queue_view import export


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.staging = self.root/'staging'
        self.projects = self.root/'projects'
        self.staging.mkdir()
        (self.projects/'2026/9月/20260928_00888_合成项目').mkdir(parents=True)
        self.m = fixture(self.staging)
        self.rows = [{**self.m['batch'],'revision':1}]

    def provider(self):
        return {'generated_at':datetime.now(timezone.utc).isoformat(),'complete_listing':True,'batches':copy.deepcopy(self.rows)}

    def queue(self, **kw):
        return DiscoveryQueue(self.staging,self.projects,self.root/'queue',**kw)

    def retry_now(self,q):
        q.db.execute('UPDATE jobs SET next_try=0')
        q.db.commit()

    def test_wait_for_final_commit_duplicate_and_restart(self):
        self.rows[0]['state']='FINALIZING'
        with self.queue() as q:
            self.assertEqual(q.tick(self.provider)['jobs'][0]['state'],'waiting_completion')
            self.rows[0]['state']='COMPLETED'
            result = q.tick(self.provider)
            self.assertEqual(result['jobs'][0]['state'],'awaiting_confirmation')
            artifact = result['jobs'][0]['artifact']
            self.assertTrue((q.root/artifact/'拍摄段确认.html').is_file())
            with patch('muli_sorter.discovery_queue.build_job',side_effect=AssertionError('must not regenerate')):
                for _ in range(3):
                    self.assertEqual(q.tick(self.provider)['jobs'][0]['artifact'],artifact)
        with self.queue() as q:
            self.assertEqual(q.tick(self.provider)['jobs'][0]['artifact'],artifact)
            self.assertEqual(len(q.jobs()),1)

    def test_missing_receipt_then_backfill_without_new_event(self):
        path=self.staging/self.m['batch']['batch_id']/'ingest_complete.json'
        data=path.read_bytes()
        path.unlink()
        with self.queue() as q:
            self.assertEqual(q.tick(self.provider)['jobs'][0]['state'],'retry_wait')
            path.write_bytes(data)
            self.retry_now(q)
            self.assertEqual(q.tick(self.provider)['jobs'][0]['state'],'awaiting_confirmation')

    def test_stale_or_failed_source_pauses_and_preserves_queue(self):
        with self.queue() as q:
            q.tick(self.provider)
            stale=self.provider()
            stale['generated_at']=(datetime.now(timezone.utc)-timedelta(minutes=3)).isoformat()
            result=q.tick(lambda:stale)
            self.assertFalse(result['source']['ok'])
            self.assertEqual(len(result['jobs']),1)
            calls=[]
            q.tick(lambda:calls.append('should back off'))
            self.assertEqual(calls,[])
            q.set_meta('source',{'ok':False,'retry_at':0})
            q.db.commit()
            self.assertTrue(q.tick(self.provider)['source']['ok'])

    def test_crash_after_claim_and_output_recovers(self):
        for stage in ('claimed','artifact_written'):
            with self.subTest(stage=stage):
                state=self.root/('queue-'+stage)
                def crash(s):
                    if s==stage:
                        raise SystemExit(73)
                with DiscoveryQueue(self.staging,self.projects,state,checkpoint=crash) as q:
                    with self.assertRaises(SystemExit):
                        q.tick(self.provider)
                with DiscoveryQueue(self.staging,self.projects,state) as q:
                    self.assertEqual(q.tick(self.provider)['jobs'][0]['state'],'awaiting_confirmation')
                    self.assertEqual(len(q.jobs()),1)

    def test_manifest_tamper_invalidates_previous_ready(self):
        with self.queue() as q:
            q.tick(self.provider)
            path=self.staging/self.m['batch']['batch_id']/'ingest_manifest.json'
            path.write_bytes(path.read_bytes()+b' ')
            self.assertEqual(q.tick(self.provider)['jobs'][0]['state'],'retry_wait')

    def test_version_change_supersedes_old_and_missing_batch_invalidates(self):
        with self.queue() as q:
            q.tick(self.provider)
            self.m['revision']=2
            self.m['manifest_id']='uid1-r2'
            write(self.staging,self.m)
            self.rows[0]['revision']=2
            states=[j['state'] for j in q.tick(self.provider)['jobs']]
            self.assertCountEqual(states,['superseded','awaiting_confirmation'])
            self.rows=[]
            self.assertTrue(all(j['state']=='superseded' for j in q.tick(self.provider)['jobs']))

    def test_cross_batch_owner_state_required(self):
        child=fixture(self.staging,'BATCH_20260928_000002','uid2')
        f=child['files'][0]
        f['copy_status']='skipped_existing'
        f['existing_copy']={'batch_id':self.m['batch']['batch_id'],'batch_uid':'uid1','manifest_id':'uid1-r1',
                            'staging_relative_path':self.m['batch']['batch_id']+'/SOURCE_DATA/A.ARW'}
        f['hash']['existing_destination']=f['hash']['source']
        child['summary'].update(verified_file_count=0,previously_ingested_count=1)
        write(self.staging,child)
        self.rows.append({**child['batch'],'revision':1})
        with self.queue() as q:
            result=q.tick(self.provider,max_jobs=2)
            self.assertTrue(all(j['state']=='awaiting_confirmation' for j in result['jobs']))
            display=export(q,result)
            self.assertEqual(display['summary']['unique_files'],1)
            self.assertEqual(display['summary']['units'],1)
            first=display['confirmation_page']
            self.assertEqual(export(q,q.tick(self.provider))['confirmation_page'],first)
            self.rows[0]['state']='INTERRUPTED'
            result=q.tick(self.provider,max_jobs=2)
            self.assertFalse(any(j['state']=='awaiting_confirmation' for j in result['jobs']))

    def test_new_queued_revision_zero_does_not_block_other_batches(self):
        self.rows.append({'batch_id':'BATCH_20260928_000003','batch_uid':'uid3','revision':0,
                          'state':'QUEUED','result':'NOT_VERIFIED','completed_at':None})
        with self.queue() as q:
            result=q.tick(self.provider)
            self.assertTrue(result['source']['ok'])
            self.assertCountEqual([j['state'] for j in result['jobs']],['awaiting_confirmation','waiting_completion'])

    def test_missing_artifact_regenerates_but_tampered_model_stops(self):
        with self.queue() as q:
            job=q.tick(self.provider)['jobs'][0]
            (q.root/job['artifact']/'拍摄段确认.html').unlink()
            job=q.tick(self.provider)['jobs'][0]
            self.assertEqual(job['state'],'awaiting_confirmation')
            path=q.root/job['artifact']/'确认模型.json'
            model=json.loads(path.read_text())
            model['units']=[]
            path.write_text(json.dumps(model))
            self.assertEqual(q.tick(self.provider)['jobs'][0]['state'],'retry_wait')

    def test_real_process_exit_then_restart_recovers_durable_discovery(self):
        state=self.root/'crashqueue'
        snapshot=self.root/'snapshot.json'
        snapshot.write_text(json.dumps(self.provider()))
        code='''import json,os,sys
from pathlib import Path
from muli_sorter.discovery_queue import DiscoveryQueue
def checkpoint(s):
    if s=='artifact_written': os._exit(73)
with DiscoveryQueue(*sys.argv[1:4],checkpoint=checkpoint) as q:
    q.tick(lambda:json.loads(Path(sys.argv[4]).read_text()))
'''
        result=subprocess.run([sys.executable,'-c',code,str(self.staging),str(self.projects),str(state),str(snapshot)],capture_output=True,text=True)
        self.assertEqual(result.returncode,73,result.stderr)
        with DiscoveryQueue(self.staging,self.projects,state) as q:
            result=q.tick(self.provider)
            self.assertEqual(len(result['jobs']),1)
            self.assertEqual(result['jobs'][0]['state'],'awaiting_confirmation')

    def test_incompatible_input_cannot_reuse_queue(self):
        with self.queue() as q:
            q.tick(self.provider)
        other=self.root/'other'
        other.mkdir()
        with self.assertRaises(EvidenceError):
            DiscoveryQueue(other,self.projects,self.root/'queue')

    def test_mid_processing_state_change_never_publishes_ready(self):
        calls=0
        def provider():
            nonlocal calls
            calls+=1
            result=self.provider()
            if calls>1:
                result['batches'][0]['state']='INTERRUPTED'
            return result
        with self.queue() as q:
            self.assertEqual(q.tick(provider)['jobs'][0]['state'],'retry_wait')

    def test_readonly_sqlite_adapter_only_projects_batch_fields(self):
        path=self.root/'ingest.sqlite3'
        c=sqlite3.connect(path)
        c.execute('CREATE TABLE objects(kind TEXT,id TEXT,data TEXT,PRIMARY KEY(kind,id))')
        c.execute('INSERT INTO objects VALUES (?,?,?)',('batch','uid1',json.dumps(self.rows[0])))
        c.execute('INSERT INTO objects VALUES (?,?,?)',('setting','unrelated',json.dumps({'must_not_read':'synthetic-canary'})))
        c.commit()
        c.close()
        before=path.read_bytes()
        result=sqlite_snapshot(path)
        self.assertEqual(result['batches'],[{k:self.rows[0][k] for k in ('batch_id','batch_uid','revision','state','result','completed_at')}])
        self.assertNotIn('canary',json.dumps(result))
        self.assertEqual(path.read_bytes(),before)
        missing=self.root/'missing.sqlite3'
        with self.assertRaises(EvidenceError):
            sqlite_snapshot(missing)
        self.assertFalse(missing.exists())

    def test_snapshot_requires_complete_unique_rows(self):
        for mutate in (lambda s:s.update(complete_listing=False),lambda s:s['batches'].append(copy.deepcopy(s['batches'][0]))):
            s=self.provider()
            mutate(s)
            with self.assertRaises(EvidenceError):
                validate_snapshot(s)

    def test_size_only_completion_has_no_confirmation_page(self):
        self.rows[0]['result']='COPY_SIZE_VERIFIED'
        with self.queue() as q:
            status=q.tick(self.provider)
            self.assertEqual(status['jobs'][0]['state'],'verification_required')
            self.assertIsNone(export(q,status)['confirmation_page'])

    def test_output_directory_symlinks_never_write_outside_queue(self):
        outside=self.root/'outside'
        outside.mkdir()
        with self.queue() as q:
            q.tick(self.provider,max_jobs=0)
            job=q.jobs()[0]
            (q.root/'reports').mkdir()
            (q.root/'reports'/job['id']).symlink_to(outside,target_is_directory=True)
            self.assertEqual(q.tick(self.provider)['jobs'][0]['state'],'retry_wait')
            self.assertEqual(list(outside.iterdir()),[])
        with DiscoveryQueue(self.staging,self.projects,self.root/'queue2') as q:
            status=q.tick(self.provider)
            (q.root/'combined').symlink_to(outside,target_is_directory=True)
            with self.assertRaises(OSError):
                export(q,status)
            self.assertEqual(list(outside.iterdir()),[])

    def test_half_written_combined_bundle_and_tampering_repaired(self):
        def crash(stage):
            if stage=='combined_model_written':
                raise SystemExit(73)
        with self.queue(checkpoint=crash) as q:
            status=q.tick(self.provider)
            with self.assertRaises(SystemExit):
                export(q,status)
        with self.queue() as q:
            display=export(q,q.tick(self.provider))
            page=q.root/display['confirmation_page']
            self.assertTrue(page.is_file())
            expected=page.read_bytes()
            page.write_text('unexpected modification')
            export(q,q.status())
            self.assertEqual(page.read_bytes(),expected)


if __name__=='__main__':
    unittest.main()
