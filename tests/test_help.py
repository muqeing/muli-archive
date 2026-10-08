import threading, unittest
from pathlib import Path
from urllib.request import build_opener, ProxyHandler, Request
from urllib.error import HTTPError
from unittest.mock import patch
from test_archive_jobs import Fixture
from muli_sorter.archive_console import make_server
from muli_sorter.review_render import render_review
from muli_sorter.page_loading import loading_page

class HelpTests(Fixture,unittest.TestCase):
 def setUp(self):
  super().setUp(); self.server=make_server(self.service,self.root)
  self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
  self.client=build_opener(ProxyHandler({}))
 def tearDown(self):
  self.server.shutdown();self.server.server_close();self.thread.join(2);super().tearDown()
 def read(self,path,**headers):
  return self.client.open(Request(self.server.public_origin+path,headers=headers),timeout=3)
 def test_document_and_all_images_without_reading_media(self):
  with patch.object(self.service,'provider',side_effect=AssertionError('Media provider must not be called')) as provider:
   with self.read('/help/') as r:
    html=r.read();self.assertIn('图文操作指南'.encode(),html);self.assertNotIn(b'<script',html)
   for n in range(1,7):
    with self.read('/help/step-'+str(n)+'.png') as r:
     self.assertEqual(r.headers['Content-Type'],'image/png');self.assertTrue(r.read().startswith(b'\x89PNG'))
   provider.assert_not_called()
  self.assertEqual(self.service.list_jobs(),[])
 def test_help_paths_cannot_access_arbitrary_files(self):
  for path in ['/help/../archive_console.py','/help/%2e%2e/archive_console.py','/help/step-7.png','/help/secrets.json']:
   with self.assertRaises(HTTPError) as e:self.read(path)
   self.assertEqual(e.exception.code,404)
 def test_existing_origin_gate_protects_help(self):
  with self.assertRaises(HTTPError) as e:self.read('/help/',Origin='https://outside.invalid')
  self.assertEqual(e.exception.code,403)
 def test_loading_and_review_help_preserve_offline_default(self):
  self.assertIn('href="/help/"',loading_page('http://127.0.0.1:18765'))
  self.assertIn('workflow-back-loading',loading_page('http://127.0.0.1:18765'))
  self.assertIn('href="/help/"',render_review(self.model,help_enabled=True))
  self.assertNotIn('href="/help/"',render_review(self.model))

if __name__=='__main__':unittest.main()
