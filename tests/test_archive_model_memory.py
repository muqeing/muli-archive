"""Reduce internal duplicates without sharing mutable state with callers."""
from copy import deepcopy
import unittest
from unittest.mock import patch

from muli_sorter.order_feed_io import atomic_json
from muli_sorter.queue_model_cache import QueueModelCache
from test_archive_jobs import Fixture
import test_order_feed


class OrderCopyTests(unittest.TestCase):
    def test_order_cache_shares_descriptors_but_public_results_stay_detached(self):
        fixture=test_order_feed.OrderFeedTests('test_initial_refresh_idle_and_restart_do_not_repeat_api_calls')
        fixture.setUp();self.addCleanup(fixture.tearDown)
        self.assertEqual(fixture.step(fixture.worker())['state'],'ready')
        provider=QueueModelCache(fixture.queue)
        with patch('muli_sorter.order_feed_view.time.time',return_value=fixture.now):
            actual=provider()
            internal=provider.orders['model']['units'][0]['files']
            self.assertIs(internal,provider.model['units'][0]['files'])
            baseline=deepcopy(provider.model)
            expected=deepcopy(actual)
            actual['units'][0]['files'][0]['name']='caller-only'
            actual['projects'].clear()
            again=provider()
        self.assertEqual(again,expected)
        self.assertEqual(provider.model,baseline)


class EvictionFallbackTests(Fixture, unittest.TestCase):
    def test_evicted_material_page_uses_exact_immutable_snapshot(self):
        queue=self.root/'queue';queue.mkdir()
        model_path=queue/'combined'/self.model['report_id']/'确认模型.json'
        model_path.parent.mkdir(parents=True);atomic_json(model_path,self.model)
        atomic_json(queue/'队列状态.json',{'base_model_path':model_path.relative_to(queue).as_posix()})
        self.service.provider=QueueModelCache(queue)
        self.service.material_reports.MAX_BYTES=1
        expected=self.service.material_state(model=self.model)
        self.assertEqual(len(self.service.material_reports),0)
        self.assertEqual(self.service.material_state(report_id=self.model['report_id']),expected)
        with self.assertRaises(ValueError):
            self.service.material_state(report_id='sha256:'+'0'*64)
