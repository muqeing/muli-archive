"""Mixed same-volume MOVE receipts keep independent backup feedback valid."""
import json
import unittest
from pathlib import Path
from test_archive_jobs import Fixture
from muli_sorter.archive_layout import studio_target_rows
from muli_sorter.archive_handoff import ArchiveHandoff
from muli_sorter.archive_rename_io import STRATEGY

class MixedFeedbackTests(Fixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.service.move_enabled=True
        self.service.direct_move_view={'root':str(self.root),'staging':'staging','projects':'projects'}
        self.decisions['archive_options']={'mode':'move','existing':'skip_identical'}
        seg=next(x for x in self.decisions['segments'] if x['decision']=='confirmed')
        unit=next(x for x in self.model['units'] if x['unit_id']==seg['unit_ids'][0])
        project=next(x for x in self.model['projects'] if x['project_id']==seg['project_id'])
        row=studio_target_rows(unit,project)[0]
        src=self.root/'staging'/row['source_path'];dst=self.root/'projects'/row['target_path']
        dst.parent.mkdir(parents=True,exist_ok=True);dst.write_bytes(src.read_bytes())
        self.outbox=self.root/'feedback-out';self.acks=self.root/'feedback-ack'
        self.outbox.mkdir();self.acks.mkdir()
        self.producer=ArchiveHandoff(self.service,self.outbox,self.acks,'2020-01-01T00:00:00+00:00')

    def tearDown(self):
        self.producer.close();super().tearDown()

    def complete(self):
        job=self.submit();self.assertEqual(job['execution_strategy'],STRATEGY)
        result=self.service.run_job(job['job_id']);self.assertEqual(result['status'],'completed',result)
        self.assertEqual(result['summary']['copy_files'],0)
        return result

    def test_mixed_receipts_can_build_feedback_without_inode_equating_duplicates(self):
        job=self.complete()
        event=self.producer.build(job['job_id'])
        self.assertIsInstance(event,dict)
        self.assertEqual(job['summary']['skipped_files'],1)
        self.assertEqual(job['summary']['direct_moved_files'],3)

    def test_duplicate_receipt_requires_explicit_identical_hash_evidence(self):
        job=self.complete()
        for outcome in job['outcomes']:
            p=self.service.state/'units'/outcome['receipt'];receipt=json.loads(p.read_text())
            found=False
            for row in receipt['files']:
                if row.get('transfer_method')=='skip_identical':
                    row.pop('identical_target_blake3',None);found=True
            if found:
                p.write_text(json.dumps(receipt));break
        else:self.fail('No identical outcome')
        with self.assertRaisesRegex(ValueError,'identical_move_evidence_missing'):
            self.producer.build(job['job_id'])
