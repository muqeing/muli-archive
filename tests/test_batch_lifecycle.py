from datetime import datetime,timezone,timedelta
import unittest
from muli_sorter.batch_lifecycle import confirmed_handoff

class ClosureProofTests(unittest.TestCase):
    def row(self):
        stamp=datetime.now(timezone.utc)
        return {'state':'COMPLETED','result':'COPY_SIZE_VERIFIED','revision':2,
                'archive_handoff':{'status':'confirmed','revision':2,'confirmed_file_count':10,
                'total_file_count':10,'missing_file_count':0,'source_disposition':'moved',
                'checked_at':stamp.isoformat(),'expires_at':(stamp+timedelta(minutes=30)).isoformat()}}

    def test_size_only_copy_needs_independent_full_archive_ack(self):
        row=self.row();self.assertTrue(confirmed_handoff(row));row.pop('archive_handoff');self.assertFalse(confirmed_handoff(row))

    def test_partial_expired_unknown_and_other_revision_never_close(self):
        for key,value in [('status','partial'),('revision',1),('confirmed_file_count',9),('missing_file_count',1),('source_disposition','partly_moved'),('expires_at','2000-01-01T00:00:00+00:00')]:
            with self.subTest(key=key):
                row=self.row();row['archive_handoff'][key]=value;self.assertFalse(confirmed_handoff(row))
        row=self.row();row['state']='FINALIZING';self.assertFalse(confirmed_handoff(row))

from copy import deepcopy
from pathlib import Path
import tempfile
from unittest.mock import patch
from test_intake import fixture
from muli_sorter.discovery_queue import DiscoveryQueue
from muli_sorter.queue_view import export


class QueueLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name).resolve();self.staging=self.root/'staging';self.staging.mkdir()
        self.projects=self.root/'projects';self.projects.mkdir()
        self.m=fixture(self.staging);self.rows=[{**self.m['batch'],'revision':1}]

    def provider(self):
        return {'generated_at':datetime.now(timezone.utc).isoformat(),'complete_listing':True,'batches':deepcopy(self.rows)}

    def open(self,enabled=True):
        return DiscoveryQueue(self.staging,self.projects,self.root/'queue',enable_lifecycle=enabled)

    def confirmed(self):
        h=ClosureProofTests().row()['archive_handoff'];h['revision']=1;self.rows[0]['archive_handoff']=h

    def test_closed_batch_exits_models_and_never_reopens_after_expiry_restart(self):
        self.confirmed()
        with self.open() as q:
            with patch('muli_sorter.discovery_queue.size_verified_candidate',side_effect=AssertionError('old media examined')):
                status=q.tick(self.provider)
            self.assertEqual(status['jobs'],[])
            self.assertEqual(status['lifecycle']['counts'],{'closed':1})
            display=export(q,status)
            self.assertEqual(display['summary']['units'],0)
            self.assertIsNotNone(display['base_model_path'])
            self.assertEqual(q.history_page()['jobs'][0]['state'],'closed')
        self.rows[0]['archive_handoff']['expires_at']='2000-01-01T00:00:00+00:00'
        with self.open() as q, patch('muli_sorter.discovery_queue.media_signal',side_effect=AssertionError('retired rechecked')):
            self.assertEqual(q.tick(self.provider)['jobs'],[])
            self.assertEqual(q.history_page()['jobs'][0]['state'],'closed')

    def test_partial_batch_remains_active_and_cancellation_persists_without_deletion(self):
        self.confirmed();self.rows[0]['archive_handoff']['status']='partial'
        with self.open() as q:
            snapshot,status=q.discover(self.provider);jid=status['jobs'][0]['id']
            self.assertNotEqual(status['jobs'][0]['state'],'closed')
            before=sorted(p.relative_to(self.staging).as_posix() for p in self.staging.rglob('*'))
            with self.assertRaises(ValueError):q.cancel(jid,False)
            q.cancel(jid,True)
            self.assertEqual(q.tick(self.provider)['jobs'],[])
            self.assertEqual(before,sorted(p.relative_to(self.staging).as_posix() for p in self.staging.rglob('*')))
        with self.open() as q:
            self.assertEqual(q.tick(self.provider)['jobs'],[])
            self.assertEqual(q.history_page()['jobs'][0]['state'],'cancelled')

    def test_new_revision_is_new_work_not_permanently_hidden(self):
        self.confirmed()
        with self.open() as q:
            q.tick(self.provider);self.rows[0]['revision']=2
            _,status=q.discover(self.provider)
            self.assertEqual(len(status['jobs']),1)
            self.assertEqual(status['jobs'][0]['revision'],2)
            self.assertEqual(q.history_page()['jobs'][0]['revision'],1)

    def test_feature_is_opt_in_and_does_not_create_retirement_evidence_without_ack(self):
        self.confirmed()
        with self.open(enabled=False) as q:
            _,status=q.discover(self.provider)
            self.assertNotIn('lifecycle',status)
            self.assertNotEqual(status['jobs'][0]['state'],'closed')

    def test_retired_batch_unexpected_same_revision_state_change_is_quarantined(self):
        self.confirmed()
        with self.open() as q:
            q.tick(self.provider)
            self.rows[0]['state']='COPYING'
            self.assertEqual(q.tick(self.provider)['jobs'],[])
            self.assertEqual(q.history_page()['jobs'][0]['state'],'needs_attention')
        with self.assertRaisesRegex(ValueError,'禁止关闭'):
            self.open(enabled=False)

    def test_readonly_sqlite_summary_excludes_handoff_file_details(self):
        import sqlite3,json
        from muli_sorter.queue_source import sqlite_snapshot
        self.confirmed();self.rows[0]['archive_handoff']['files']=['not-for-classifier']*100
        db=self.root/'ingest.sqlite3'
        with sqlite3.connect(db) as conn:
            conn.execute('CREATE TABLE objects(kind,id,data)')
            conn.execute('INSERT INTO objects VALUES (?,?,?)',('batch','one',json.dumps(self.rows[0])))
        before=db.read_bytes()
        result=sqlite_snapshot(db,include_handoff=True)
        self.assertNotIn('not-for-classifier',json.dumps(result))
        self.assertTrue(confirmed_handoff(result['batches'][0]))
        self.assertEqual(before,db.read_bytes())

    def test_history_reads_running_queue_without_mutating_sqlite_or_source(self):
        from muli_sorter.batch_lifecycle import read_history
        self.confirmed()
        with self.open() as q:
            q.tick(self.provider)
            before=(self.root/'queue'/'queue.sqlite3').read_bytes()
            result=read_history(self.staging,self.projects,self.root/'queue')
            self.assertEqual(result['total'],1)
            self.assertEqual(before,(self.root/'queue'/'queue.sqlite3').read_bytes())

    def test_10_100_1000_retired_batches_do_not_touch_old_media_or_models(self):
        from muli_sorter.discovery_queue import size_verified_candidate
        for count in (10,100,1000):
            with self.subTest(history=count):
                rows=[deepcopy(self.rows[0])]
                for index in range(count):
                    old={**self.rows[0],'batch_id':f'BATCH_20260101_{index:06d}',
                         'batch_uid':f'old-{count}-{index}'}
                    old['archive_handoff']=ClosureProofTests().row()['archive_handoff']
                    old['archive_handoff']['revision']=1
                    rows.append(old)
                def provider():
                    return {'generated_at':datetime.now(timezone.utc).isoformat(),
                            'complete_listing':True,'batches':deepcopy(rows)}
                with DiscoveryQueue(self.staging,self.projects,self.root/f'queue-{count}',enable_lifecycle=True) as q:
                    with patch('muli_sorter.discovery_queue.size_verified_candidate',wraps=size_verified_candidate) as checks:
                        q.tick(provider);status=q.tick(provider)
                    self.assertEqual(len(status['jobs']),1)
                    self.assertEqual(status['lifecycle']['counts']['closed'],count)
                    self.assertTrue(all(c.args[1]['batch_id']==self.rows[0]['batch_id'] for c in checks.call_args_list))
                    page=q.history_page(0,50)
                    self.assertEqual(page['total'],count)
                    self.assertEqual(len(page['jobs']),min(50,count))

    def test_empty_page_explains_retirement_with_only_bounded_summary(self):
        from muli_sorter.discovery_status import snapshot
        self.confirmed()
        with self.open() as q:
            status=q.tick(self.provider);export(q,status)
            view=snapshot(q.root)
            self.assertEqual(view['lifecycle'],{'closed':1})
            self.assertEqual(view['batches'],[])
            self.assertIn('1 批已结案',(q.root/'分类队列.html').read_text())
