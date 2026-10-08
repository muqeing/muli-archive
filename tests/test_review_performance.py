import json
import threading
import unittest
from unittest.mock import patch
from pathlib import Path
from test_review_page_cache import FakeClock, wait_until
import test_archive_history as history_tests
from muli_sorter.review_page_cache import ReviewPageCache
from muli_sorter.order_feed_io import atomic_json


class DisplayRefreshTests(unittest.TestCase):
    def bundle(self, value='ready'):
        return {'report_id':'report', 'archive_state':{'report_id':'report'},
                'html_bytes':('<html><body>'+value+'</body></html>').encode()}

    def test_display_is_immediate_during_refresh_but_strict_access_is_not_stale(self):
        clock=FakeClock();release=threading.Event();calls=[]
        def builder(phase):
            calls.append(1)
            if len(calls)>1:release.wait(2)
            return self.bundle(str(len(calls)))
        cache=ReviewPageCache(builder,lambda:'key',ttl=30,display_grace=300,clock=clock)
        cache.read();wait_until(lambda:cache.get_bundle() is not None)
        clock.advance(31)
        response=cache.read()
        self.assertEqual(response[0],200)
        self.assertIn('上次核验'.encode(),response[1])
        self.assertIsNone(cache.get_bundle())
        self.assertIsNotNone(cache.get_display_bundle())
        wait_until(lambda:len(calls)==2)
        for _ in range(8):self.assertEqual(cache.read()[0],200)
        self.assertEqual(len(calls),2)
        release.set();wait_until(lambda:cache.get_bundle() is not None)
        self.assertNotIn(b'review-refresh',cache.read()[1])
        cache.shutdown()

    def test_new_batch_signature_and_explicit_invalidation_never_reuse_old_page(self):
        clock=FakeClock();key=['old'];release=threading.Event();calls=[]
        def build(phase):
            calls.append(1)
            if len(calls)>1:release.wait(2)
            return self.bundle()
        cache=ReviewPageCache(build,lambda:key[0],display_grace=300,clock=clock)
        cache.read();wait_until(lambda:cache.get_bundle() is not None)
        key[0]='new'
        self.assertEqual(cache.read()[0],202)
        self.assertIsNone(cache.get_display_bundle())
        self.assertTrue(cache.status()['requires_reload'])
        release.set();wait_until(lambda:cache.get_bundle() is not None)
        cache.invalidate()
        self.assertIsNone(cache.get_display_bundle())
        cache.shutdown()

    def test_refresh_failure_has_bounded_display_age_and_backoff(self):
        clock=FakeClock();calls=[]
        def build(phase):
            calls.append(1)
            if len(calls)>1:raise ValueError('unavailable')
            return self.bundle()
        cache=ReviewPageCache(build,lambda:'key',ttl=30,display_grace=300,clock=clock)
        cache.read();wait_until(lambda:cache.get_bundle() is not None)
        clock.advance(31);self.assertEqual(cache.read()[0],200)
        wait_until(lambda:cache.status()['status']=='failed')
        for _ in range(8):self.assertEqual(cache.read()[0],200)
        self.assertEqual(len(calls),2)
        clock.advance(301)
        self.assertIsNone(cache.get_display_bundle())
        self.assertNotEqual(cache.read()[0],200)
        cache.shutdown()

    def test_idle_refresh_prepares_page_before_navigation_and_stops_on_shutdown(self):
        clock=FakeClock();idle=[False];calls=[]
        def build(phase):calls.append(1);return self.bundle()
        cache=ReviewPageCache(build,lambda:'key',clock=clock)
        cache.warm(lambda:idle[0],interval=.01)
        self.assertEqual(calls,[])
        idle[0]=True;wait_until(lambda:cache.get_bundle() is not None)
        self.assertEqual(len(calls),1)
        clock.advance(31);wait_until(lambda:len(calls)==2 and cache.get_bundle() is not None)
        cache.shutdown();cache._maintenance.join(1)
        self.assertFalse(cache._maintenance.is_alive())


class IncrementalHistoryTests(history_tests.HistoryTests):
    # Inherited safety cases run against the incremental reader as well.
    def test_unchanged_completed_jobs_reuse_projection_without_parsing_receipts(self):
        done=self.complete(move=True);expected=self.snapshot();history=self.service.history
        with patch.object(history,'_item',wraps=history._item) as project:
            self.assertEqual(self.snapshot(),expected)
            self.assertEqual(project.call_count,0)
        receipt=self.service.state/'units'/done['outcomes'][0]['receipt']
        data=json.loads(receipt.read_text());data['status']='incomplete';atomic_json(receipt,data)
        with patch.object(history,'_item',wraps=history._item) as project:
            result=self.snapshot()
            self.assertGreater(project.call_count,0)
        self.assertEqual(len(result['archived_units']),1)

    def test_removed_job_is_not_served_from_projection(self):
        done=self.complete();self.snapshot()
        (self.service.state/'requests'/('job-'+done['job_id']+'.json')).unlink()
        self.assertEqual(self.snapshot()['archived_units'],[])

    def test_projection_budget_is_bounded(self):
        self.complete()
        with patch('muli_sorter.archive_history.PROJECTED_CACHE_UNITS',1):self.snapshot()
        self.assertLessEqual(self.service.history.projected_units,1)

    def test_changing_journal_during_projection_is_not_cached(self):
        done=self.complete(move=True);path=self.service.state/'requests'/('move-'+done['job_id']+'.json')
        history=self.service.history;original=history._item;calls=[]
        def project(*args,**kwargs):
            item=original(*args,**kwargs);calls.append(1)
            if len(calls)==1:
                journal=json.loads(path.read_text())
                for row in journal['files']:row['state']='pending'
                atomic_json(path,journal)
            return item
        with patch.object(history,'_item',side_effect=project):self.snapshot()
        self.assertEqual(self.snapshot()['archived_units'],[])
