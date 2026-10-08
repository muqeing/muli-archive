import json
import threading
import unittest
from urllib.request import Request,urlopen
from urllib.error import HTTPError
from test_archive_jobs import Fixture
from muli_sorter.archive_console import make_server
from muli_sorter.photo_previews import PhotoPreviews


class PhotoHTTPTests(Fixture,unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.photos=PhotoPreviews(self.root/'staging',self.root/'photos',extractor=lambda *_:('照片预览',b'\xff\xd8test\xff\xd9'))
        self.server=make_server(self.service,self.root/'queue',photo_previews=self.photos)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.origin=self.server.public_origin
        self.ids=[u['unit_id'] for u in self.model['units'] if u['kind']=='photo'][:6]

    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join(2);self.photos.close()
        super().tearDown()

    def call(self,body,**headers):
        req=Request(self.origin+'/api/photo-previews',data=json.dumps(body).encode(),headers={'Content-Type':'application/json','Origin':self.origin,'X-Muli-Request':'1',**headers})
        with urlopen(req,timeout=10) as r:return json.load(r)

    def test_preview_route_does_not_submit_or_create_projects(self):
        before=sorted(str(p.relative_to(self.root/'projects')) for p in (self.root/'projects').rglob('*'))
        body={'report_id':self.model['report_id'],'unit_ids':self.ids}
        self.call(body);self.photos.queue.join();response=self.call(body)
        for entry in response['previews'].values():
            self.assertEqual(entry['state'],'ready')
            with urlopen(self.origin+'/'+entry['src']) as r:self.assertEqual(r.read(),b'\xff\xd8test\xff\xd9')
        self.assertEqual(self.service.list_jobs(),[])
        self.assertEqual(before,sorted(str(p.relative_to(self.root/'projects')) for p in (self.root/'projects').rglob('*')))

    def test_cross_origin_stale_and_path_payload_are_rejected(self):
        body={'report_id':self.model['report_id'],'unit_ids':self.ids}
        with self.assertRaises(HTTPError) as err:self.call(body,Origin='http://evil.example')
        self.assertEqual(err.exception.code,403)
        for bad in [dict(body,report_id='old'),dict(body,unit_ids=['../../file']),dict(body,path='/staging')]:
            with self.assertRaises(HTTPError) as err:self.call(bad)
            self.assertIn(err.exception.code,[404,409])
        self.assertEqual(list(self.photos.output.iterdir()),[])
