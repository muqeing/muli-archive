import copy
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from muli_sorter.order_feed_io import digest
from muli_sorter.video_previews import signature
from muli_sorter.video_pending_scope import publish_scope,PendingVideoWorker,VideoDisplayCache
from muli_sorter.review import identity_digest, legacy_identity_digest
from muli_sorter.order_feed_io import atomic_json


class PendingTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve();self.staging=self.root/'staging';self.staging.mkdir()
        self.output=self.root/'output';self.output.mkdir();self.scope=self.root/'scope'
        self.unit={'unit_id':'u1','kind':'video','files':[{'source_path':'one.mp4','size_bytes':4,'blake3':'a'*64}]}
        (self.staging/'one.mp4').write_bytes(b'demo')
        self.view={'report_id':'canonical','schema_version':'0.2','snapshot_at':'2026-10-05T00:00:00+00:00',
            'example_data':True,'units':[self.unit], 'initial_segments':[{'segment_id':'s1','unit_ids':['u1']}],'projects':[]}
        self.calls=[]
        def extract(fd,file):
            self.calls.append(file['source_path'])
            return [(1,b'\xff\xd8demo\xff\xd9')],signature(fd)
        self.worker=PendingVideoWorker(self.scope/'current.json',self.staging,self.output,extractor=extract)
    def test_unchanged_scope_idle_never_reopens_original_or_full_model(self):
        publish_scope(self.scope,self.view)
        self.assertTrue(self.worker.step());self.assertEqual(self.calls,['one.mp4'])
        with patch('muli_sorter.video_previews.source_fd',side_effect=AssertionError('re-read original')),patch('muli_sorter.video_pending_scope.read_json',side_effect=AssertionError('reload scope')):
            for _ in range(20):self.assertFalse(self.worker.step())
    def test_archived_removed_from_scope_has_no_source_recheck(self):
        publish_scope(self.scope,self.view);self.worker.step()
        self.view['units']=[]
        publish_scope(self.scope,self.view)
        with patch('muli_sorter.video_previews.source_fd',side_effect=AssertionError('archived source reopened')):
            self.assertFalse(self.worker.step())
        self.assertEqual(json.loads((self.output/'index.json').read_text())['entries'],{})
    def test_tampered_missing_and_symlink_scope_never_falls_back(self):
        with self.assertRaises(FileNotFoundError):self.worker.step()
        publish_scope(self.scope,self.view)
        p=self.scope/'current.json';data=json.loads(p.read_text());data['model']['units']=[];p.write_text(json.dumps(data))
        with self.assertRaises(ValueError):self.worker.step()
        p.unlink();p.symlink_to(self.staging/'one.mp4')
        with self.assertRaises(ValueError):self.worker.step()
        self.assertEqual(self.calls,[])
    def test_bad_clip_does_not_block_following_clip_and_does_not_spin(self):
        other=copy.deepcopy(self.unit);other['unit_id']='u2';other['files'][0]['source_path']='two.mp4';(self.staging/'two.mp4').write_bytes(b'demo')
        self.view['units'].append(other);self.view['initial_segments'][0]['unit_ids'].append('u2')
        publish_scope(self.scope,self.view);original=self.worker.extractor
        def extract(fd,file):
            if file['source_path']=='one.mp4':raise ValueError('no video stream')
            return original(fd,file)
        self.worker.extractor=extract
        self.assertTrue(self.worker.step());self.assertTrue(self.worker.step());self.assertFalse(self.worker.step())
        self.assertEqual(self.worker.index['entries']['u1']['state'],'error');self.assertEqual(self.worker.index['entries']['u2']['state'],'ready')
    def test_scope_change_during_decode_discards_old_index_publication(self):
        publish_scope(self.scope,self.view);original=self.worker.extractor
        def extract(fd,file):
            result=original(fd,file);view=copy.deepcopy(self.view);view['units']=[];publish_scope(self.scope,view);return result
        self.worker.extractor=extract;self.worker.step();self.assertFalse(self.worker.step())
        self.assertEqual(self.worker.index['entries'],{})

    def test_new_and_legacy_scope_identity_generate_and_reuse_cached_frames(self):
        for recipe in (identity_digest, legacy_identity_digest):
            publish_scope(self.scope,self.view)
            data=json.loads((self.scope/'current.json').read_text())
            data['model']['report_id']=recipe(data['model'])
            data['scope_id']=digest({k:v for k,v in data.items() if k!='scope_id'})
            atomic_json(self.scope/'current.json',data)
            self.assertTrue(self.worker.step())
            self.assertEqual(self.worker.index['entries']['u1']['state'],'ready')
        self.assertEqual(self.calls,['one.mp4'])

    def test_rebound_tampered_model_is_rejected_and_records_safe_error(self):
        publish_scope(self.scope,self.view)
        data=json.loads((self.scope/'current.json').read_text())
        data['model']['units'][0]['files'][0]['blake3']='b'*64
        data['scope_id']=digest({k:v for k,v in data.items() if k!='scope_id'})
        atomic_json(self.scope/'current.json',data)
        self.assertEqual(self.worker.run(threading.Event(),once=True),1)
        state=json.loads((self.output/'worker-state.json').read_text())
        self.assertEqual(state['error_code'],'model_invalid')
        self.assertEqual(self.calls,[])

    def test_display_distinguishes_waiting_and_fault_without_opening_media(self):
        descriptor=publish_scope(self.scope,self.view)
        self.worker.refresh()
        display=VideoDisplayCache(self.scope/'current.json',self.output)
        with patch('muli_sorter.video_previews.source_fd',side_effect=AssertionError('display reopened media')):
            entry=display.read(descriptor)['u1']
            self.assertEqual(entry['state'],'pending')
            self.assertEqual(entry['source_name'],'one.mp4')
            atomic_json(self.output/'worker-state.json',{'state':'waiting_pending_scope','error_code':'model_invalid'})
            self.assertEqual(display.read(descriptor)['u1']['state'],'error')
            atomic_json(self.output/'worker-state.json',{'state':'running','scope_id':descriptor['scope_id']})
            self.assertEqual(display.read(descriptor)['u1']['state'],'pending')

    def test_worker_status_recovers_after_invalid_scope(self):
        descriptor=publish_scope(self.scope,self.view)
        path=self.scope/'current.json';valid=path.read_bytes();path.write_text('{}')
        self.assertEqual(self.worker.run(threading.Event(),once=True),1)
        path.write_bytes(valid)
        self.assertEqual(self.worker.run(threading.Event(),once=True),0)
        state=json.loads((self.output/'worker-state.json').read_text())
        self.assertEqual(state['state'],'ready')
        self.assertEqual(state['scope_id'],descriptor['scope_id'])
        self.assertNotIn('error_code',state)

    def test_ready_preview_survives_unavailable_scope_worker(self):
        descriptor=publish_scope(self.scope,self.view);self.worker.step()
        atomic_json(self.output/'worker-state.json',{'state':'waiting_pending_scope','error_code':'scope_unavailable'})
        self.assertEqual(VideoDisplayCache(self.scope/'current.json',self.output).read(descriptor)['u1']['state'],'ready')

    def test_decoder_upgrade_retries_legacy_error_without_rebuilding_ready_cache(self):
        publish_scope(self.scope,self.view);self.worker.refresh()
        self.worker.index['entries']['u1']={'state':'error','frames':[],'retry_after':10**12}
        self.assertTrue(self.worker.step())
        self.assertEqual(self.worker.index['entries']['u1']['state'],'ready')
        self.assertEqual(self.calls,['one.mp4'])
        self.assertFalse(self.worker.step())

if __name__=='__main__':unittest.main()
