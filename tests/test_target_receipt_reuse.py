import unittest,json,os
from unittest.mock import Mock
from test_archive_jobs import Fixture
from muli_sorter.archive_target_evidence import SelectedTargetEvidence
from muli_sorter.archive_io import directory,subdirectory,open_file
from pathlib import PurePosixPath
class ReceiptReuse(Fixture,unittest.TestCase):
 def prepare_proof(self):
  job=self.submit();result=self.service.run_job(job['job_id']);self.assertEqual(result['status'],'completed')
  _,selected,_,_=self.service._compile(self.model,self.decisions)
  return selected,result
 def read(self,selected,cache):
  row=selected[0]['rows'][0]
  with directory(self.root/'projects') as root,subdirectory(root,str(PurePosixPath(row['target_path']).parent)) as d:
   f=open_file(d,row['target_name'])
   try:return cache.digest(f)
   finally:os.close(f)
 def test_verified_receipt_reuses_digest_without_media_read(self):
  selected,_=self.prepare_proof();fallback=Mock();cache=SelectedTargetEvidence(self.service,selected,fallback)
  self.assertEqual(self.read(selected,cache),selected[0]['rows'][0]['blake3']);fallback.digest.assert_not_called()
 def test_target_changed_falls_back(self):
  selected,_=self.prepare_proof();fallback=Mock();cache=SelectedTargetEvidence(self.service,selected,fallback)
  p=self.root/'projects'/selected[0]['rows'][0]['target_path'];p.write_bytes(b'changed')
  self.read(selected,cache);fallback.digest.assert_called_once()
 def test_missing_receipt_never_accepts_task_totals(self):
  selected,result=self.prepare_proof()
  for outcome in result['outcomes']:(self.service.state/'units'/outcome['receipt']).unlink()
  fallback=Mock();cache=SelectedTargetEvidence(self.service,selected,fallback)
  self.read(selected,cache);fallback.digest.assert_called_once()
 def test_repeat_move_rejected_before_reading_targets(self):
  self.prepare_proof();self.service.move_enabled=True
  self.decisions['archive_options']={'mode':'move','existing':'skip_identical'}
  self.service.target_digests.digest=Mock(side_effect=AssertionError('must not read target'))
  result=self.service.preflight(self.decisions)
  self.assertEqual(result['status'],'blocked')
  self.assertIn('已有归档执行记录',str(result))
  self.service.target_digests.digest.assert_not_called()
