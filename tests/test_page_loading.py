"""Page availability under slow/failing media I/O, without weakening writes."""
import gzip
import json
import threading
import time
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

from test_archive_jobs import Fixture
from muli_sorter.archive_console import make_server


class PageLoadingTests(Fixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.server = make_server(self.service, self.root)
        self.origin = self.server.public_origin
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(2)
        super().tearDown()

    def read(self, route, **headers):
        with urlopen(Request(self.origin + route, headers=headers), timeout=3) as response:
            return dict(response.headers), response.read()

    def wait_page(self):
        until = time.monotonic() + 4
        while time.monotonic() < until:
            headers, body = self.read('/api/review-page')
            if headers['Content-Type'].startswith('text/html'):
                return headers, body
            time.sleep(.01)
        self.fail('builder did not finish')

    def test_shell_available_when_media_provider_fails(self):
        self.server.page_cache.retry_delay = 0
        with patch.object(self.service, 'provider', side_effect=OSError('offline disk')) as provider:
            _, shell = self.read('/')
            self.assertIn('正在读取待处理素材'.encode(), shell)
            provider.assert_not_called()
            self.assertEqual(json.loads(self.read('/api/review-page')[1])['status'], 'building')
            until = time.monotonic() + 2
            while self.server.page_cache._state == 'building' and time.monotonic() < until:
                time.sleep(.01)
            self.server.page_cache.retry_delay = 5
            with self.assertRaises(HTTPError) as error:
                self.read('/api/review-page')
            self.assertEqual(error.exception.code, 409)
            self.assertEqual(json.loads(self.read('/health')[1])['service'], 'muli-sorter-console')
        self.server.page_cache.invalidate()
        _, html = self.wait_page()
        self.assertIn(b'id="review-model"', html)
        self.assertEqual(self.service.list_jobs(), [])

    def test_small_discovery_endpoint_does_not_call_media_or_archive_providers(self):
        (self.root/'队列状态.json').write_text(json.dumps({'source':{'ok':True},'jobs':[],
            'base_model_path':'combined/sha256:'+'a'*64+'/确认模型.json'}))
        with patch.object(self.service,'provider',side_effect=OSError('offline disk')) as media:
            _,raw=self.read('/api/discovery-status')
            self.assertEqual(json.loads(raw)['schema'],'discovery-status/1')
            media.assert_not_called()

    def test_slow_builder_does_not_block_shell_health_or_spawn_second_scan(self):
        entered, release = threading.Event(), threading.Event()
        def slow_provider():
            entered.set()
            if not release.wait(5):
                raise OSError('test timeout')
            return self.model
        with patch.object(self.service, 'provider', side_effect=slow_provider) as provider:
            try:
                self.assertEqual(json.loads(self.read('/api/review-page')[1])['status'], 'building')
                self.assertTrue(entered.wait(2))
                self.assertIn(b'load-status', self.read('/')[1])
                self.assertEqual(json.loads(self.read('/health')[1])['service'], 'muli-sorter-console')
                for _ in range(5):
                    self.assertEqual(json.loads(self.read('/api/review-page')[1])['status'], 'building')
                self.assertEqual(provider.call_count, 1)
            finally:
                release.set()
            self.assertIn(b'id="review-model"', self.wait_page()[1])
            self.read('/api/review-page')
            self.assertEqual(provider.call_count, 1)

    def test_compression_preserves_entire_review_and_honors_refusal(self):
        plain_headers, plain = self.wait_page()
        headers, encoded = self.read('/api/review-page', **{'Accept-Encoding': 'gzip'})
        self.assertEqual(headers['Content-Encoding'], 'gzip')
        self.assertEqual(gzip.decompress(encoded), plain)
        self.assertLess(len(encoded), len(plain) // 2)
        self.assertEqual(headers['Content-Length'], str(len(encoded)))
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertEqual(headers['Vary'], 'Accept-Encoding')
        for accept in ('gzip;q=0', '*;q=1,gzip;q=0', 'x-gzip', 'gzip;q=bad', 'gzip;q=2'):
            with self.subTest(accept=accept):
                headers, payload = self.read('/api/review-page', **{'Accept-Encoding': accept})
                self.assertNotIn('Content-Encoding', headers)
                self.assertEqual(payload, plain)

    def test_summary_retains_results_errors_and_full_job_api(self):
        job = self.submit()
        self.service.run_job(job['job_id'])
        full = json.loads(self.read('/api/jobs')[1])['jobs'][0]
        summary = json.loads(self.read('/api/jobs?view=summary')[1])['jobs'][0]
        for key in ('job_id', 'status', 'summary', 'projects', 'archive_options', 'completed_at'):
            if key in full:
                self.assertEqual(summary[key], full[key])
        self.assertNotIn('outcomes', summary)
        self.assertEqual(json.loads(self.read('/api/jobs/' + job['job_id'])[1]), full)
        with patch.object(self.service, 'list_jobs', return_value=[{**full, 'errors': ['job failed'], 'outcomes': [{'error': 'file failed'}]}]):
            summary = json.loads(self.read('/api/jobs?view=summary')[1])['jobs'][0]
            self.assertEqual(summary['errors'], ['job failed', 'file failed'])

    def test_history_pages_reuse_one_verified_build_and_reject_other_generations(self):
        job = self.submit()
        self.service.run_job(job['job_id'])
        archive = self.service.history.snapshot(self.model)
        sample = archive['archived_units'][0]
        archive['archived_units'] += [dict(sample, unit_id='historical-' + str(i), in_current_model=False) for i in range(120)]
        with patch.object(self.service.history, 'snapshot', return_value=archive) as snapshot:
            self.wait_page()
            route = '/api/archive-history?report_id=' + self.model['report_id']
            first = json.loads(self.read(route)[1])
            second = json.loads(self.read(route + '&offset=50&generation=' + first['generation'])[1])
            self.assertEqual(len(first['archived_units']), 50)
            self.assertEqual(len(second['archived_units']), 50)
            self.assertEqual(snapshot.call_count, 1)
            for suffix in ('&limit=51', '&offset=-1', '&generation=stale', '&offset=0&offset=1'):
                with self.subTest(suffix=suffix), self.assertRaises(HTTPError) as error:
                    self.read(route + suffix)
                self.assertEqual(error.exception.code, 409)
            with self.assertRaises(HTTPError) as error:
                self.read('/api/archive-history?report_id=other')
            self.assertEqual(error.exception.code, 409)

    def test_new_read_routes_keep_host_and_origin_restrictions(self):
        for route in ('/', '/api/review-page', '/api/archive-history?report_id=x', '/api/jobs?view=summary'):
            for headers in ({'Host': 'other.example'}, {'Origin': 'null'}, {'Sec-Fetch-Site': 'cross-site'}):
                with self.subTest(route=route, headers=headers), self.assertRaises(HTTPError) as error:
                    self.read(route, **headers)
                self.assertEqual(error.exception.code, 403)
        self.assertEqual(self.service.list_jobs(), [])
