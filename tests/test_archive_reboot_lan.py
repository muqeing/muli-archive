"""Regression: device renumbering must not invalidate safe persistent receipts."""
import json
import os
from types import SimpleNamespace
from contextlib import contextmanager
import threading
import unittest
from unittest.mock import patch
from urllib.request import Request, urlopen
from urllib.error import HTTPError

from test_archive_jobs import Fixture
from muli_sorter.archive_console import make_server
from muli_sorter.archive_io import ArchiveError


class RebootTests(Fixture, unittest.TestCase):
    @contextmanager
    def remapped_devices(self):
        original = os.fstat
        original_stat = os.stat
        def remapped(fd):
            value=original(fd)
            data={key:getattr(value,key) for key in dir(value) if key.startswith('st_')}
            data['st_dev']+=77
            return SimpleNamespace(**data)
        def remapped_stat(*args, **kwargs):
            value=original_stat(*args, **kwargs)
            data={key:getattr(value,key) for key in dir(value) if key.startswith('st_')}
            data['st_dev']+=77
            return SimpleNamespace(**data)
        # A reboot renumbers both fd and path observations consistently.
        with patch('os.fstat',side_effect=remapped), patch('os.stat',side_effect=remapped_stat):
            yield

    def test_queued_job_recovers_with_different_kernel_device_numbers(self):
        job=self.submit();self.service.close()
        with self.remapped_devices():
            self.service=self.make_service()
            result=self.service.run_job(job['job_id'])
            self.assertEqual(result['status'],'completed',result)
            repeated=self.submit()
            self.assertEqual(repeated['job_id'],job['job_id'])
            self.assertEqual(repeated['status'],'completed')

    def test_partial_copy_and_created_project_resume_after_device_renumbering(self):
        self.manual();job=self.submit();tripped=[False]
        def stop(phase,row):
            if phase=='copy_chunk' and not tripped[0]:
                tripped[0]=True;raise ArchiveError('simulated power interruption')
        self.service.checkpoint=stop
        partial=self.service.run_job(job['job_id'])
        self.assertEqual(partial['status'],'partial')
        self.service.close()
        with self.remapped_devices():
            self.service=self.make_service()
            self.service.retry(job['job_id'],True)
            done=self.service.run_job(job['job_id'])
            self.assertEqual(done['status'],'completed',done)
            self.assertGreater(sum(x['resumed_bytes'] for x in done['outcomes']),0)
            self.assertGreater(sum(x['reused_files'] for x in done['outcomes']),0)

    def test_different_filesystem_same_inode_is_rejected(self):
        self.service.close();original=os.fstatvfs
        def changed(fd):
            v=original(fd)
            return SimpleNamespace(**{key:(getattr(v,key)+1 if key=='f_fsid' else getattr(v,key))
                                      for key in dir(v) if key.startswith('f_')})
        with patch('os.fstatvfs',side_effect=changed),self.assertRaises(ArchiveError):
            self.make_service()

    def test_missing_filesystem_identity_never_falls_back_to_inode(self):
        self.service.close()
        with patch('os.fstatvfs',return_value=SimpleNamespace(f_fsid=0)),self.assertRaises(ArchiveError):
            self.make_service()

    def test_legacy_state_is_not_silently_migrated(self):
        self.service.close()
        path=self.root/'console-state/service-identity.json'
        old=json.loads(path.read_text());old.pop('identity_scheme')
        path.write_text(json.dumps(old))
        with self.assertRaises(ArchiveError):self.make_service()
        self.assertEqual(json.loads(path.read_text()),old)


class LANTests(Fixture,unittest.TestCase):
    def test_lan_origin_requires_explicit_opt_in_and_private_address(self):
        for origin,flag in [('http://192.168.31.244:18767',False),('http://8.8.8.8:18767',True),('http://0.0.0.0:18767',True),('http://untrusted.example:18767',True)]:
            with self.subTest(origin=origin,flag=flag),self.assertRaises(ValueError):
                make_server(self.service,self.root,public_origin=origin,allow_lan_origin=flag)

    def test_lan_origin_requests_work_and_other_origins_are_denied(self):
        origin='http://192.168.31.244:18767'
        server=make_server(self.service,self.root,public_origin=origin,allow_lan_origin=True)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        local='http://127.0.0.1:'+str(server.server_port)
        try:
            with urlopen(Request(local+'/api/jobs',headers={'Host':'192.168.31.244:18767'}),timeout=5) as r:
                self.assertEqual(json.load(r),{'jobs':[]})
            body=json.dumps({'decisions':self.decisions}).encode()
            headers={'Host':'192.168.31.244:18767','Origin':origin,'Content-Type':'application/json','X-Muli-Request':'1'}
            with urlopen(Request(local+'/api/preflight',data=body,headers=headers),timeout=5) as r:
                self.assertEqual(json.load(r)['status'],'ready')
            for changes in [{'Origin':'http://other.example'},{'Host':'other.example'},{'Origin':'null'},{'Sec-Fetch-Site':'cross-site'}]:
                with self.subTest(changes=changes),self.assertRaises(HTTPError) as e:
                    urlopen(Request(local+'/api/preflight',data=body,headers={**headers,**changes}),timeout=5)
                self.assertEqual(e.exception.code,403)
            with urlopen(local+'/health',timeout=5) as r:
                self.assertEqual(json.load(r)['version'],'0.16.2')
            with self.assertRaises(HTTPError):urlopen(local+'/api/jobs',timeout=5)
        finally:
            server.shutdown();server.server_close();thread.join(2)
