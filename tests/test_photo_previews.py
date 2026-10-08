from copy import deepcopy
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from PIL import Image
from blake3 import blake3
from muli_sorter.photo_previews import PhotoPreviews, extract, key
from muli_sorter.photo_decode import decode
from muli_sorter.video_previews import source_fd


class PhotoPreviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name).resolve();self.staging=self.root/'source';self.staging.mkdir()
        self.source=self.staging/'photo.jpg'
        image=Image.new('RGB',(120,80),'#325f38');exif=Image.Exif();exif[274]=6
        image.save(self.source,exif=exif)
        data=self.source.read_bytes()
        self.unit={'unit_id':'photo-1','kind':'photo','files':[{'source_path':'photo.jpg','size_bytes':len(data),'blake3':blake3(data).hexdigest()}]}
        self.model={'report_id':'test-report','units':[self.unit]}
        self.service=PhotoPreviews(self.staging,self.root/'cache')
        self.addCleanup(self.service.close)

    def request(self):
        return self.service.request(self.model,'test-report',['photo-1'])['previews']['photo-1']

    def test_real_jpeg_rotation_cache_restart_and_source_unchanged(self):
        before=self.source.stat();original=self.source.read_bytes()
        self.request();self.service.queue.join();ready=self.request()
        self.assertEqual(ready['state'],'ready')
        with Image.open(self.root/'cache'/Path(ready['src']).name) as image:
            self.assertEqual(image.size,(80,120));self.assertEqual(image.getexif().get(274),None)
        self.service.close()
        self.service=PhotoPreviews(self.staging,self.root/'cache',extractor=lambda *_:self.fail('cache was not reused'))
        self.addCleanup(self.service.close)
        self.assertEqual(self.request()['state'],'ready')
        self.assertEqual(self.source.read_bytes(),original)
        self.assertEqual(self.source.stat().st_mtime_ns,before.st_mtime_ns)

    def test_large_jpeg_is_downsampled_before_rotation(self):
        with Image.new('RGB',(9504,6336),'#758973') as image:
            exif=Image.Exif();exif[274]=6;image.save(self.source,exif=exif)
        file=self.unit['files'][0];file['size_bytes']=self.source.stat().st_size
        with source_fd(self.staging,file) as fd:label,data=extract(fd,file)
        with Image.open(io.BytesIO(data)) as preview:
            self.assertLessEqual(max(preview.size),720);self.assertGreater(preview.height,preview.width)

    def test_reject_unknown_stale_more_than_six_duplicate_or_nonphoto(self):
        for report,ids in [('old',['photo-1']),('test-report',['unknown']),('test-report',[]),('test-report',['photo-1']*7),('test-report',['photo-1']*2)]:
            with self.subTest(ids=ids),self.assertRaises(ValueError):self.service.request(self.model,report,ids)
        self.unit['kind']='video'
        with self.assertRaises(ValueError):self.request()
        self.assertEqual(list((self.root/'cache').iterdir()),[])

    def test_symlink_and_traversal_never_read(self):
        original=self.source.read_bytes();self.source.unlink();self.source.symlink_to(self.root/'other')
        (self.root/'other').write_bytes(original)
        self.request();self.service.queue.join();self.assertEqual(self.request()['state'],'error')
        self.unit['files'][0]['source_path']='../other'
        self.request();self.service.queue.join();self.assertEqual(self.request()['state'],'error')
        self.assertEqual(list((self.root/'cache').glob('*.jpg')),[])

    def test_changed_source_or_corrupt_cache_does_not_reuse(self):
        self.request();self.service.queue.join();ready=self.request()
        cached=self.root/'cache'/Path(ready['src']).name;cached.write_bytes(b'bad')
        self.assertIsNone(self.service._cached(self.unit))
        self.request();self.service.queue.join()
        # A new mtime invalidates cache even if size and path stay the same.
        os.utime(self.source,ns=(self.source.stat().st_atime_ns,self.source.stat().st_mtime_ns+1000000))
        self.assertIsNone(self.service._cached(self.unit))

    def test_changed_during_decode_publishes_nothing_and_failure_backs_off(self):
        calls=[]
        def mutate(fd,file):
            calls.append(1);self.source.write_bytes(b'x'*file['size_bytes'])
            return '照片预览',b'\xff\xd8hello\xff\xd9'
        self.service.extractor=mutate
        self.request();self.service.queue.join();self.assertEqual(self.request()['state'],'error')
        self.assertEqual(len(calls),1);self.assertEqual(list((self.root/'cache').glob('*.jpg')),[])

    def test_raw_embedded_preview_avoids_unpack_and_uses_orientation(self):
        import rawpy
        fake=SimpleNamespace(open_file=lambda _:None,extract_thumb=lambda:SimpleNamespace(format=rawpy.ThumbFormat.JPEG,data=self.source.read_bytes()),sizes=SimpleNamespace(flip=6),unpack=lambda:self.fail('embedded JPEG should not unpack RAW'))
        from unittest.mock import MagicMock
        ctx=MagicMock();ctx.__enter__.return_value=fake
        with patch('rawpy.RawPy',return_value=ctx),source_fd(self.staging,self.unit['files'][0]) as fd:
            label,data=decode(fd,True)
        self.assertEqual(label,'RAW 内嵌预览');self.assertEqual(Image.open(io.BytesIO(data)).size,(80,120))

    def test_raw_without_thumbnail_decodes_half_size(self):
        import rawpy,numpy as np
        from unittest.mock import MagicMock
        raw=MagicMock();raw.extract_thumb.side_effect=rawpy.LibRawNoThumbnailError('none')
        raw.postprocess.return_value=np.zeros((40,60,3),dtype=np.uint8)
        ctx=MagicMock();ctx.__enter__.return_value=raw
        with patch('rawpy.RawPy',return_value=ctx),source_fd(self.staging,self.unit['files'][0]) as fd:
            label,data=decode(fd,True)
        self.assertEqual(label,'RAW 解码预览');raw.unpack.assert_called_once()
        raw.postprocess.assert_called_once_with(half_size=True,use_camera_wb=True,output_bps=8)
        self.assertEqual(Image.open(io.BytesIO(data)).size,(60,40))
