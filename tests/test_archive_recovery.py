import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from muli_sorter.archive import run_synthetic
from muli_sorter.archive_fixture import prepare

CRASH = '''
import json,os,sys
from pathlib import Path
from muli_sorter.archive import run_synthetic
root=Path(sys.argv[1]);phase=sys.argv[2]
model=json.loads((root/'model.json').read_text())
decisions=json.loads((root/'decisions.json').read_text())
def stop(name,row):
    if name==phase: os._exit(73)
run_synthetic(root,model,decisions,checkpoint=stop,chunk_size=64)
'''


class RecoveryTests(unittest.TestCase):
    def test_process_exit_during_copy_publication_and_receipt(self):
        for phase in ('intent_recorded','copy_chunk','published','receipt'):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp).resolve()/'fixture'
                model,decisions=prepare(root)
                child=subprocess.run([sys.executable,'-c',CRASH,str(root),phase],capture_output=True,text=True)
                self.assertEqual(child.returncode,73,child.stderr)
                jobs=[json.loads(p.read_text()) for p in (root/'state').glob('job-*.json')]
                self.assertEqual(jobs[0]['state'],'in_progress')
                if phase=='copy_chunk':
                    self.assertEqual(list((root/'state').glob('receipt-*.json')),[])
                    self.assertEqual(len(list((root/'projects').rglob('*.partial'))),1)
                report=run_synthetic(root,model,decisions,chunk_size=64)
                self.assertEqual(report['summary']['completed_files'],4,report)
                self.assertEqual(report['summary']['incomplete_units'],0)
                self.assertEqual(list((root/'projects').rglob('*.partial')),[])
                if phase=='copy_chunk': self.assertEqual(report['summary']['resumed_bytes'],64)
                if phase=='published': self.assertGreaterEqual(report['summary']['reused_files'],1)
                for receipt in (root/'state').glob('receipt-*.json'):
                    data=json.loads(receipt.read_text())
                    for row in data['files']:
                        src=root/'staging'/row['source_path'];dst=root/'projects'/row['target_path']
                        self.assertEqual(src.read_bytes(),dst.read_bytes())
                        self.assertNotEqual(src.stat().st_ino,dst.stat().st_ino)
                        self.assertEqual(dst.stat().st_nlink,1)

    def test_corrupt_partial_is_not_overwritten_or_published(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve()/'fixture'
            model,decisions=prepare(root)
            child=subprocess.run([sys.executable,'-c',CRASH,str(root),'copy_chunk'],capture_output=True,text=True)
            self.assertEqual(child.returncode,73)
            part=next((root/'projects').rglob('*.partial'))
            part.write_bytes(b'x'*part.stat().st_size)
            report=run_synthetic(root,model,decisions)
            self.assertEqual(report['summary']['incomplete_units'],1)
            self.assertEqual(part.read_bytes(),b'x'*64)
            self.assertEqual(report['summary']['completed_files'],2)
