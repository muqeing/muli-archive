from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.request import Request, urlopen
from urllib.error import HTTPError

from muli_sorter.archive_checks import CheckCancelled, PreflightChecks, TargetDigestCache
from muli_sorter.archive_console import make_server
from muli_sorter.archive_io import ArchiveError, hash_fd
from muli_sorter.archive_layout import studio_target_rows
from test_archive_jobs import Fixture

CLIENT = 'a' * 32
OTHER = 'b' * 32


def finished(checks, key):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        result = checks.get(key)
        if result['status'] not in ('queued', 'checking'):
            return result
        time.sleep(.01)
    raise AssertionError('check did not finish')


class CheckQueueTests(unittest.TestCase):
    def test_single_worker_supersedes_old_work_and_rejects_late_requests(self):
        started, release = threading.Event(), threading.Event()
        calls, concurrent = [], []
        def run(decisions, *, progress):
            calls.append(decisions['n']); concurrent.append(threading.get_ident())
            started.set(); release.wait(2); progress(phase='核对目标文件', bytes_delta=20)
            return {'status':'ready','preview_id':'test'}
        checks = PreflightChecks(run)
        try:
            first = checks.start(CLIENT, 1, {'n':1})
            self.assertTrue(started.wait(1))
            second = checks.start(CLIENT, 2, {'n':2})
            self.assertEqual(checks.start(CLIENT, 2, {'n':2})['check_id'], second['check_id'])
            checks.cancel(CLIENT, 1)
            with self.assertRaises(ArchiveError): checks.start(CLIENT, 1, {'n':1})
            release.set()
            self.assertEqual(finished(checks, second['check_id'])['status'],'ready')
            self.assertEqual(checks.get(first['check_id'])['status'],'cancelled')
            self.assertEqual(calls,[1,2]); self.assertEqual(len(set(concurrent)),1)
        finally: release.set(); checks.close()

    def test_cancel_before_delayed_start_and_capacity_are_bounded(self):
        release = threading.Event()
        def run(decisions, *, progress):
            release.wait(2); progress(); return {'status':'blocked','errors':['fixture']}
        checks = PreflightChecks(run,max_active=1)
        try:
            checks.cancel(CLIENT, 3)
            with self.assertRaises(ArchiveError):checks.start(CLIENT, 3,{})
            checks.start(CLIENT,4,{})
            with self.assertRaises(ArchiveError):checks.start(OTHER,1,{})
            checks.cancel(CLIENT,4)
            with self.assertRaises(ArchiveError):checks.start(CLIENT,4,{})
        finally:release.set();checks.close()

    def test_progress_is_observable_before_work_finishes(self):
        started, release = threading.Event(), threading.Event()
        def run(decisions, *, progress):
            progress(phase='校验目标',checked_files=2,total_files=5,bytes_delta=1024,current_file='test.ARW')
            started.set();release.wait(2);progress();return {'status':'ready','preview_id':'test'}
        checks=PreflightChecks(run)
        try:
            row=checks.start(CLIENT,1,{})
            self.assertTrue(started.wait(1));live=checks.get(row['check_id'])
            self.assertEqual(live['status'],'checking');self.assertEqual(live['progress']['bytes_read'],1024)
            self.assertEqual(live['progress']['checked_files'],2)
            checks.cancel(CLIENT,1);release.set();self.assertEqual(finished(checks,row['check_id'])['status'],'cancelled')
        finally:release.set();checks.close()

    def test_exception_does_not_stall_queue_or_expose_internals(self):
        def run(decisions, *, progress):raise RuntimeError('private internal data')
        checks=PreflightChecks(run)
        try:
            a=checks.start(CLIENT,1,{})
            result=finished(checks,a['check_id'])
            self.assertEqual(result['status'],'blocked');self.assertNotIn('private',json.dumps(result))
            b=checks.start(CLIENT,2,{})
            self.assertEqual(finished(checks,b['check_id'])['status'],'blocked')
        finally:checks.close()


class DigestCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'file';self.path.write_bytes(b'a'*2048);self.cache=TargetDigestCache()
    def tearDown(self):self.tmp.cleanup()
    def read(self,progress=None):
        with self.path.open('rb') as f:return self.cache.digest(f.fileno(),progress)
    def test_unchanged_file_reuses_hash_but_same_size_edit_with_restored_mtime_does_not(self):
        with patch('muli_sorter.archive_checks.hash_fd',wraps=hash_fd) as compute:
            first=self.read();self.assertEqual(self.read(),first);self.assertEqual(compute.call_count,1)
            original=self.path.stat();self.path.write_bytes(b'b'*2048);os.utime(self.path,ns=(original.st_atime_ns,original.st_mtime_ns))
            self.assertNotEqual(self.read(),first);self.assertEqual(compute.call_count,2)
    def test_replacement_and_expiry_invalidate_cache(self):
        with patch('muli_sorter.archive_checks.hash_fd',wraps=hash_fd) as compute:
            first=self.read();other=self.path.with_name('replacement');other.write_bytes(b'a'*2048);other.replace(self.path)
            self.assertEqual(self.read(),first);self.assertEqual(compute.call_count,2)
            self.cache.ttl=0;self.read();self.assertEqual(compute.call_count,3)
    def test_cancelled_or_changing_hash_is_never_cached(self):
        def cancel(_):raise CheckCancelled()
        with self.assertRaises(CheckCancelled):self.read(cancel)
        self.assertEqual(len(self.cache.values),0)
        def change(_):self.path.write_bytes(b'c'*2048)
        with self.assertRaises(ArchiveError):self.read(change)
        self.assertEqual(len(self.cache.values),0)
    def test_cache_capacity_is_bounded(self):
        self.cache.limit=2
        for n in range(5):self.path.write_bytes(bytes([n])*2048);self.read()
        self.assertEqual(len(self.cache.values),2)


class CheckIntegrationTests(Fixture,unittest.TestCase):
    def setUp(self):
        super().setUp();self.decisions['archive_options']={'mode':'copy','existing':'skip_identical'}
    def duplicate(self):
        segment=next(s for s in self.decisions['segments'] if s['decision']=='confirmed')
        unit=next(u for u in self.model['units'] if u['unit_id']==segment['unit_ids'][0])
        project=next(p for p in self.model['projects'] if p['project_id']==segment['project_id'])
        row=studio_target_rows(unit,project)[0]
        source=self.root/'staging'/row['source_path'];dest=self.root/'projects'/row['target_path']
        dest.parent.mkdir(parents=True,exist_ok=True);dest.write_bytes(source.read_bytes());return dest
    def test_same_target_hashed_once_across_both_passes_recheck_and_submit(self):
        self.duplicate()
        with patch('muli_sorter.archive_checks.hash_fd',wraps=hash_fd) as compute:
            first=self.service.preflight(self.decisions);second=self.service.preflight(self.decisions)
            self.assertEqual(first['status'],'ready');self.assertEqual(second['status'],'ready')
            self.assertEqual(compute.call_count,1)
            self.service.submit(second['preview_id'],self.decisions,True)
            self.assertEqual(compute.call_count,1)
    def test_changed_same_size_target_after_preview_cannot_reuse_cached_proof(self):
        dest=self.duplicate();preview=self.service.preflight(self.decisions);before=dest.stat()
        dest.write_bytes(b'X'*before.st_size);os.utime(dest,ns=(before.st_atime_ns,before.st_mtime_ns))
        job=self.service.submit(preview['preview_id'],self.decisions,True)
        done=self.service.run_job(job['job_id'])
        self.assertEqual(done['status'],'failed',done)
        self.assertEqual(done['summary']['completed_files'],0)
        self.assertEqual(dest.read_bytes(),b'X'*before.st_size)
    def test_async_preflight_does_not_create_directories_or_archive_without_submit(self):
        self.manual();before=sorted(str(p.relative_to(self.root/'projects')) for p in (self.root/'projects').rglob('*'))
        check=self.service.checks.start(CLIENT,1,self.decisions);result=finished(self.service.checks,check['check_id'])
        self.assertEqual(result['status'],'ready');self.assertEqual(self.service.list_jobs(),[])
        self.assertEqual(before,sorted(str(p.relative_to(self.root/'projects')) for p in (self.root/'projects').rglob('*')))
        job=self.service.submit(result['preview_id'],self.decisions,True)
        self.assertEqual(self.service.run_job(job['job_id'])['status'],'completed')


class CheckHTTPTests(Fixture,unittest.TestCase):
    def setUp(self):
        super().setUp();self.server=make_server(self.service,self.root/'queue')
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start();self.origin=self.server.public_origin
    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join(2);super().tearDown()
    def post(self,path,body,origin=None):
        request=Request(self.origin+path,data=json.dumps(body).encode(),headers={'Content-Type':'application/json','Origin':origin or self.origin,'X-Muli-Request':'1'})
        with urlopen(request,timeout=3) as r:return r.status,json.load(r)
    def test_start_and_poll_return_immediately_and_keep_existing_submit_contract(self):
        release,started=threading.Event(),threading.Event()
        original=self.service.checks.runner
        def slow(decisions,*,progress):
            progress(total_files=4,phase='核对目标文件');started.set();release.wait(2);return original(decisions,progress=progress)
        self.service.checks.runner=slow
        try:
            status,check=self.post('/api/preflight-checks',{'client_id':CLIENT,'revision':1,'decisions':self.decisions})
            self.assertEqual(status,202);self.assertTrue(started.wait(1))
            with urlopen(self.origin+'/api/preflight-checks/'+check['check_id'],timeout=1) as r:self.assertEqual(json.load(r)['status'],'checking')
            self.assertEqual(self.service.list_jobs(),[]);release.set()
            self.assertEqual(finished(self.service.checks,check['check_id'])['status'],'ready')
        finally:release.set()
    def test_cross_origin_and_invalid_request_are_rejected(self):
        body={'client_id':CLIENT,'revision':1,'decisions':self.decisions}
        with self.assertRaises(HTTPError) as err:self.post('/api/preflight-checks',body,origin='http://evil.example')
        self.assertEqual(err.exception.code,403)
        for bad in ({**body,'client_id':'../invalid'},{**body,'revision':True},{**body,'path':'/staging'}):
            with self.assertRaises(HTTPError):self.post('/api/preflight-checks',bad)
        self.assertEqual(self.service.list_jobs(),[])
