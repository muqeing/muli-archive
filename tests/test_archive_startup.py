"""Startup visibility cannot admit work before the existing checks finish."""
from http.server import BaseHTTPRequestHandler
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from muli_sorter.archive_console import main, make_server
from muli_sorter.archive_startup import StartupServer
from test_archive_jobs import Fixture


def call(origin, path='/', method='GET'):
    request = Request(origin+path, data=b'{}' if method=='POST' else None, method=method)
    try:
        response = urlopen(request, timeout=3)
    except HTTPError as response:
        return response.code, response.read(), dict(response.headers)
    with response:
        return response.status, response.read(), dict(response.headers)


class StartupTests(unittest.TestCase):
    def test_status_visible_but_all_writes_rejected_before_activation(self):
        writes=[]
        class Actual(BaseHTTPRequestHandler):
            def log_message(self, *args):pass
            def do_GET(self):
                self.send_response(200);self.end_headers();self.wfile.write(b'actual')
            def do_POST(self):
                writes.append(1);self.send_response(202);self.end_headers()
        with StartupServer(('127.0.0.1',0)) as server:
            origin=f'http://127.0.0.1:{server.server_port}'
            code,body,_=call(origin);self.assertEqual(code,200)
            self.assertIn('归档服务正在准备'.encode(),body)
            for path in ('/health','/api/review-status','/api/preflight','/api/submissions','/api/jobs/'+'a'*64+'/retry'):
                method='GET' if path in ('/health','/api/review-status') else 'POST'
                code,body,headers=call(origin,path,method)
                self.assertEqual(code,503);self.assertEqual(json.loads(body)['ready'],False)
                self.assertEqual(headers['Retry-After'],'2')
            closed=[];server.attach(Actual,lambda:closed.append(1))
            self.assertEqual(call(origin,'/api/submissions','POST')[0],503)
            self.assertEqual(writes,[])
            server.activate()
            self.assertEqual(call(origin)[1],b'actual')
            self.assertEqual(call(origin,'/api/submissions','POST')[0],202)
            self.assertEqual(writes,[1])
        self.assertEqual(closed,[1]);server.server_close();self.assertEqual(closed,[1])

    def test_main_listens_during_slow_initializer_and_closes_on_failure(self):
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        origin=f'http://127.0.0.1:{port}'
        def fail(*args,**kwargs):
            self.assertEqual(call(origin)[0],200)
            self.assertEqual(call(origin,'/health')[0],503)
            kwargs['startup_progress']('核对历史目录索引')
            self.assertEqual(json.loads(call(origin,'/api/review-status')[1])['phase'],'核对历史目录索引')
            raise ValueError('synthetic_corrupt_index')
        with tempfile.TemporaryDirectory() as folder:
            args=['--queue',folder,'--staging',folder,'--projects',folder,'--state',folder,'--state-db',folder+'/synthetic.sqlite','--port',str(port)]
            with patch('muli_sorter.archive_console.ArchiveJobs',side_effect=fail):
                with self.assertRaisesRegex(ValueError,'synthetic_corrupt_index'):main(args)
            with self.assertRaises(OSError):urlopen(origin,timeout=1)

    def test_invalid_origin_rejected_before_any_listener_or_business_state(self):
        args=['--queue','q','--staging','s','--projects','p','--state','state','--state-db','synthetic','--public-origin','https://example.invalid']
        with patch('muli_sorter.archive_console.ArchiveJobs') as factory,patch('muli_sorter.archive_startup.StartupServer') as server:
            with self.assertRaises(ValueError):main(args)
        factory.assert_not_called();server.assert_not_called()


class ApplicationHandoffTests(Fixture, unittest.TestCase):
    def test_full_console_process_reaches_ready_and_exits_cleanly_on_sigterm(self):
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        origin=f'http://127.0.0.1:{port}'
        log=self.root/'startup-test.log'
        with log.open('w') as output:
            proc=subprocess.Popen([sys.executable,'-m','muli_sorter.archive_console',
                '--queue',str(self.root/'queue'), '--staging',str(self.root/'staging'),
                '--projects',str(self.root/'projects'),'--state',str(self.root/'process-state'),
                '--port',str(port),'--demo-root',str(self.root)], stdout=output,stderr=subprocess.STDOUT)
            try:
                deadline=time.monotonic()+15
                while time.monotonic()<deadline:
                    self.assertIsNone(proc.poll(),log.read_text())
                    try:
                        code,body,_=call(origin,'/health')
                        if code==200:
                            self.assertEqual(json.loads(body)['service'],'muli-sorter-console');break
                    except OSError:pass
                    time.sleep(.05)
                else:self.fail('console not ready: '+log.read_text())
                self.assertEqual(call(origin,'/api/jobs')[0],200)
                proc.terminate();self.assertEqual(proc.wait(timeout=15),0,log.read_text())
            finally:
                if proc.poll() is None:
                    proc.kill();proc.wait(timeout=3)
        self.assertIn('archive-startup',log.read_text())

    def test_original_application_uses_same_listener_after_all_setup(self):
        queue=self.root/'queue';queue.mkdir()
        with StartupServer(('127.0.0.1',0)) as startup:
            origin=f'http://127.0.0.1:{startup.server_port}'
            app=make_server(self.service,queue,port=startup.server_port,startup_server=startup)
            self.assertIs(app,startup)
            self.assertEqual(call(origin,'/health')[0],503)
            startup.activate()
            code,body,_=call(origin,'/health')
            self.assertEqual(code,200);self.assertEqual(json.loads(body)['service'],'muli-sorter-console')
            code,body,_=call(origin,'/api/jobs')
            self.assertEqual(code,200)
