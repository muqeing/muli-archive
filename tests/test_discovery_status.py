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

class HistoricalDisplayTests(unittest.TestCase):
    def project(self, rows, verification=None):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);bid='BATCH_20261001_000014'
            (root/'队列状态.json').write_text(json.dumps({'source':{'ok':True},'jobs':[
                {'batch_id':bid,**row} for row in rows]}))
            if verification:
                (root/(bid+'.status.json')).write_text(json.dumps({'schema':'postcopy-status/1','batch_id':bid,**verification}))
            with patch.dict(os.environ,{'MULI_POSTCOPY_RECEIPTS':str(root)}):
                return snapshot(root)['batches']

    def test_terminal_copy_is_history_without_rewriting_failure_or_completion(self):
        for reason,kind in [('拷贝失败','copy_failed'),('拷贝已中断','copy_interrupted')]:
            row=self.project([{'state':'waiting_completion','error':reason}])[0]
            self.assertEqual((row['state'],row['reason']),('waiting_completion',reason))
            self.assertTrue(row['history_only']);self.assertEqual(row['history_kind'],kind)
            self.assertNotIn('verification',row)

    def test_active_or_unknown_errors_never_silently_become_history(self):
        for state,reason in [('processing','拷贝失败'),('retry_wait','整理失败'),
                             ('waiting_completion','等待拷贝程序确认完成'),
                             ('verification_required','缺少内容证据')]:
            row=self.project([{'state':state,'error':reason}])[0]
            self.assertNotIn('history_only',row)
        row=self.project([{'state':'verification_required'}],{'state':'failed','error':'hash mismatch'})[0]
        self.assertNotIn('history_only',row)
        self.assertEqual(row['verification']['error'],'hash mismatch')

    def test_manual_archive_is_display_history_with_original_gate_preserved(self):
        row=self.project([{'state':'verification_required','error':'仍缺独立证明'}],
                         {'state':'manual_archive_verified','verified_files':7})[0]
        self.assertEqual(row['history_kind'],'manual_archived')
        self.assertEqual(row['state'],'verification_required')
        self.assertEqual(row['reason'],'仍缺独立证明')
