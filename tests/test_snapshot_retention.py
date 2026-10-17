import io,json,os,tempfile,time,unittest,tracemalloc
from pathlib import Path
from dataclasses import asdict
from contextlib import redirect_stdout
from unittest.mock import patch
from muli_sorter.order_feed_io import atomic_json,digest
from muli_sorter.snapshot_references import References,RetentionBlocked,scan
from muli_sorter.snapshot_cleanup import Policy,plan,Cleanup
from muli_sorter.snapshot_maintenance_cli import main


def rid(n):return 'sha256:'+format(n,'064x')
class Runtime:
 def __init__(self):self.pauses=0;self.resumes=0;self.busy=None;self.frozen=False;self.onpause=None
 def check(self,*_):return {'busy':self.busy} if self.busy else {'classifier':'fixture','id':'original'}
 def pause(self,b):self.pauses+=1;self.frozen=True;self.onpause and self.onpause()
 def check_paused(self,b):assert self.frozen
 def resume(self,b):self.resumes+=1;self.frozen=False
 def verify(self,b):assert not self.frozen

class Fixture(unittest.TestCase):
 def setUp(self):
  self.t=tempfile.TemporaryDirectory();self.root=Path(self.t.name).resolve();self.state=self.root/'archive';self.queue=self.root/'queue'
  for p in [self.state/'requests',self.state/'submissions',self.state/'confirmation-tickets',self.queue/'combined']:p.mkdir(parents=True,exist_ok=True)
  self.identity={'identity_scheme':'fsid-inode/v1','source_identity':[1,2],'target_identity':[3,4]};atomic_json(self.state/'service-identity.json',self.identity)
  for n in range(25):self.page(n)
  atomic_json(self.queue/'队列状态.json',{'base_model_path':'combined/'+rid(24)+'/确认模型.json','confirmation_page':'combined/'+rid(24)+'/拍摄段确认.html'})
  self.refs=References(self.state,self.queue,create=True);self.refs.activate();self.refs.refresh(maximum_records=1000,seconds=None)
  self.policy=Policy(enabled=True,maximum_generations=2);self.runtime=Runtime();self.cleanup=Cleanup(self.refs,self.policy,self.runtime)
 def tearDown(self):self.t.cleanup()
 def page(self,n,age=9):
  d=self.queue/'combined'/rid(n);d.mkdir(exist_ok=True)
  for name,data in [('确认模型.json',b'{}'),('拍摄段确认.html',b'<p>synthetic</p>')]:
   p=d/name;p.write_bytes(data);stamp=time.time()-age*86400+n;os.utime(p,(stamp,stamp))
  return d
 def control(self,n=1,status='completed',**changes):
  key=format(n,'064x');report=rid(0);plans={'synthetic':{'size':1}}
  request={'admission_policy':'confirmed-plan/v1','job_id':key,'roots':self.identity,'file_plans':plans,'model':{'report_id':report,'synthetic':'x'*10000},'decisions':{}}
  atomic_json(self.state/'requests'/('request-'+key+'.json'),request)
  job={'job_id':key,'status':status,'report_id':report,'file_plans':plans,'request_digest':digest(request),'errors':[]};job.update(changes)
  atomic_json(self.state/'requests'/('job-'+key+'.json'),job);return key,job,request
 def refresh(self):return self.refs.refresh(maximum_records=1000,seconds=None)

class PolicyTests(unittest.TestCase):
 def test_disabled_default_and_strict_bounds(self):
  self.assertFalse(Policy().enabled)
  for field,value in [('minimum_age_days',6),('keep_latest',19),('maximum_generations',21),('maximum_bytes',1000000001),('enabled',1),('pause_seconds',float('nan'))]:
   with self.subTest(field=field),self.assertRaises(ValueError):Policy.parse(dict(asdict(Policy()),**{field:value}))
 def test_unknown_fields_rejected(self):
  with self.assertRaises(ValueError):Policy.parse(dict(asdict(Policy()),extra=True))

class PlanningTests(Fixture):
 def test_recent_latest_current_and_explicit_reference_union(self):
  self.page(0,age=1);p=plan(self.queue,self.policy,{rid(1)})
  self.assertEqual([x['report_id'] for x in p['selected']],[rid(2),rid(3)])
  self.assertTrue({rid(0),rid(1),rid(24)}.issubset(set(p['protected'])|{rid(0)}))
 def test_base_and_display_current_differ(self):
  atomic_json(self.queue/'队列状态.json',{'base_model_path':'combined/'+rid(0)+'/确认模型.json','confirmation_page':'combined/'+rid(1)+'/拍摄段确认.html'})
  p=plan(self.queue,self.policy,set());self.assertNotIn(rid(0),[x['report_id'] for x in p['selected']]);self.assertIn(rid(1),p['protected'])
 def test_byte_budget_skips_oversized_generation(self):
  p=plan(self.queue,Policy(enabled=True,maximum_bytes=1),set());self.assertFalse(p['selected']);self.assertGreater(p['eligible_count'],0)
 def test_unsafe_snapshot_entry_stops_preview(self):
  (self.queue/'combined'/rid(0)/'extra').write_text('never delete')
  with self.assertRaises(RetentionBlocked):plan(self.queue,self.policy,set())
 def test_symlink_ancestor_and_hardlink_rejected(self):
  p=self.queue/'combined'/rid(0)/'确认模型.json';os.link(p,self.root/'linked')
  with self.assertRaises(RetentionBlocked):plan(self.queue,self.policy,set())
  alias=self.root/'alias';alias.symlink_to(self.queue,target_is_directory=True)
  with self.assertRaises(OSError):plan(alias,self.policy,set())

class ReferenceTests(Fixture):
 def test_completed_self_contained_request_releases_display_only(self):
  self.control();self.refresh();before={p:p.read_bytes() for p in (self.state/'requests').iterdir()}
  self.assertNotIn(rid(0),self.refs.pins());self.assertEqual({p:p.read_bytes() for p in before},before)
 def test_failed_retained_running_and_unknown_jobs_stay_protected(self):
  for status in ('failed','partial','copied_unverified','running','unknown'):
   self.control(status=status);self.refresh();self.assertIn(rid(0),self.refs.pins())
 def test_changed_request_roots_plans_or_digest_cannot_release(self):
  for mode in ('digest','roots','plans','policy','unknown_field'):
   key,j,r=self.control()
   if mode=='digest':j['request_digest']='f'*64
   if mode=='roots':r['roots']={'wrong':True}
   if mode=='plans':j['file_plans']={'different':True}
   if mode=='policy':r['admission_policy']='unknown'
   if mode=='unknown_field':r['unknown_future']=True
   if mode!='digest':j['request_digest']=digest(r)
   atomic_json(self.state/'requests'/('request-'+key+'.json'),r);atomic_json(self.state/'requests'/('job-'+key+'.json'),j)
   self.refresh();self.assertIn(rid(0),self.refs.pins(),mode)
 def test_ticket_expiry_margin_and_unresolved_submission(self):
  from muli_sorter.archive_tickets import PersistentTickets
  clock=time.time();tickets=PersistentTickets(self.state/'confirmation-tickets',self.identity,retention=self.refs)
  tickets['b'*64]={'origin_report_id':rid(0),'model':{},'expires_at':clock-119}
  atomic_json(self.state/'submissions'/('c'*64+'.json'),{'submission_id':'c'*64,'report_id':rid(1),'status':'unknown'})
  self.refresh();self.assertEqual(self.refs.pins(clock),{rid(0),rid(1)});self.assertEqual(self.refs.pins(clock+2),{rid(1)})
 def test_orphan_request_never_means_unreferenced(self):
  key,_,_=self.control();(self.state/'requests'/('job-'+key+'.json')).unlink();self.refresh()
  with self.assertRaises(RetentionBlocked):self.refs.pins()
 def test_unchanged_controls_never_parse_again(self):
  self.control();self.refresh()
  with patch.object(self.refs,'_summary',side_effect=AssertionError('historical reparse')):self.assertEqual(self.refresh()['read_records'],0)
 def test_incremental_budget_cannot_authorize_partial_index(self):
  self.control(1);self.control(2);r=self.refs.refresh(maximum_records=1,seconds=None);self.assertFalse(r['complete'])
  with self.assertRaises(RetentionBlocked):self.refs.pins()
  self.assertTrue(self.refresh()['complete'])
 def test_failed_control_write_fences_cleanup_and_original_stays(self):
  path=self.state/'submissions'/('a'*64+'.json')
  def fail(*_):raise OSError('injected failure')
  with self.assertRaises(OSError):self.refs.write(path,{'status':'unknown'},fail)
  self.assertFalse(path.exists())
  with self.assertRaises(RetentionBlocked):self.refresh()
 def test_streamed_historical_model_memory_is_bounded(self):
  key,job,r=self.control();r['model']['synthetic']='x'*(8*1024*1024);path=self.state/'requests'/('request-'+key+'.json');atomic_json(path,r)
  tracemalloc.start();v=scan(path,{'job_id','admission_policy'});_,peak=tracemalloc.get_traced_memory();tracemalloc.stop()
  self.assertEqual(v['sha256'],digest(r));self.assertLess(peak,1024*1024)

class ExecutionTests(Fixture):
 def test_default_run_is_preview_and_has_no_writes(self):
  before=sorted(str(p) for p in self.root.rglob('*'));r=self.cleanup.run();self.assertEqual(r['status'],'dry_run');self.assertEqual(before,sorted(str(p) for p in self.root.rglob('*')));self.assertEqual(self.runtime.pauses,0)
 def test_disabled_or_unconfirmed_policy_never_pauses(self):
  self.assertEqual(Cleanup(self.refs,Policy(),self.runtime).run(execute=True)['status'],'disabled')
  with self.assertRaises(ValueError):self.cleanup.run(execute=True)
  self.assertEqual(self.runtime.pauses,0)
 def test_exact_pair_removal_and_preserved_business_controls(self):
  key,j,r=self.control();self.refresh();before={p:p.read_bytes() for p in (self.state/'requests').iterdir()}
  result=self.cleanup.run(execute=True,confirmed_policy=self.policy.digest())
  self.assertEqual(result['deleted_generations'],2);self.assertFalse((self.queue/'combined'/rid(0)).exists());self.assertTrue((self.queue/'combined'/rid(24)).exists());self.assertEqual(before,{p:p.read_bytes() for p in before});self.assertEqual(self.runtime.resumes,1)
 def test_busy_and_readers_skip_without_pause(self):
  self.runtime.busy='copy';self.assertEqual(self.cleanup.run(execute=True,confirmed_policy=self.policy.digest())['status'],'busy');self.assertEqual(self.runtime.pauses,0)
  self.runtime.busy=None
  with self.refs.gate():self.assertEqual(self.cleanup.run(execute=True,confirmed_policy=self.policy.digest())['status'],'busy')
 def test_nonrecent_running_job_protects_and_defers(self):
  self.control(status='running');self.refresh();self.assertEqual(self.cleanup.run(execute=True,confirmed_policy=self.policy.digest())['status'],'busy');self.assertEqual(self.runtime.pauses,0)
 def test_changed_after_pause_retains_marker_and_always_resumes(self):
  self.runtime.onpause=lambda:(self.queue/'combined'/rid(0)/'确认模型.json').write_text('changed')
  with self.assertRaises(RetentionBlocked):self.cleanup.run(execute=True,confirmed_policy=self.policy.digest())
  self.assertFalse(self.runtime.frozen);self.assertTrue(self.cleanup._pending().exists());count=self.runtime.pauses
  with self.assertRaises(RetentionBlocked):self.cleanup.run(execute=True,confirmed_policy=self.policy.digest())
  self.assertEqual(self.runtime.pauses,count)
 def test_unresolved_freezer_recovery_does_not_replay_deletion(self):
  self.runtime.onpause=lambda:(_ for _ in ()).throw(OSError('pause command result lost'))
  with self.assertRaises(RetentionBlocked):self.cleanup.run(execute=True,confirmed_policy=self.policy.digest())
  before=len(list((self.queue/'combined').iterdir()));result=self.cleanup.recover_freezer(confirmed_policy=self.policy.digest());self.assertTrue(result['requires_review']);self.assertEqual(before,len(list((self.queue/'combined').iterdir())))
 def test_plan_sha_mismatch_has_no_effect(self):
  with self.assertRaises(RetentionBlocked):self.cleanup.run(execute=True,confirmed_policy=self.policy.digest(),expected_plan='wrong')
  self.assertEqual(self.runtime.pauses,0)

class CLITests(Fixture):
 def test_policy_and_version_without_host_access(self):
  with redirect_stdout(io.StringIO()) as out:self.assertEqual(main(['policy']),0)
  self.assertFalse(json.loads(out.getvalue())['data']['policy']['enabled'])
 def test_init_defaults_to_dry_run(self):
  config={'schema':'snapshot-maintenance/1','queue':str(self.queue),'archive_state':str(self.state),'ingest_database':str(self.root/'ingest.db'),'classifier_container':'c','archive_container':'a','protected_containers':['1','2','3','4'],'archive_image_id':'sha256:'+'a'*64,'storage':{'mounts':['/example'],'arrays':['md0'],'disks':['sda']},'policy':asdict(self.policy)}
  path=self.root/'config.json';atomic_json(path,config)
  with redirect_stdout(io.StringIO()) as out:self.assertEqual(main(['--config',str(path),'init']),0)
  self.assertEqual(json.loads(out.getvalue())['data']['status'],'dry_run')

class AdditionalSafetyTests(Fixture):
 def test_derived_index_corruption_does_not_block_original_control_writes(self):
  db=self.refs.root/'references.sqlite3';db.write_bytes(b'injected corrupt cache')
  restarted=References(self.state,self.queue,create=True)
  path=self.state/'submissions'/('d'*64+'.json');value={'submission_id':'d'*64,'status':'checking','report_id':rid(0)}
  restarted.write(path,value,atomic_json);self.assertEqual(json.loads(path.read_text()),value)
  self.assertTrue((restarted.root/'index-blocked.json').exists())
  with self.assertRaises(RetentionBlocked):restarted.refresh()
 def test_readonly_preview_rejects_new_unindexed_reference(self):
  atomic_json(self.state/'submissions'/('d'*64+'.json'),{'submission_id':'d'*64,'status':'checking','report_id':rid(0)})
  with self.assertRaisesRegex(RetentionBlocked,'stale'):self.cleanup.preview()
 def test_budget_expires_before_deletion_and_restores_classifier(self):
  import time
  self.runtime.onpause=lambda:time.sleep(.02)
  policy=Policy(enabled=True,pause_seconds=.001)
  result=Cleanup(self.refs,policy,self.runtime).run(execute=True,confirmed_policy=policy.digest())
  self.assertEqual(result['deleted_generations'],0);self.assertFalse(self.runtime.frozen)
 def test_unknown_files_and_failed_logs_are_not_rotated(self):
  import os
  runs=self.refs.root/'runs';failed=runs/('f'*32);failed.mkdir();(failed/'intent.json').write_text('{}')
  extra=runs/('e'*32);extra.mkdir();atomic_json(extra/'result.json',{'status':'completed','at':time.time()-100*86400});(extra/'unexpected').write_bytes(b'keep')
  self.cleanup._rotate_success_logs();self.assertTrue(failed.exists());self.assertTrue(extra.exists())

class ShadowTests(Fixture):
 def test_shadow_updates_only_derived_index_without_freezing_or_unlinking(self):
  self.control();before={p:p.read_bytes() for p in (self.queue/'combined').rglob('*') if p.is_file()}
  result=Cleanup(self.refs,Policy(),self.runtime).shadow()
  self.assertEqual(result['status'],'shadow');self.assertEqual(before,{p:p.read_bytes() for p in before});self.assertEqual(self.runtime.pauses,0)

class PlanBoundaryTests(Fixture):
 def test_injected_plan_filename_cannot_escape_generation(self):
  from muli_sorter.snapshot_cleanup import delete_pair
  p=plan(self.queue,self.policy,set());row=p['selected'][0];row['files']={'../../outside':next(iter(row['files'].values()))}
  outside=self.queue/'combined'/'outside';outside.write_text('untouched')
  with self.assertRaisesRegex(RetentionBlocked,'filenames'):delete_pair(self.queue,row,lambda:None,lambda event:None)
  self.assertEqual(outside.read_text(),'untouched')

class LegacyControlTests(Fixture):
 def test_indented_legacy_retained_job_is_readable_and_stays_protected(self):
  key,job,_=self.control(status='copied_unverified')
  (self.state/'requests'/('job-'+key+'.json')).write_text(json.dumps(job,ensure_ascii=False,indent=2)+'\n')
  self.refresh();self.assertIn(rid(0),self.refs.pins());self.assertEqual(self.refresh()['read_records'],0)

class MaintenanceLogTests(Fixture):
 def test_only_known_old_success_log_expires(self):
  runs=self.refs.root/'runs';old=runs/('a'*32);old.mkdir();atomic_json(old/'result.json',{'run_id':old.name,'status':'completed','at':time.time()-91*86400});(old/'intent.json').write_text('{}')
  unknown=runs/('b'*32);unknown.mkdir();(unknown/'result.json').write_text('{"status":"completed","at":null}')
  self.cleanup._rotate_success_logs();self.assertFalse(old.exists());self.assertTrue(unknown.exists())
if __name__=='__main__':unittest.main()
