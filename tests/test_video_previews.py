import copy
import json
import os
from pathlib import Path
import shutil
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from blake3 import blake3
from muli_sorter.review import canonical,digest,validate_model
from muli_sorter.video_previews import (SCHEMA,RECIPE,build,cached,generate_unit,source_fd,
    signature,identity,load_index,extract,video_units)
from muli_sorter.video_preview_view import preview_map,render_with_previews
from muli_sorter.order_feed_io import atomic_json
from muli_sorter.queue_files import publish


class VideoPreviewTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve()
        self.staging=self.root/'staging';self.staging.mkdir()
        self.output=self.root/'video-previews';self.output.mkdir()
        data=Path(__file__).parents[1].joinpath('src/muli_sorter/demo_assets/synthetic.mp4').read_bytes()
        self.source=self.staging/'clip.mp4';self.source.write_bytes(data)
        file={'source_path':'clip.mp4','name':'clip.mp4','size_bytes':len(data),'blake3':blake3(data).hexdigest()}
        self.unit={'unit_id':'unit-a','kind':'video','files':[file],'file_names':['clip.mp4'],'file_count':1,'bytes':len(data),'candidate_project_ids':[],'provenance':[]}
        self.model={'schema_version':'0.2','snapshot_at':'2026-09-28T00:00:00+00:00','example_data':True,'projects':[],
            'units':[self.unit], 'initial_segments':[{'segment_id':'segment-a','label':'demo','unit_ids':['unit-a']}],'excluded_batches':[]}
        self.model['report_id']='sha256:'+digest(self.model)
        self.calls=0
    def fake(self,fd,file):
        self.calls+=1
        return [(1,b'\xff\xd8frame1\xff\xd9'),(2,b'\xff\xd8frame2\xff\xd9'),(3,b'\xff\xd8frame3\xff\xd9')],signature(fd)
    def ready(self):
        return build(self.model,self.staging,self.output,extractor=self.fake)
    def test_cached_reuse_and_model_identity_survives_progress(self):
        before=canonical(self.model)
        first=self.ready();self.assertEqual(self.calls,1)
        self.assertTrue(cached(self.output,first['entries']['unit-a'],self.unit,self.staging))
        self.ready();self.assertEqual(self.calls,1)
        self.assertEqual(canonical(self.model),before);validate_model(self.model)
    def test_truncated_cache_is_regenerated(self):
        first=self.ready();entry=first['entries']['unit-a']
        (self.output/entry['frames'][0]['file']).write_bytes(b'bad')
        self.assertFalse(cached(self.output,entry,self.unit,self.staging))
        self.ready();self.assertEqual(self.calls,2)
    def test_readonly_source_and_no_symlink_following(self):
        before=self.source.stat();entry=generate_unit(self.staging,self.output,self.unit,self.fake)
        after=self.source.stat();self.assertEqual((before.st_size,before.st_mtime_ns),(after.st_size,after.st_mtime_ns))
        self.source.unlink();self.source.symlink_to(self.root/'outside')
        (self.root/'outside').write_bytes(b'x'*self.unit['files'][0]['size_bytes'])
        entry=generate_unit(self.staging,self.output,self.unit,self.fake)
        self.assertEqual(entry['state'],'error');self.assertEqual(self.calls,1)
    def test_source_changed_during_extraction_publishes_no_frames(self):
        def mutate(fd,file):
            old=signature(fd);self.source.write_bytes(b'x'*file['size_bytes'])
            return [(1,b'\xff\xd8frame\xff\xd9')],old
        entry=generate_unit(self.staging,self.output,self.unit,mutate)
        self.assertEqual(entry['state'],'error');self.assertEqual(list(self.output.glob('*.jpg')),[])
    def test_proxy_failure_falls_back_to_verified_main_file(self):
        proxy=self.staging/'clip.lrf';proxy.write_bytes(b'bad')
        self.unit['files'].append({'source_path':'clip.lrf','name':'clip.lrf','size_bytes':3,'blake3':blake3(b'bad').hexdigest()})
        seen=[]
        def probe(fd,file):
            seen.append(file['name'])
            if file['name'].endswith('.lrf'):raise ValueError('bad proxy')
            return self.fake(fd,file)
        entry=generate_unit(self.staging,self.output,self.unit,probe)
        self.assertEqual(seen,['clip.lrf','clip.mp4']);self.assertEqual(entry['state'],'ready');self.assertEqual(entry['source_label'],'主视频')
    def test_failed_preview_does_not_raise_and_waits_before_retry(self):
        def fail(fd,file):self.calls+=1;raise ValueError('bad')
        first=build(self.model,self.staging,self.output,extractor=fail)
        second=build(self.model,self.staging,self.output,extractor=fail)
        self.assertEqual(self.calls,1);self.assertEqual(second['entries']['unit-a']['state'],'error')
    def test_view_rejects_other_media_identity_and_path_escape(self):
        index=self.ready();view=preview_map(self.output,self.model,prefix='video-previews/')
        self.assertEqual(len(view['unit-a']['frames']),3)
        index['entries']['unit-a']['frames'][0]['file']='../../source.mp4'
        atomic_json(self.output/'index.json',index)
        self.assertEqual(len(preview_map(self.output,self.model)['unit-a']['frames']),2)
        index['entries']['unit-a']['source']['blake3']='f'*64
        atomic_json(self.output/'index.json',index)
        self.assertEqual(preview_map(self.output,self.model),{})
    def test_new_frames_update_same_report_page_and_preserve_model(self):
        queue=SimpleNamespace(root=self.root,bundle_cache={})
        relative='combined/'+self.model['report_id']
        html=render_with_previews(queue,self.model)
        files={'确认模型.json':canonical(self.model),'拍摄段确认.html':html}
        publish(self.root,relative,files,cache=queue.bundle_cache)
        before=(self.root/relative/'确认模型.json').read_bytes()
        self.ready()
        files['拍摄段确认.html']=render_with_previews(queue,self.model)
        publish(self.root,relative,files,cache=queue.bundle_cache)
        self.assertNotEqual(html,(self.root/relative/'拍摄段确认.html').read_bytes())
        self.assertEqual(before,(self.root/relative/'确认模型.json').read_bytes())
    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'),'local ffmpeg unavailable')
    def test_real_decoder_three_jpegs_from_synthetic_video(self):
        with source_fd(self.staging,self.unit['files'][0]) as fd:
            images,before=extract(fd,self.unit['files'][0])
            self.assertEqual(signature(fd),before)
        self.assertEqual(len(images),3)
        for position,data in images:self.assertTrue(data.startswith(b'\xff\xd8') and data.endswith(b'\xff\xd9'))

if __name__=='__main__':unittest.main()
