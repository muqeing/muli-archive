import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from muli_sorter.order_feed_io import digest
from muli_sorter.video_previews import signature
from muli_sorter.video_pending_scope import publish_scope,PendingVideoWorker


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

if __name__=='__main__':unittest.main()
