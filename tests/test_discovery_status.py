import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from muli_sorter.discovery_status import snapshot


class DiscoveryStatusTests(unittest.TestCase):
    def test_progress_projection_excludes_archived_jobs_and_private_queue_fields(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); receipts=root/'postcopy'; receipts.mkdir()
            bid='BATCH_20261001_000014'
            queue={'base_model_path':'combined/sha256:'+'a'*64+'/确认模型.json','source':{'ok':True},'jobs':[
                {'batch_id':bid,'state':'verification_required','token':'private-token'},
                {'batch_id':'BATCH_20261001_000013','state':'awaiting_confirmation'}]}
            (root/'队列状态.json').write_text(json.dumps(queue))
            (receipts/(bid+'.status.json')).write_text(json.dumps({'schema':'postcopy-status/1','batch_id':bid,'state':'verifying','verified_files':7,'total_files':41,'read_bytes':123}))
            with patch.dict(os.environ,{'MULI_POSTCOPY_RECEIPTS':str(receipts)}):
                status=snapshot(root)
            self.assertEqual(len(status['batches']),1)
            self.assertEqual(status['batches'][0]['verification']['verified_files'],7)
            self.assertNotIn('private-token',json.dumps(status))

    def test_foreign_progress_does_not_fabricate_completion(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);bid='BATCH_20261001_000014'
            (root/'队列状态.json').write_text(json.dumps({'jobs':[{'batch_id':bid,'state':'verification_required'}]}))
            (root/(bid+'.status.json')).write_text(json.dumps({'schema':'postcopy-status/1','batch_id':'other','state':'completed'}))
            with patch.dict(os.environ,{'MULI_POSTCOPY_RECEIPTS':str(root)}):
                status=snapshot(root)
            self.assertEqual(status['batches'][0]['verification']['state'],'unavailable')


if __name__=='__main__':unittest.main()
