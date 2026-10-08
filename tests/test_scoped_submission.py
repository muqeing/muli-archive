import json
import unittest
from pathlib import Path
from unittest.mock import patch

from test_archive_jobs import Fixture
from muli_sorter.review import digest
from muli_sorter.order_feed_io import canonical, atomic_json


class ScopedSubmissionTests(Fixture, unittest.TestCase):
    def test_large_unselected_details_do_not_enter_request_or_block_copy(self):
        selected = {uid for s in self.decisions['segments'] if s['decision'] == 'confirmed'
                    for uid in s['unit_ids']}
        pending = next(u for u in self.model['units'] if u['unit_id'] not in selected)
        pending['warnings'] = ['unselected-history-' + 'x' * (2 * 1024 * 1024)]
        self.model['report_id'] = 'sha256:' + digest({k:v for k,v in self.model.items() if k != 'report_id'})
        self.decisions['report_id'] = self.model['report_id']
        preview = self.service.preflight(self.decisions)
        self.assertEqual(preview['status'], 'ready', preview)
        ticket = self.service.previews[preview['preview_id']]
        self.assertLess(len(canonical(ticket['model'])), 100000)
        # A small limit reproduces the production failure without needing
        # a 48 MiB fixture. The prior full-model request would be rejected.
        with patch('muli_sorter.order_feed_io.MAX_BYTES', 200000):
            job = self.service.submit(preview['preview_id'], self.decisions, True)
        path = self.root/'console-state/requests'/('request-'+job['job_id']+'.json')
        self.assertLess(path.stat().st_size, 200000)
        request = json.loads(path.read_text())
        self.assertEqual(request['scope_binding']['report_id'], self.model['report_id'])
        self.assertEqual(job['report_id'], self.model['report_id'])
        self.assertEqual(set(request['scope_binding']['selected_unit_ids']), selected)
        self.assertNotIn(pending['unit_id'], {u['unit_id'] for u in request['model']['units']})
        done = self.service.run_job(job['job_id'])
        self.assertEqual(done['status'], 'completed', done)
        self.assertEqual(done['summary']['completed_files'], 4)
        self.assertTrue((self.root/'staging'/pending['files'][0]['source_path']).exists())

    def test_submit_reads_live_report_id_without_reloading_full_model(self):
        class Provider:
            def __call__(inner): return self.model
            def current_report_id(inner): return self.model['report_id']
        self.service.provider = Provider()
        preview = self.service.preflight(self.decisions)
        with patch.object(Provider, '__call__', side_effect=AssertionError('full model reread')):
            job = self.service.submit(preview['preview_id'], self.decisions, True)
        self.assertEqual(job['status'], 'queued')

    def test_queue_report_identity_rechecks_changes_without_cloning(self):
        from muli_sorter.queue_model_cache import QueueModelCache
        root=self.root/'queue';root.mkdir()
        name='combined/sha256:'+('a'*64)+'/确认模型.json'
        p=root/name;p.parent.mkdir(parents=True);atomic_json(p,self.model)
        atomic_json(root/'队列状态.json',{'base_model_path':name})
        provider=QueueModelCache(root)
        provider()
        with patch('muli_sorter.queue_model_cache.deepcopy', side_effect=AssertionError('full model clone')):
            self.assertEqual(provider.current_report_id(), self.model['report_id'])
            self.model['snapshot_at']='2026-10-06T03:41:00+08:00'
            self.model['report_id']='sha256:'+digest({k:v for k,v in self.model.items() if k!='report_id'})
            atomic_json(p,self.model)
            self.assertEqual(provider.current_report_id(), self.model['report_id'])


if __name__ == '__main__':
    unittest.main()
