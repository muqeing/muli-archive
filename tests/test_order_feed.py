from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from muli_sorter.order_catalog import OrderError
from muli_sorter.order_feed import OrderFeedWorker, build_feed, utc, nas_cli_reader, folder_namer
from muli_sorter.order_feed_io import atomic_json, read_json, digest
from muli_sorter.order_feed_view import apply_feed
from muli_sorter.review import build_review_model
from test_review import sample_report
from test_order_preview import _reader_factory, _namer
import test_discovery_queue as queue_fixture
from muli_sorter.queue_view import export


class OrderFeedTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.queue, self.projects = self.root/'queue', self.root/'projects'
        self.queue.mkdir(); self.projects.mkdir()
        self.now = 1790574000.0
        self.clock = lambda: self.now
        self.model = build_review_model(sample_report())
        self.resource = {'base_token':'SYNTHETIC', 'table_name':'orders', 'product_table_id':'products'}
        self.calls = []
        self.reader = _reader_factory(calls=self.calls)
        self.install_model(self.model)

    def tearDown(self):
        self.temp.cleanup()

    def install_model(self, model):
        parent = self.queue/'combined'/model['report_id']
        parent.mkdir(parents=True, exist_ok=True)
        atomic_json(parent/'确认模型.json', model)
        atomic_json(self.queue/'队列状态.json', {'observer_stopped':False,
            'source':{'ok':True,'checked_at':utc(self.now)},
            'base_model_path':f"combined/{model['report_id']}/确认模型.json"})

    def worker(self, reader=None):
        return OrderFeedWorker(self.queue, self.projects, self.resource, reader or self.reader,
                               _namer, clock=self.clock)

    def step(self, worker):
        with patch('muli_sorter.order_feed.project_index', return_value=self.model['projects']):
            return worker.step()

    def current(self):
        return read_json(self.queue/'order-feed/current.json')

    def test_initial_refresh_idle_and_restart_do_not_repeat_api_calls(self):
        self.assertEqual(self.step(self.worker())['state'],'ready')
        count = len(self.calls)
        self.now += 10; self.install_model(self.model)
        self.assertEqual(self.step(self.worker())['state'],'ready')
        self.assertEqual(len(self.calls),count)
        enriched,status = apply_feed(self.queue,self.model,now=self.now)
        self.assertEqual(status['state'],'ready')
        self.assertTrue(all(s['decision']=='pending' for s in enriched['initial_segments']))
        self.assertTrue(any(p.get('exists') is False for p in enriched['projects']))

    def test_unchanged_orders_preserve_report_id_and_drafts_after_refresh(self):
        self.step(self.worker()); first = self.current()
        enriched,_ = apply_feed(self.queue,self.model,now=self.now)
        self.now += 301; self.install_model(self.model)
        self.step(self.worker()); second = self.current()
        refreshed,_ = apply_feed(self.queue,self.model,now=self.now)
        self.assertEqual(first['content_id'],second['content_id'])
        self.assertNotEqual(first['verified_at'],second['verified_at'])
        self.assertEqual(enriched['report_id'],refreshed['report_id'])

    def test_meaningful_order_change_changes_report_and_never_inherits_confirmation(self):
        self.step(self.worker()); first = self.current()
        def changed(args):
            result = self.reader(args)
            if args[1]=='+record-list':
                result['data'][0][3] = ['取消']
            return result
        self.now += 301; self.install_model(self.model)
        self.step(self.worker(changed))
        self.assertNotEqual(first['content_id'],self.current()['content_id'])
        enriched,status = apply_feed(self.queue,self.model,now=self.now)
        self.assertEqual(status['folder_intent_counts']['needs_review'],1)
        self.assertTrue(all(s['decision']=='pending' for s in enriched['initial_segments']))

    def test_failed_product_read_publishes_no_partial_result_and_keeps_history(self):
        self.step(self.worker()); before = self.current()
        def failing(args):
            if args[1]=='+record-get': raise OrderError('synthetic unavailable')
            return self.reader(args)
        self.now += 301; self.install_model(self.model)
        worker=self.worker(failing)
        self.assertEqual(self.step(worker)['state'],'error')
        self.assertEqual(self.current(),before)
        calls=len(self.calls)
        self.now += 5
        self.assertEqual(self.step(worker)['state'],'retry_wait')
        self.assertEqual(len(self.calls),calls)
        model,status=apply_feed(self.queue,self.model,now=self.now)
        self.assertIs(model,self.model);self.assertEqual(status['state'],'unavailable')

    def test_source_change_during_read_discards_candidate(self):
        self.step(self.worker()); before=self.current()
        self.now+=301;self.install_model(self.model)
        def race(args):
            data=self.reader(args)
            changed=deepcopy(self.model);changed['snapshot_at']=utc(self.now+1)
            changed['report_id']='sha256:'+digest({k:v for k,v in changed.items() if k!='report_id'})
            self.install_model(changed)
            return data
        self.assertEqual(self.step(self.worker(race))['state'],'error')
        self.assertEqual(self.current(),before)

    def test_stale_source_and_changed_directory_prevent_any_order_call(self):
        self.now+=121
        self.assertEqual(self.step(self.worker())['state'],'error')
        self.assertEqual(self.calls,[])
        self.install_model(self.model)
        with patch('muli_sorter.order_feed.project_index',return_value=[]):
            self.assertEqual(self.worker().step()['state'],'error')
        self.assertEqual(self.calls,[])

    def test_expired_or_mismatched_feed_never_enriches(self):
        self.step(self.worker())
        base,status=apply_feed(self.queue,self.model,now=self.now+601)
        self.assertIs(base,self.model);self.assertEqual(status['state'],'unavailable')
        other=deepcopy(self.model);other['snapshot_at']=utc(self.now+1)
        other['report_id']='sha256:'+digest({k:v for k,v in other.items() if k!='report_id'})
        base,status=apply_feed(self.queue,other,now=self.now)
        self.assertIs(base,other);self.assertEqual(status['state'],'waiting')

    def test_corruption_and_symlinks_are_not_accepted(self):
        self.step(self.worker())
        path=self.queue/'order-feed/current.json';value=read_json(path)
        value['content']['catalog']['orders'].clear();atomic_json(path,value)
        _,status=apply_feed(self.queue,self.model,now=self.now)
        self.assertEqual(status['state'],'unavailable')
        path.unlink();path.symlink_to(self.queue/'队列状态.json')
        with self.assertRaises(OSError):read_json(path)
        with self.assertRaises(ValueError):atomic_json(path,{'bad':True})

    def test_more_than_sixty_dates_and_revision_race_rejected(self):
        model=deepcopy(self.model)
        model['units']=[{'capture_date':f'2020-01-{i:02d}'} for i in range(1,32)]
        model['units'] += [{'capture_date':f'2020-03-{i:02d}'} for i in range(1,32)]
        count=0
        def changed(args):
            nonlocal count
            data=self.reader(args)
            if args[1]=='+record-list':
                count+=1;data['rev']=count
            return data
        with self.assertRaises(OrderError):
            build_feed(model,model['projects'],self.resource,changed,_namer,now=self.now)
        self.assertEqual(count,2)

    def test_native_cli_rejects_auth_or_alternate_identity_without_subprocess(self):
        with patch('muli_sorter.order_feed.subprocess.run') as run:
            for args in (['auth','login'],['base','+record-list','--as','bot']):
                with self.assertRaises(OrderError):nas_cli_reader(args)
            run.assert_not_called()

    def test_no_input_files_change(self):
        before={p.relative_to(self.queue):p.read_bytes() for p in self.queue.rglob('*') if p.is_file()}
        self.step(self.worker())
        self.assertEqual(before,{p:self.queue.joinpath(p).read_bytes() for p in before})
        self.assertEqual(list(self.projects.iterdir()),[])

    def test_source_failure_really_waits_without_reading_source_each_cycle(self):
        worker=self.worker()
        with patch('muli_sorter.order_feed.queue_reference',side_effect=OrderError('offline')) as reference:
            self.assertEqual(worker.step()['state'],'error')
            retry=worker.retry_at
            for _ in range(3):
                self.now+=5
                self.assertEqual(worker.step()['state'],'retry_wait')
            self.assertEqual(reference.call_count,1)
            self.assertEqual(worker.retry_at,retry)

    def test_directory_change_after_remote_read_discards_feed(self):
        with patch('muli_sorter.order_feed.project_index',side_effect=[self.model['projects'],[]]):
            self.assertEqual(self.worker().step()['state'],'error')
        self.assertFalse((self.queue/'order-feed/current.json').exists())

    def test_product_limit_fails_before_any_multi_snapshot_product_query(self):
        with patch('muli_sorter.order_feed.candidate_products',return_value=[f'recPRODUCT{i}' for i in range(201)]):
            with self.assertRaises(OrderError):
                build_feed(self.model,self.model['projects'],self.resource,self.reader,_namer,now=self.now)
        self.assertFalse(any(c[1]=='+record-get' for c in self.calls))

    def test_changed_naming_module_is_not_executed(self):
        target=self.root/'unexpected'
        module=self.root/'namer.py'
        module.write_text('from pathlib import Path\nPath('+repr(str(target))+').write_text("bad")\n')
        with self.assertRaises(ValueError):folder_namer(module,'0'*64)
        self.assertFalse(target.exists())


class QueueOrderIntegrationTests(unittest.TestCase):
    setUp = queue_fixture.QueueTests.setUp
    provider = queue_fixture.QueueTests.provider
    queue = queue_fixture.QueueTests.queue

    def test_orders_change_only_confirmation_view_and_failure_keeps_discovery(self):
        with self.queue() as queue:
            status=queue.tick(self.provider)
            original=export(queue,status)
            base=read_json(queue.root/original['base_model_path'])
            now=__import__('time').time()
            feed=build_feed(base,base['projects'],
                            {'base_token':'SYNTHETIC','table_name':'orders','product_table_id':'products'},
                            _reader_factory(),_namer,now=now)
            atomic_json(queue.root/'order-feed/current.json',feed)
            atomic_json(queue.root/'order-feed/状态.json',{
                'schema_version':'order-feed/0.6','state':'ready','stopped':False,
                'checked_at':utc(now),'last_verified_at':feed['verified_at'],'base_report_id':base['report_id']})
            ready=export(queue,status)
            self.assertEqual(ready['orders']['state'],'ready')
            self.assertNotEqual(ready['confirmation_page'],original['confirmation_page'])
            self.assertEqual(ready['base_model_path'],original['base_model_path'])
            self.assertEqual(ready['summary'],original['summary'])
            again=export(queue,status)
            self.assertEqual(again['confirmation_page'],ready['confirmation_page'])
            atomic_json(queue.root/'order-feed/状态.json',{
                'schema_version':'order-feed/0.6','state':'error','stopped':False,'checked_at':utc(now)})
            unavailable=export(queue,queue.tick(self.provider))
            self.assertEqual(unavailable['orders']['state'],'unavailable')
            self.assertEqual(unavailable['confirmation_page'],original['confirmation_page'])
            self.assertEqual(unavailable['summary'],original['summary'])
            self.assertTrue((queue.root/ready['confirmation_page']).is_file())

if __name__=='__main__':unittest.main()
