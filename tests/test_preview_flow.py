import json
from pathlib import Path
import tempfile
import threading
import time
import hashlib
import sqlite3
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.request import Request,urlopen
from urllib.error import HTTPError
from test_archive_jobs import Fixture
from muli_sorter.archive_console import make_server
from muli_sorter.photo_previews import PhotoPreviews
from muli_sorter.photo_preview_index import PhotoPreviewIndex,selected_photos
from muli_sorter.material_triage import build_material_state
from muli_sorter.photo_preview_warmup import PhotoWarmup
from muli_sorter.video_previews import extract,generate_unit

class IndexTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.index=PhotoPreviewIndex(Path(self.temp.name).resolve()/'index');self.addCleanup(self.index.close)
        self.units=[{'unit_id':f'u{i}','kind':'photo','capture_time':f'2026-10-05T10:{i:02d}:00+08:00','files':[{'source_path':f'{i}.jpg','size_bytes':1,'blake3':'a'*64}]} for i in range(20)]
        self.model={'report_id':'report','units':self.units,'initial_segments':[{'unit_ids':[u['unit_id'] for u in self.units]}]}
    def test_bounded_six_lookup_and_persistent_restore(self):
        d=self.index.build(self.model)
        self.assertEqual([u['unit_id'] for u in selected_photos(self.units)],['u0','u4','u8','u11','u15','u19'])
        self.assertEqual(self.index.priority_ids(d),['u0','u4','u8','u11','u15','u19'])
        sql=[];self.index.connection.set_trace_callback(sql.append)
        self.assertEqual(len(self.index.lookup(d,'report',['u4','u19'])),2)
        self.assertTrue(any('where uid in' in q for q in sql));self.assertFalse(any('select *' in q for q in sql))
        self.index.close();other=PhotoPreviewIndex(self.index.root);self.addCleanup(other.close)
        self.assertEqual(other.lookup(d,'report',['u0'])[0]['files'][0]['source_path'],'0.jpg')
    def test_stale_unknown_duplicate_and_changed_index_rejected(self):
        d=self.index.build(self.model)
        for report,ids in [('old',['u0']),('report',['unknown']),('report',['u0','u0']),('report',[])]:
            with self.assertRaises(ValueError):self.index.lookup(d,report,ids)
        path=self.index.root/d['file'];raw=path.read_bytes();path.write_bytes(raw+b'x')
        with self.assertRaises(ValueError):self.index.lookup(d,'report',['u0'])
    def test_symlink_index_and_parent_escaping_rejected(self):
        d=self.index.build(self.model);path=self.index.root/d['file'];original=path.with_suffix('.original');path.rename(original);path.symlink_to(original)
        with self.assertRaises(ValueError):self.index.lookup(d,'report',['u0'])
        with self.assertRaises(ValueError):self.index.lookup(dict(d,file='../bad.sqlite'),'report',['u0'])
    def test_only_pending_photos_registered(self):
        self.model['units']=self.units[:2];self.model['initial_segments'][0]['unit_ids']=[u['unit_id'] for u in self.units]
        d=self.index.build(self.model);self.assertEqual(d['count'],2)
        with self.assertRaises(ValueError):self.index.lookup(d,'report',['u19'])
    def test_damaged_existing_index_rebuilt_and_semantic_tamper_rejected(self):
        d=self.index.build(self.model);path=self.index.root/d['file']
        path.write_bytes(b'broken sqlite')
        d=self.index.build(self.model)
        self.assertEqual(self.index.lookup(d,'report',['u0'])[0]['unit_id'],'u0')
        self.index.close()
        conn=sqlite3.connect(path);conn.execute('delete from priority where ordinal=2');conn.commit();conn.close()
        d['sha256']=hashlib.sha256(path.read_bytes()).hexdigest()
        with self.assertRaises(ValueError):self.index.activate(d)
    def test_final_close_cannot_be_reopened_by_background_builder(self):
        d=self.index.build(self.model);self.index.close(final=True)
        with self.assertRaises(ValueError):self.index.activate(d)
        with self.assertRaises(ValueError):self.index.build(self.model)
    def test_background_queue_deferral_retries_without_losing_samples(self):
        d=self.index.build(self.model)
        calls=[];retried=threading.Event()
        def request(units,**kwargs):
            ids=[u['unit_id'] for u in units];calls.append(ids)
            if len(calls)==1:return {'previews':{uid:{'state':'pending'} for uid in ids},'deferred':ids[-2:]}
            retried.set();return {'previews':{uid:{'state':'ready'} for uid in ids},'deferred':[]}
        photos=SimpleNamespace(queue=SimpleNamespace(qsize=lambda:0),scope_token=None,request_units=request,lock=threading.Lock())
        warm=PhotoWarmup(self.index,photos);self.addCleanup(warm.close);warm.start_scope(d)
        self.assertTrue(retried.wait(2));self.assertEqual(calls[0],calls[1])

class FlowTests(Fixture,unittest.TestCase):
    def setUp(self):
        super().setUp();self.queue=self.root/'queue';self.queue.mkdir();(self.queue/'队列状态.json').write_text('{}')
        self.calls=[]
        def extract(*args):self.calls.append(1);return '照片预览',b'\xff\xd8test\xff\xd9'
        self.photos=PhotoPreviews(self.root/'staging',self.root/'photos',extractor=extract)
        self.servers=[]
    def tearDown(self):
        for s in self.servers:s.shutdown();s.server_close()
        self.photos.close();super().tearDown()
    def server(self):
        s=make_server(self.service,self.queue,photo_previews=self.photos);self.servers.append(s)
        threading.Thread(target=s.serve_forever,daemon=True).start()
        s.page_cache.read()
        for i in range(500):
            if s.page_cache.get_bundle():return s
            time.sleep(.01)
        self.fail('page never ready')
    def call(self,s,ids):
        body={'report_id':self.model['report_id'],'unit_ids':ids}
        req=Request(s.public_origin+'/api/photo-previews',data=json.dumps(body).encode(),headers={'Content-Type':'application/json','Origin':s.public_origin,'X-Muli-Request':'1'})
        with urlopen(req) as r:return json.load(r)
    def test_prewarms_without_browser_and_hot_restore_never_loads_full_model(self):
        s=self.server()
        for i in range(100):
            if s.photo_warmup.finished and self.photos.queue.unfinished_tasks==0:break
            time.sleep(.02)
        self.assertTrue(self.calls,'must generate ahead of photo requests')
        ids=[u['unit_id'] for u in self.model['units'] if u['kind']=='photo'][:6]
        count=len(self.calls)
        with patch.object(self.service,'model',side_effect=AssertionError('full model hot request')),patch('muli_sorter.photo_previews.source_fd',side_effect=AssertionError('original source inspected on presentation')):
            value=self.call(s,ids);self.assertTrue(all(v['state']=='ready' for v in value['previews'].values()))
        with patch.object(self.service,'model',side_effect=AssertionError('full model restored display')):
            s.shutdown();s.server_close();two=self.server();self.assertTrue(all(v['state']=='ready' for v in self.call(two,ids)['previews'].values()))
        self.assertEqual(len(self.calls),count)
        with urlopen(two.public_origin+'/api/review-page') as response:html=response.read().decode()
        ready=json.loads(html.split('<script id="photo-preview-ready" type="application/json">')[1].split('</script>')[0])
        self.assertTrue(all(ready[uid]['state']=='ready' for uid in ids))
    def test_archive_hides_units_from_preview_index_and_skips_source_inspection(self):
        job=self.submit();self.service.run_job(job['job_id'])
        archive=self.service.history.snapshot(self.model)
        hidden={r['unit_id'] for r in archive['archived_units'] if r['in_current_model']}
        archived_paths={f['source_path'] for u in self.model['units'] if u['unit_id'] in hidden for f in u['files']}
        from muli_sorter.material_triage import media_stat
        def guard(root,path,size):
            self.assertNotIn(path,archived_paths,'display inspected archived source')
            return media_stat(root,path,size)
        with patch('muli_sorter.material_triage.media_stat',side_effect=guard):s=self.server()
        pending_photos=[u for u in self.model['units'] if u['kind']=='photo' and u['unit_id'] not in hidden]
        self.assertEqual(s.photo_index.descriptor['count'],len(pending_photos))
        uid=next(iter(hidden))
        with self.assertRaises(HTTPError):self.call(s,[uid])
        with patch('muli_sorter.material_triage.media_stat',side_effect=guard):build_material_state(self.model,self.root/'staging',skip_source_ids=hidden)
    def test_business_preflight_still_reads_live_model_and_source(self):
        self.server()
        with patch.object(self.service,'model',wraps=self.service.model) as live:
            result=self.service.preflight(self.decisions)
            self.assertGreater(live.call_count,0);self.assertEqual(result['status'],'ready')

class VideoFaultTests(unittest.TestCase):
    def test_empty_video_stream_is_bounded_error_not_worker_crash(self):
        with tempfile.TemporaryDirectory() as folder:
            source=Path(folder)/'clip.mp4';source.write_bytes(b'bad media')
            with source.open('rb') as f,patch('muli_sorter.video_previews.subprocess.run',return_value=SimpleNamespace(stdout=b'{"streams": [], "format": {}}')) as probe:
                with self.assertRaisesRegex(ValueError,'no_video_stream'):extract(f.fileno(),{'source_path':'clip.mp4'})
                self.assertEqual(probe.call_count,1)
            unit={'unit_id':'u','files':[{'source_path':'clip.mp4','size_bytes':source.stat().st_size,'blake3':'a'*64}]}
            with patch('muli_sorter.video_previews.subprocess.run',return_value=SimpleNamespace(stdout=b'{"streams": []}')):
                entry=generate_unit(folder,Path(folder)/'cache',unit)
            self.assertEqual(entry['state'],'error')

if __name__=='__main__':unittest.main()
