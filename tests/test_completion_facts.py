"""Synthetic permanent completion and pending-only exchange; no live media."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import threading
import time
import unittest
from unittest.mock import patch
repo = Path(__file__).resolve().parents[1]
for source in (repo/'sibling-ingest/src', repo.parent/'github/muli-ingest/src'):
    if (source/'muli_ingest').is_dir():
        sys.path.insert(0,str(source));break
else:raise RuntimeError('Maintained sibling ingest source required')
from test_archive_jobs import Fixture
from muli_sorter.archive_handoff import ArchiveHandoff, FeedbackDeferred
from muli_sorter import completion_facts as facts, handoff_protocol as wire
from muli_sorter.batch_lifecycle import confirmed_handoff
from muli_sorter.queue_source import sqlite_snapshot
from muli_ingest.archive_handoff import IngestHandoff
from muli_ingest.retention import Retention
from muli_ingest.store import Store

class Engine:
    def __init__(self,root):
        self.staging=root/'staging';self.mode='demo';self.lock=threading.RLock();self.allow_cleanup=False
        self.store=Store(root/'completion-test.sqlite3')
        for p in self.staging.glob('BATCH_*/ingest_manifest.json'):
            m=json.loads(p.read_text());b=deepcopy(m['batch']);b.update(revision=m['revision'],summary=m['summary'])
            b['lifecycle']={'storage_state':'ACTIVE','expires_at':'2020-01-01T00:00:00+00:00','hold':False,'handoff_confirmed':False}
            b['retention_policy_at_completion']={'enabled':True,'expiry_action':'trash','require_handoff_confirmation':True}
            self._save(b)
    def batch(self,uid):return self.store.get('batch',uid)
    def batches(self):return self.store.all('batch')
    def _save(self,b):self.store.put('batch',b['batch_uid'],b)
    def settings(self):return {'values':{'cleanup_enabled':True,'cleanup_paused':False}}

class CompletionFixture(Fixture):
    def setUp(self):
        super().setUp()
        for s in self.decisions['segments']:
            s['decision']='confirmed';s['project_id']=next(u for u in self.model['units'] if u['unit_id']==s['unit_ids'][0])['candidate_project_ids'][0]
        self.engine=Engine(self.root);self.outbox=self.root/'outbox';self.acks=self.root/'acks';self.outbox.mkdir();self.acks.mkdir()
        self.producer=ArchiveHandoff(self.service,self.outbox,self.acks,'2020-01-01T00:00:00+00:00',completion_facts=True)
        self.service.handoff=self.producer;self.consumer=IngestHandoff(self.engine,self.outbox,self.acks,completion_facts=True)
        self.uid=self.engine.batches()[0]['batch_uid']
    def tearDown(self):
        self.service.handoff=None;self.consumer.close();self.producer.close();self.engine.store.close();super().tearDown()
    def complete(self,mode='copy'):
        self.service.move_enabled=True;self.decisions['archive_options']={'mode':mode,'existing':'skip_identical'}
        j=self.service.run_job(self.submit()['job_id']);self.assertEqual(j['status'],'completed',j);return j
    def b(self):return self.engine.batch(self.uid)
    def publish(self,j,clock=None):
        self.producer.tick(clock);return wire.read(self.outbox,j['job_id']+'.completion.json')
    def exchange(self,mode='copy'):
        j=self.complete(mode);f=self.publish(j);a=self.consumer.apply_completion(f);self.producer.tick(time.time()+11);return j,f,a
class CompletionTests(CompletionFixture,unittest.TestCase):
    def test_full_coverage_retires_without_granting_cleanup(self):
        j,f,a=self.exchange();self.assertEqual(a['status'],'recorded');self.assertTrue(facts.confirmed(self.b()))
        self.assertFalse(self.b()['lifecycle']['handoff_confirmed']);self.assertNotIn('archive_handoff_confirmed',self.b()['lifecycle'])
        self.assertTrue(Retention(self.engine).reasons(self.b()));self.assertTrue(confirmed_handoff(self.b(),time.time()+1000000))
        self.assertEqual(self.producer.status(j['job_id'])['status'],'recorded');self.assertEqual(list(self.producer.completion_pending.iterdir()),[])
    def test_permanent_record_survives_delay_but_old_lease_cannot_be_imported(self):
        j=self.complete();f=self.publish(j);late=wire.timestamp(f['evidence']['expires_at'])+100000
        self.assertEqual(self.consumer.apply_completion(f,late)['status'],'recorded');self.assertTrue(facts.confirmed(self.b()))
        with self.assertRaises(ValueError):facts.create(f['evidence'],published_at=late)
        with self.assertRaises(ValueError):facts.validate(f['evidence'])
        self.assertFalse(self.b()['lifecycle']['handoff_confirmed'])
    def test_partial_then_second_job_accumulates_exact_scope(self):
        original=deepcopy(self.decisions)
        for s in self.decisions['segments'][1:]:s.update(decision='pending',project_id=None)
        j=self.complete();self.consumer.apply_completion(self.publish(j));self.assertFalse(confirmed_handoff(self.b()))
        self.assertEqual(self.b()['archive_completion']['status'],'partial');self.producer.tick(time.time()+11)
        self.decisions=original;self.decisions['segments'][0].update(decision='pending',project_id=None)
        j=self.complete();f=self.publish(j,time.time()+12);self.consumer.apply_completion(f,time.time()+13)
        self.assertTrue(facts.confirmed(self.b()));self.assertEqual(self.b()['archive_completion']['confirmed_file_count'],5)
        self.assertEqual(self.b()['archive_completion']['job_count'],2)
    def test_lost_ack_only_republishes_same_ack_after_restart(self):
        j=self.complete();f=self.publish(j)
        with patch('muli_ingest.completion_consumer.wire.write',side_effect=OSError('ack unavailable')):
            with self.assertRaises(OSError):self.consumer.apply_completion(f)
        before=self.engine.store.events(self.uid);self.consumer.close();self.consumer=IngestHandoff(self.engine,self.outbox,self.acks,completion_facts=True)
        with patch.object(self.consumer,'manifest',side_effect=AssertionError('old manifest reread')):self.consumer.apply_completion(f,time.time()+100000)
        self.assertEqual(self.engine.store.events(self.uid),before);self.producer.tick(time.time()+11)
        self.assertEqual(self.producer.status(j['job_id'])['status'],'recorded')
    def test_prepared_publication_recovers_without_rebuilding(self):
        j=self.complete();clock=time.time()
        with patch('muli_sorter.completion_exchange.wire.write',side_effect=FeedbackDeferred()):self.producer.tick(clock)
        self.producer.close();self.producer=ArchiveHandoff(self.service,self.outbox,self.acks,'2020-01-01T00:00:00+00:00',completion_facts=True);self.service.handoff=self.producer
        with patch.object(self.producer,'build',side_effect=AssertionError('rebuilt intent')):self.producer.tick(clock+11)
        self.assertEqual(wire.read(self.outbox,j['job_id']+'.completion.json')['schema'],facts.SCHEMA)
    def test_same_count_wrong_file_or_revised_batch_is_rejected(self):
        j=self.complete();f=self.publish(j);bad=deepcopy(f);bad['evidence']['batches'][0]['files'][0]['relative_path']='WRONG.JPG'
        bad['evidence']=wire.seal(bad['evidence']);bad=facts.seal(bad)
        with self.assertRaises(ValueError):self.consumer.apply_completion(bad)
        self.assertNotIn('archive_completion',self.b());b=self.b();b['revision']+=1;self.engine._save(b)
        with self.assertRaises(ValueError):self.consumer.apply_completion(f)
        self.assertNotIn('archive_completion',self.b())
    def test_target_changed_before_fact_is_isolated_without_retry(self):
        j=self.complete();r=next(iter(j['file_plans'].values()));p=self.service.projects/r['target_path'];p.write_bytes(b'x'*p.stat().st_size)
        self.producer.tick();self.assertEqual(self.producer.status(j['job_id'])['status'],'needs_review')
        with patch.object(self.producer,'build',side_effect=AssertionError('automatic retry')):self.producer.tick(time.time()+100000)
        self.assertEqual(list(self.outbox.glob('*.completion.json')),[])
    def test_closed_work_does_not_open_old_jobs_proofs_or_enumerate_batches(self):
        self.exchange()
        with patch.object(self.producer.service,'get',side_effect=AssertionError('old job read')),patch.object(self.producer,'build',side_effect=AssertionError('old proof read')),patch.object(self.engine,'batches',side_effect=AssertionError('all batches listed')):
            for i in range(3):self.producer.tick(time.time()+100000+i);self.consumer.tick(time.time()+100000+i)
        self.assertTrue(facts.confirmed(self.b()))
    def test_move_stays_protected_from_old_retention(self):
        self.exchange('move');self.assertTrue(self.b()['lifecycle']['archive_sources_moved']);self.assertFalse(self.b()['lifecycle']['handoff_confirmed'])
        self.assertTrue(any('归档移出' in x for x in Retention(self.engine).reasons(self.b())))
    def test_readonly_projection_preserves_boolean_types_and_revision(self):
        self.exchange();rows=sqlite_snapshot(self.root/'completion-test.sqlite3',include_handoff=True)['batches'];b=next(x for x in rows if x['batch_uid']==self.uid)
        self.assertIs(b['archive_completion']['historical_only'],True);self.assertIs(b['archive_completion']['operation_authorized'],False)
        self.assertTrue(confirmed_handoff(b));b['revision']+=1;self.assertFalse(confirmed_handoff(b))
    def test_historical_jobs_not_implicitly_imported(self):
        j,f,a=self.exchange();old=self.service.get(j['job_id']);old['job_id']='e'*64;self.service.handoff=None;self.service._save(old);self.service.handoff=self.producer
        self.producer.tick(time.time()+100000);self.assertEqual(self.producer.status(old['job_id'])['status'],'not_included')
    def test_pending_directory_symlink_is_rejected(self):
        p=self.producer.completion_pending;p.rename(p.with_name('original-pending'));p.symlink_to(p.with_name('original-pending'),target_is_directory=True)
        with self.assertRaises(OSError):self.producer.tick()
    def test_vendor_files_match(self):
        import muli_ingest.completion_facts as other
        self.assertEqual(Path(facts.__file__).read_bytes(),Path(other.__file__).read_bytes())
