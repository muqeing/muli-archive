from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from muli_sorter.archive import ArchiveError, run_synthetic
from muli_sorter.archive_fixture import prepare


class ArchiveTests(unittest.TestCase):
    def test_complete_and_repeated_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve()/'fixture'
            model,decisions=prepare(root)
            before={str(p):p.read_bytes() for p in (root/'staging').rglob('*') if p.is_file()}
            first=run_synthetic(root,model,decisions)
            self.assertEqual(first['summary']['completed_files'],4,first)
            self.assertEqual(first['summary']['pending_units'],1)
            self.assertEqual(first['status'],'partial')
            second=run_synthetic(root,model,decisions)
            self.assertEqual(second['summary']['written_bytes'],0)
            self.assertEqual(second['summary']['reused_files'],4)
            self.assertEqual(before,{str(p):p.read_bytes() for p in (root/'staging').rglob('*') if p.is_file()})

    def test_production_models_never_execute(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve()/'fixture'
            model,decisions=prepare(root)
            model['example_data']=False
            with self.assertRaises(ArchiveError): run_synthetic(root,model,decisions)
            self.assertEqual(list((root/'state').iterdir()),[])
