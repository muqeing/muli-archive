"""Real synthetic archive executors, with completed page files removed."""
import json,time,unittest
from pathlib import Path
from unittest.mock import patch
import test_archive_jobs as archive_fixture
import test_submission_recovery as recovery_fixture
from muli_sorter.archive_handoff import ArchiveHandoff
from muli_sorter.order_feed_io import atomic_json,read_json
from muli_sorter.snapshot_cleanup import plan,Policy

class ArchiveRetentionTests(archive_fixture.Fixture,unittest.TestCase):
 def setUp(self):
  super().setUp();self.queue=self.root/'queue';self.page=self.queue/'combined'/self.model['report_id'];self.page.mkdir(parents=True)
  atomic_json(self.page/'确认模型.json',self.model);(self.page/'拍摄段确认.html').write_text('synthetic page')
  atomic_json(self.queue/'队列状态.json',{'base_model_path':'combined/'+self.model['report_id']+'/确认模型.json','confirmation_page':'combined/'+self.model['report_id']+'/拍摄段确认.html'})
  self.service.close();self.service=self.make_service(snapshot_retention_queue=self.queue)
 def discard_page_only(self):
  for p in self.page.iterdir():p.unlink()
  self.page.rmdir()
 def no_page(self):return patch.object(self.service,'model',side_effect=AssertionError('must use admitted request'))
 def test_copy_executes_original_request_when_page_is_gone(self):
  job=self.submit();original=(self.service.state/'requests'/('request-'+job['job_id']+'.json')).read_bytes();self.discard_page_only()
  with self.no_page():done=self.service.run_job(job['job_id'])
  self.assertEqual(done['status'],'completed',done);self.assertEqual(original,(self.service.state/'requests'/('request-'+job['job_id']+'.json')).read_bytes())
 def test_direct_move_executes_original_request_when_page_is_gone(self):
  self.service.move_enabled=True;self.service.direct_move_view={'root':str(self.root),'staging':'staging','projects':'projects'};self.decisions['archive_options']={'mode':'move','existing':'skip_identical'}
  job=self.submit();self.discard_page_only()
  with self.no_page():done=self.service.run_job(job['job_id'])
  self.assertEqual(done['status'],'completed',done);self.assertGreater(done['summary']['direct_moved_files'],0)
 def test_copy_then_cleanup_move_executes_without_page(self):
  self.service.move_enabled=True;self.decisions['archive_options']={'mode':'move','existing':'skip_identical'}
  job=self.submit();self.discard_page_only()
  with self.no_page():done=self.service.run_job(job['job_id'])
  self.assertEqual(done['status'],'completed',done);self.assertGreater(done['summary']['removed_sources'],0)
 def test_expired_ticket_and_completed_request_release_pin_feedback_still_works(self):
  preview=self.service.preflight(self.decisions);token=preview['preview_id'];job=self.service.submit(token,self.decisions,True);done=self.service.run_job(job['job_id']);self.assertEqual(done['status'],'completed')
  refs=self.service.retention;refs.refresh(maximum_records=1000,seconds=None);self.assertIn(self.model['report_id'],refs.pins())
  ticket=self.service.previews.get(token);ticket['expires_at']=time.time()-121;self.service.previews[token]=ticket
  refs.refresh(maximum_records=1000,seconds=None);self.assertNotIn(self.model['report_id'],refs.pins())
  before={p:p.read_bytes() for p in (self.service.state/'units').glob('*.json')};self.discard_page_only()
  out=self.root/'out';ack=self.root/'ack';out.mkdir();ack.mkdir();producer=ArchiveHandoff(self.service,out,ack,'2020-01-01T00:00:00+00:00')
  with self.no_page():event=producer.build(job['job_id'])
  self.assertEqual(event['status'],'verified');self.assertEqual(before,{p:p.read_bytes() for p in before})
 def test_original_interrupted_admission_recovers_without_full_page(self):
  token=recovery_fixture.RecoveryTests.interrupted(self);before=self.request_path.read_bytes();self.discard_page_only()
  self.service.close();self.service=self.make_service(snapshot_retention_queue=self.queue)
  with self.no_page(),patch.object(self.service,'_prepare',side_effect=AssertionError('no new preflight')):
   result=recovery_fixture.RecoveryTests.recover_and_wait(self,token)
  self.assertEqual(result['status'],'accepted',result);self.assertEqual(result['job_id'],self.jid);self.assertEqual(before,self.request_path.read_bytes())
 def test_readonly_preview_cannot_omit_new_ticket_from_stale_index(self):
  refs=self.service.retention;refs.refresh(maximum_records=1000,seconds=None)
  self.service.preflight(self.decisions)
  from muli_sorter.snapshot_references import RetentionBlocked
  with self.assertRaisesRegex(RetentionBlocked,'stale'):refs.assert_fresh()
if __name__=='__main__':unittest.main()
