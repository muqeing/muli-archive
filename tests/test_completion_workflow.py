from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch
from test_completion_facts import CompletionFixture, Store, wire, facts
from muli_sorter.completion_workflow import CompletionWorkflow

class IndexTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve();self.index=CompletionWorkflow(self.root,{'source':'synthetic'})
        self.addCleanup(self.index.close)
    def job(self,jid='a'*64,status='completed'):
        return {'job_id':jid,'status':status,'request_digest':'b'*64,'summary':{'total_files':1},'completed_at':'2026-01-01T00:00:00+00:00'}
    def test_ten_thousand_recorded_jobs_do_not_enter_pending_lookup(self):
        with self.index.lock,self.index.db:
            self.index.db.executemany("INSERT INTO work(job_id,binding,ready,state) VALUES(?,?,1,'recorded')",((f'{i:064x}','old') for i in range(10000)))
        self.index.register(self.job());rows=[];self.index.db.set_trace_callback(rows.append)
        for i in range(10):self.assertEqual(self.index.next(100)['job_id'],'a'*64)
        self.assertTrue(all('LIMIT 1' in q for q in rows))
        plan=self.index.db.execute("EXPLAIN QUERY PLAN SELECT * FROM work WHERE ready=1 AND state IN ('pending','publishing','published') AND next_try<=100 ORDER BY next_try,job_id LIMIT 1").fetchall()
        self.assertTrue(any('INDEX pending' in str(tuple(r)) for r in plan))
    def test_index_replacement_fails_closed(self):
        p=self.index.path;p.rename(p.with_suffix('.other'));p.write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError,'replaced'):self.index.register(self.job())
    def test_changed_recorded_binding_is_not_silently_reopened(self):
        j=self.job();self.index.register(j);self.index.prepare(j['job_id'],self.index.binding(j),{'fact_id':'c'*64})
        self.index.record(j['job_id'],'c'*64,{'checked_at':'2026-01-01T00:00:00+00:00','batches':[]})
        j['summary']['total_files']=2
        with self.assertRaisesRegex(ValueError,'changed'):self.index.register(j)
        self.assertEqual(self.index.status(j['job_id'])['status'],'recorded')
    def test_registration_before_control_write_does_not_publish_prematurely(self):
        j=self.job(status='running');self.index.register(j);self.assertIsNone(self.index.next(100000))
        j['status']='completed';self.index.register(j);self.assertEqual(self.index.next(100000)['state'],'pending')
    def test_unknown_index_state_rejected(self):
        import sqlite3
        with self.assertRaises(sqlite3.IntegrityError):
            with self.index.db:self.index.db.execute("INSERT INTO work(job_id,binding,ready,state) VALUES('x','x',1,'unknown')")
    def test_expiry_uses_only_due_permissions_not_ten_thousand_permanent_records(self):
        s=Store(self.root/'ingest.sqlite');self.addCleanup(s.close)
        with s.transaction():
            for i in range(10000):s.put('batch',str(i),{'archive_completion':{'status':'confirmed'}})
            s.put('batch','due',{'archive_handoff':{'status':'confirmed','expires_at':'2020-01-01T00:00:00+00:00'}})
            s.put('batch','future',{'archive_handoff':{'status':'confirmed','expires_at':'2030-01-01T00:00:00+00:00'}})
        rows=s.expiring_archive_handoffs('2026-01-01T00:00:00+00:00');self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['archive_handoff']['expires_at'],'2020-01-01T00:00:00+00:00')

# Inherit the end-to-end synthetic scenarios as well as the additional crash fences.
class TransactionTests(CompletionFixture, unittest.TestCase):
    def test_cas_rejects_batch_change_without_partial_fact_commit(self):
        j=self.complete();f=self.publish(j);original=facts.projection
        def revise(*a,**kw):
            result=original(*a,**kw);b=self.b();b['revision']+=1;self.engine._save(b);return result
        with patch('muli_ingest.completion_consumer.facts.projection',side_effect=revise):
            with self.assertRaisesRegex(ValueError,'changed_before_commit'):self.consumer.apply_completion(f)
        self.assertIsNone(self.engine.store.get('archive_completion_seen',j['job_id']));self.assertNotIn('archive_completion',self.b())
    def test_pending_consumer_acks_and_producer_retires_marker(self):
        import time
        j=self.complete();self.publish(j);self.consumer.tick();self.producer.tick(time.time()+11)
        self.assertEqual(self.producer.status(j['job_id'])['status'],'recorded');self.assertTrue(facts.confirmed(self.b()))
    def test_refused_fact_does_not_modify_existing_completed_fact(self):
        j,f,a=self.exchange();before=self.b();bad=json.loads(json.dumps(f));bad['operation_authorized']=True;bad=facts.seal(bad)
        with self.assertRaises(ValueError):self.consumer.apply_completion(bad)
        self.assertEqual(before,self.b())
    def test_rejected_revision_retires_pending_marker_and_remains_attention(self):
        import time
        j=self.complete();self.publish(j);b=self.b();b['revision']+=1;self.engine._save(b)
        self.consumer.tick();self.producer.tick(time.time()+11)
        self.assertEqual(self.producer.status(j['job_id'])['status'],'needs_review')
        self.assertEqual(list(self.producer.completion_pending.iterdir()),[])
        self.assertNotIn('archive_completion',self.b())
    def test_acknowledged_marker_hint_is_removed_from_consumer_memory(self):
        import time
        j=self.complete();self.publish(j);self.consumer.tick();self.assertEqual(len(self.consumer.tokens),1)
        self.producer.tick(time.time()+11);self.consumer.tick(time.time()+12)
        self.assertEqual(self.consumer.tokens,{})
    def test_legacy_closure_evidence_does_not_select_an_invalid_completion_projection(self):
        from muli_sorter.batch_lifecycle import handoff_evidence
        row={'archive_completion':None,'archive_handoff':{'status':'confirmed'}}
        self.assertEqual(handoff_evidence(row),row['archive_handoff'])
