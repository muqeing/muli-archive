from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch
from test_archive_jobs import Fixture
from muli_sorter.archive_io import ArchiveError
from muli_sorter.archive_layout import studio_target_rows


class ModesTests(Fixture,unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.decisions['archive_options']={'mode':'copy','existing':'skip_identical'}
        self.service.move_enabled=True

    def first(self):
        segment=next(s for s in self.decisions['segments'] if s['decision']=='confirmed')
        unit=next(u for u in self.model['units'] if u['unit_id']==segment['unit_ids'][0])
        project=next(p for p in self.model['projects'] if p['project_id']==segment['project_id'])
        row=studio_target_rows(unit,project)[0]
        src=self.root/'staging'/row['source_path'];dst=self.root/'projects'/row['target_path']
        dst.parent.mkdir(parents=True,exist_ok=True)
        return src,dst

    def run_ready(self):
        job=self.submit();done=self.service.run_job(job['job_id'])
        self.assertEqual(done['status'],'completed',done)
        return done

    def test_same_name_size_and_hash_skips_without_rewriting(self):
        src,dst=self.first();dst.write_bytes(src.read_bytes());ino=dst.stat().st_ino
        before=self.service.preflight(self.decisions)
        self.assertEqual(before['summary']['skipped_files'],1)
        done=self.run_ready()
        self.assertEqual(done['summary']['skipped_files'],1)
        self.assertEqual(dst.stat().st_ino,ino)
        self.assertTrue(src.exists())
        self.assertEqual(done['summary']['removed_sources'],0)

    def test_same_name_different_size_or_same_size_different_hash_gets_suffix(self):
        src,dst=self.first();data=b'X'*src.stat().st_size;dst.write_bytes(data)
        dst1=dst.with_stem(dst.stem+'_1');dst1.write_bytes(b'other size')
        expected=dst.with_stem(dst.stem+'_2')
        preview=self.service.preflight(self.decisions)
        self.assertEqual(preview['summary']['renamed_files'],1)
        self.assertEqual(preview['summary']['skipped_files'],0)
        self.run_ready()
        self.assertEqual(dst.read_bytes(),data)
        self.assertEqual(dst1.read_bytes(),b'other size')
        self.assertEqual(expected.read_bytes(),src.read_bytes())
        # Retry/re-submit reuses the same planned suffix.
        self.run_ready();self.assertFalse(dst.with_stem(dst.stem+'_3').exists())

    def test_different_name_same_content_is_not_skipped(self):
        src,dst=self.first();dst.with_stem('another_name').write_bytes(src.read_bytes())
        before=self.service.preflight(self.decisions)
        self.assertEqual(before['summary']['skipped_files'],0)
        self.run_ready();self.assertEqual(dst.read_bytes(),src.read_bytes())

    def test_stop_policy_preserves_existing_even_if_identical(self):
        src,dst=self.first();dst.write_bytes(src.read_bytes())
        self.decisions['archive_options']['existing']='error'
        self.assertEqual(self.service.preflight(self.decisions)['status'],'blocked')
        self.assertTrue(src.exists())

    def test_options_are_bound_to_preview_and_move_requires_gate(self):
        before=self.service.preflight(self.decisions)
        self.decisions['archive_options']['mode']='move'
        with self.assertRaises(ArchiveError):self.service.submit(before['preview_id'],self.decisions,True)
        self.service.move_enabled=False
        self.assertEqual(self.service.preflight(self.decisions)['status'],'blocked')

    def test_move_cleans_both_new_copies_and_identical_existing_after_all_verify(self):
        src,dst=self.first();content=src.read_bytes();dst.write_bytes(content);ino=dst.stat().st_ino
        self.decisions['archive_options']['mode']='move'
        evidence={p:p.read_bytes() for p in (self.root/'staging').rglob('*') if p.is_file() and 'SOURCE_DATA' not in p.parts}
        before=self.service.preflight(self.decisions)
        self.assertEqual(before['summary']['cleanup_files'],4)
        done=self.run_ready()
        self.assertFalse(src.exists());self.assertEqual(dst.stat().st_ino,ino)
        self.assertEqual(done['summary']['removed_sources'],4)
        self.assertEqual(done['summary']['skipped_files'],1)
        self.assertEqual(dst.read_bytes(),content)
        self.assertEqual(evidence,{p:p.read_bytes() for p in evidence})

    def test_partial_copy_never_starts_cleanup(self):
        src,dst=self.first();self.decisions['archive_options']['mode']='move'
        job=self.submit()
        def stop(phase,row):
            if phase=='copy_chunk':raise ArchiveError('injected copy failure')
        self.service.checkpoint=stop
        done=self.service.run_job(job['job_id'])
        self.assertEqual(done['status'],'partial')
        self.assertTrue(src.exists());self.assertFalse(done.get('cleanup_started'))
        self.assertEqual(list((self.service.state/'requests').glob('move-*')),[])

    def test_target_changes_before_unlink_preserves_source_and_fails(self):
        src,dst=self.first();self.decisions['archive_options']['mode']='move';job=self.submit()
        def change(phase,row):
            if phase=='move_intent':
                target=self.root/'projects'/row['target_path'];target.write_bytes(b'X'*target.stat().st_size)
        self.service.checkpoint=change
        done=self.service.run_job(job['job_id'])
        self.assertEqual(done['status'],'failed',done);self.assertTrue(src.exists())
        self.assertEqual(done['summary']['removed_sources'],0)

    def test_source_replaced_after_intent_is_not_deleted(self):
        src,dst=self.first();self.decisions['archive_options']['mode']='move';job=self.submit()
        def change(phase,row):
            if phase=='move_intent':
                source=self.root/'staging'/row['source_path'];source.unlink();source.write_bytes(b'NEW SOURCE')
        self.service.checkpoint=change
        done=self.service.run_job(job['job_id'])
        self.assertEqual(done['status'],'failed',done)
        self.assertEqual(src.read_bytes(),b'NEW SOURCE')

    def test_numbered_target_changed_after_preview_requires_recheck(self):
        src,dst=self.first();dst.write_bytes(b'conflict')
        before=self.service.preflight(self.decisions)
        dst.with_stem(dst.stem+'_1').write_bytes(b'appeared later')
        job=self.service.submit(before['preview_id'],self.decisions,True)
        done=self.service.run_job(job['job_id'])
        self.assertEqual(done['status'],'failed',done)
        self.assertEqual(done['summary']['completed_files'],0)
        self.assertEqual(dst.with_stem(dst.stem+'_1').read_bytes(),b'appeared later')
        self.assertTrue(src.exists())

    def test_selected_other_unit_in_batch_still_verifies_after_move(self):
        # First move just one confirmed unit, then copy the other from that batch.
        self.decisions['segments'][1]['decision']='deferred'
        self.decisions['archive_options']['mode']='move'
        done=self.run_ready();self.assertEqual(done['summary']['removed_sources'],2)
        self.decisions['segments'][0]['decision']='deferred'
        self.decisions['segments'][1]['decision']='confirmed'
        self.decisions['archive_options']['mode']='copy'
        done=self.run_ready();self.assertEqual(done['summary']['completed_files'],2)

    def test_restart_after_actual_unlink_resumes_without_deleting_another_file(self):
        self.decisions['archive_options']['mode']='move'
        job=self.submit();job_id=job['job_id'];self.service.close()
        code='''
import json,os,sys
from pathlib import Path
from datetime import datetime,timezone
from muli_sorter.archive_jobs import ArchiveJobs
root=Path(sys.argv[1])
def runtime():
 d=json.loads((root/'runtime-state.json').read_text());d['generated_at']=datetime.now(timezone.utc).isoformat();return d
def stop(phase,row):
 if phase=='source_removed':os._exit(73)
with_service=ArchiveJobs(root/'staging',root/'projects',root/'console-state',lambda:json.loads((root/'model.json').read_text()),runtime,enabled=True,move_enabled=True,checkpoint=stop)
with_service.run_job(sys.argv[2])
'''
        result=subprocess.run([sys.executable,'-c',code,str(self.root),job_id],capture_output=True,text=True)
        self.assertEqual(result.returncode,73,result.stderr)
        self.service=self.make_service(move_enabled=True)
        done=self.service.run_job(job_id)
        self.assertEqual(done['status'],'completed',done)
        self.assertEqual(done['summary']['removed_sources'],4)
        self.assertEqual(self.service.run_job(job_id),done)

    def test_same_content_under_different_numbered_name_still_keeps_both(self):
        src,dst=self.first();dst.write_bytes(b'conflict')
        dst.with_stem(dst.stem+'_1').write_bytes(src.read_bytes())
        preview=self.service.preflight(self.decisions)
        self.assertEqual(preview['summary']['skipped_files'],0)
        self.run_ready()
        self.assertEqual(dst.with_stem(dst.stem+'_2').read_bytes(),src.read_bytes())

    def test_queue_rebuild_after_move_keeps_remaining_batch_visible(self):
        from muli_sorter.queue_evidence import build_job
        from muli_sorter.archive_layout import route_view
        self.decisions['segments'][1]['decision']='deferred'
        self.decisions['archive_options']['mode']='move'
        done=self.run_ready()
        batch_id=self.model['units'][0]['provenance'][0]['batch_id']
        rebuilt,_,_=build_job(self.root/'staging',batch_id,self.runtime(),self.model['projects'],allow_examples=True)
        self.assertEqual(len(rebuilt['units']),len(self.model['units']))
        missing=[u for u in rebuilt['units'] if any('中转文件已不在原路径' in w for w in u['warnings'])]
        self.assertEqual(len(missing),1)
        self.assertTrue(route_view(missing[0])['blocked'])
        self.assertTrue(any(not route_view(u)['blocked'] for u in rebuilt['units']))

    def test_backup_shared_access_blocked_during_cleanup(self):
        from muli_sorter.staging_coordination import staging_guard
        src,dst=self.first();self.decisions['archive_options']['mode']='move';job=self.submit()
        protected=[]
        def starts_backup(phase,row):
            if phase=='move_intent':
                with self.assertRaises(ValueError):
                    with staging_guard(self.root/'staging'):pass
                protected.append(True)
        self.service.checkpoint=starts_backup
        done=self.service.run_job(job['job_id'])
        self.assertEqual(done['status'],'completed',done)
        self.assertEqual(len(protected),4)
        self.assertFalse(src.exists())

    def test_active_backup_at_cleanup_start_preserves_sources(self):
        src,dst=self.first();self.decisions['archive_options']['mode']='move';job=self.submit()
        original=self.runtime()
        original['batches'].append({'batch_id':'OTHER','state':'COPYING'})
        self.service.runtime=lambda:original
        done=self.service.run_job(job['job_id'])
        self.assertEqual(done['status'],'failed',done)
        self.assertIn('备份任务尚未结束',done['errors'][0])
        self.assertTrue(src.exists())
