import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from types import SimpleNamespace
from muli_sorter.archive_job_catalog import JobCatalog
from muli_sorter.archive_checks import TargetDigestCache
from muli_sorter.archive_submission import Submissions
from muli_sorter.order_feed_io import atomic_json
from test_archive_jobs import Fixture

class ModelReuseTests(Fixture, unittest.TestCase):
    def test_scoped_history_does_not_open_unselected_receipts(self):
        job=self.submit();self.service.run_job(job['job_id'])
        uid=self.model['units'][0]['unit_id']
        history=self.service.history
        with patch.object(history,'_item',wraps=history._item) as item:
            result=history.for_units(self.model,{uid})
            self.assertTrue(result['archived_units'])
            self.assertTrue(all(r['unit_id']==uid for r in result['archived_units']))
            self.assertTrue(all(c.args[1]['unit_id']==uid for c in item.call_args_list))
        self.assertGreater(len(history.snapshot(self.model)['archived_units']),1)

    def test_runtime_snapshot_shared_but_final_batch_change_blocks(self):
        from copy import deepcopy
        provider=self.service.runtime
        calls=[]
        def read():calls.append(1);return provider()
        self.service.runtime=read
        result=self.service.preflight(self.decisions)
        self.assertEqual(result['status'],'ready')
        self.assertEqual(len(calls),2)
        calls.clear()
        def changed():
            calls.append(1);s=deepcopy(provider())
            if len(calls)>1:s['batches'][0]['revision']+=1
            return s
        self.service.runtime=changed
        self.assertEqual(self.service.preflight(self.decisions)['status'],'blocked')

    def test_unchanged_model_is_parsed_once_and_caller_cannot_poison_cache(self):
        from muli_sorter.queue_model_cache import QueueModelCache, read_json
        root=self.root/'queue';root.mkdir()
        name='combined/sha256:'+('a'*64)+'/确认模型.json'
        p=root/name;p.parent.mkdir(parents=True);atomic_json(p,self.model)
        atomic_json(root/'队列状态.json',{'base_model_path':name})
        provider=QueueModelCache(root)
        with patch('muli_sorter.queue_model_cache.read_json',wraps=read_json) as read:
            first=provider();first['units'].clear();second=provider()
            self.assertTrue(second['units'])
            self.assertEqual(sum(Path(c.args[0])==p for c in read.call_args_list),1)
            atomic_json(p,self.model);provider()
            self.assertEqual(sum(Path(c.args[0])==p for c in read.call_args_list),2)

class ScopeTests(unittest.TestCase):
    def test_completed_job_bodies_read_once_then_only_changed(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td).resolve();a=p/'job-a.json';b=p/'job-b.json'
            atomic_json(a,{'job_id':'a','status':'completed','outcomes':[{'error':'x'}]})
            atomic_json(b,{'job_id':'b','status':'queued'})
            catalog=JobCatalog(p)
            from muli_sorter.archive_job_catalog import read_json
            with patch('muli_sorter.archive_job_catalog.read_json',wraps=read_json) as read:
                catalog.snapshot();catalog.snapshot();self.assertEqual(read.call_count,2)
                atomic_json(b,{'job_id':'b','status':'completed'});catalog.snapshot()
                self.assertEqual(read.call_count,3)
                a.unlink();self.assertEqual(len(catalog.snapshot()),1)

    def test_task_cache_not_evicted_between_passes_and_changed_file_invalidates(self):
        with tempfile.TemporaryDirectory() as td:
            cache=TargetDigestCache(limit=2);cache.reserve(6)
            files=[]
            for i in range(6):
                p=Path(td).resolve()/str(i);p.write_bytes(str(i).encode());files.append(p)
            from muli_sorter.archive_io import hash_fd
            with patch('muli_sorter.archive_checks.hash_fd',wraps=hash_fd) as hashed:
                for _ in range(2):
                    for p in files:
                        with p.open('rb') as f:cache.digest(f.fileno())
                self.assertEqual(hashed.call_count,6)
                files[0].write_bytes(b'changed')
                with files[0].open('rb') as f:cache.digest(f.fileno())
                self.assertEqual(hashed.call_count,7)

    def test_submit_receipt_returns_before_slow_work_and_never_replays(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td).resolve();(root/'requests').mkdir();gate=threading.Event();calls=[]
            token='a'*64;jid='b'*64;decisions={'selected':['one']}
            def submit(*args, **kwargs):calls.append(args);kwargs['progress'](phase='核对本次范围', checked_files=1, total_files=2);gate.wait(3);return {'job_id':jid}
            jobs=SimpleNamespace(state=root,enabled=True,previews={token:{'expires_at':time.time()+1200,'decisions':decisions,'prepared':{'job_id':jid},'model':{'report_id':'r'}}},submit=submit,_job_path=lambda j:root/'requests'/('job-'+j+'.json'))
            pending=Submissions(jobs)
            try:
                start=time.monotonic();row=pending.start(token,decisions,True)
                self.assertEqual(row['status'],'checking');self.assertLess(time.monotonic()-start,.5)
                pending.start(token,decisions,True);self.assertEqual(len(calls),1)
                for _ in range(100):
                    if pending.get(token).get('progress'):break
                    time.sleep(.01)
                self.assertEqual(pending.get(token)['progress']['checked_files'],1)
                # A different process must surface unknown, not rerun it.
                other=Submissions(jobs);self.assertEqual(other.get(token)['status'],'unknown')
                self.assertEqual(other.start(token,decisions,True)['status'],'unknown')
                self.assertEqual(len(calls),1)
                gate.set()
                for _ in range(100):
                    if pending.get(token)['status']=='accepted':break
                    time.sleep(.01)
                self.assertEqual(pending.get(token)['job_id'],jid)
            finally:gate.set();pending.close()

    def test_rejected_vs_partial_write_unknown(self):
        for partial in (False,True):
            with tempfile.TemporaryDirectory() as td:
                root=Path(td).resolve();(root/'requests').mkdir();token='c'*64;jid='d'*64;d={'x':1}
                def fail(*args, **kwargs):
                    if partial:(root/'requests'/('request-'+jid+'.json')).write_text('{}')
                    raise ValueError('changed source')
                jobs=SimpleNamespace(state=root,enabled=True,previews={token:{'expires_at':time.time()+1200,'decisions':d,'prepared':{'job_id':jid},'model':{'report_id':'r'}}},submit=fail)
                pending=Submissions(jobs);pending.start(token,d,True);pending.close()
                self.assertEqual(pending.get(token)['status'],'unknown' if partial else 'rejected')
