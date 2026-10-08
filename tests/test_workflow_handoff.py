from copy import deepcopy
import json
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from test_archive_jobs import Fixture
from muli_sorter.workflow_status import service_origin, snapshot
from muli_sorter.archive_console import make_server

BID = 'BATCH_20261002_000019'

class WorkflowProjectionTests(unittest.TestCase):
    def test_partial_archive_deduplicated_provenance_and_protected_input(self):
        model={'units':[{'unit_id':'a','file_count':2,'provenance':[{'batch_id':BID},{'batch_id':BID}]},
                        {'unit_id':'b','file_count':1,'provenance':[{'batch_id':BID}]}]}
        before=deepcopy(model)
        archive={'archived_units':[{'unit_id':'a','in_current_model':True}, {'unit_id':'b','in_current_model':False}]}
        material={'units':{'a':{'category':'shoot'},'b':{'category':'shoot'}}}
        result=snapshot(model,archive,material,None,'http://localhost:18765/')['batches'][0]
        self.assertEqual((result['units'],result['files'],result['archived'],result['pending']),(2,3,1,1))
        self.assertEqual(result['phase'],'awaiting_confirmation')
        self.assertEqual(model,before)

    def test_copy_verify_failure_manual_and_unavailable(self):
        def phase(row, ok=True):
            return snapshot({'units':[]},{},{},{'source_ok':ok,'batches':[{'batch_id':BID,**row}]},'http://localhost:18765')['batches'][0]['phase']
        self.assertEqual(phase({'state':'waiting_completion'}),'copying')
        self.assertEqual(phase({'state':'verification_required'}),'verifying')
        self.assertEqual(phase({'state':'waiting_completion','reason':'拷贝失败'}),'needs_attention')
        self.assertEqual(phase({'state':'verification_required','verification':{'state':'manual_archive_verified'}}),'manual_archived')
        self.assertEqual(phase({'state':'verification_required'},False),'unknown')

    def test_url_is_public_address_only(self):
        self.assertEqual(service_origin('http://localhost:18765/'),'http://localhost:18765')
        for url in ['javascript:alert(1)','http://user:password@localhost','http://localhost/x','http://localhost?token=x','http://localhost/#x','http://localhost:bad']:
            with self.subTest(url=url),self.assertRaises(ValueError): service_origin(url)

class WorkflowNavigationTests(Fixture,unittest.TestCase):
    def test_only_configured_document_navigation_allowed(self):
        server=make_server(self.service,self.root,ingest_url='http://127.0.0.1:18765')
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        headers={'Sec-Fetch-Site':'same-site','Sec-Fetch-Mode':'navigate','Sec-Fetch-Dest':'document','Referer':'http://127.0.0.1:18765/'}
        try:
            with urlopen(Request(server.public_origin+'/?batch_id='+BID,headers=headers)) as response:
                self.assertEqual(response.status,200)
                self.assertIn(b'workflow-back-loading',response.read())
            for route, changes in [('/api/jobs',{}),('/',{'Referer':'http://untrusted/'}),('/',{'Sec-Fetch-Mode':'cors'}),('/',{'Origin':'http://127.0.0.1:18765'})]:
                with self.subTest(route=route,changes=changes),self.assertRaises(HTTPError) as e:
                    urlopen(Request(server.public_origin+route,headers={**headers,**changes}))
                self.assertEqual(e.exception.code,403)
            with self.assertRaises(HTTPError) as e:
                urlopen(Request(server.public_origin+'/api/submit',data=b'{}',headers={**headers,'Origin':'http://127.0.0.1:18765','X-Muli-Request':'1'}))
            self.assertEqual(e.exception.code,403)
            self.assertEqual(self.service.list_jobs(),[])
        finally:
            server.shutdown();server.server_close();thread.join(2)
