import json
from pathlib import Path
import tempfile
import unittest
from muli_sorter.archive_fixture import prepare, save_manifest
from muli_sorter.archive_source import verify_sources
from muli_sorter.archive_io import ArchiveError


class SourceTests(unittest.TestCase):
    def test_fixture_evidence_is_complete(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve()/'fixture'
            model, decisions=prepare(root)
            snapshot=lambda:json.loads((root/'runtime-state.json').read_text())
            self.assertEqual(len(model['units']),3)
            for unit in model['units']:
                self.assertEqual(list(verify_sources(root/'staging',unit,snapshot)),['BATCH_20260928_000001'])

    def test_changed_capture_metadata_invalidates_old_confirmation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve()/'fixture'
            model,_=prepare(root)
            manifest=json.loads((root/'staging/BATCH_20260928_000001/ingest_manifest.json').read_text())
            manifest['files'][0]['metadata']['capture_time']['normalized']='2026-07-08T10:00:00+08:00'
            save_manifest(root,manifest)
            with self.assertRaises(ArchiveError):
                verify_sources(root/'staging',model['units'][0],lambda:json.loads((root/'runtime-state.json').read_text()))

    def test_real_batch_cannot_enter_demo_via_example_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve()/'fixture'
            model,_=prepare(root)
            batch=root/'staging/BATCH_20260928_000001'
            manifest=json.loads((batch/'ingest_manifest.json').read_text())
            manifest['example_data']=False
            save_manifest(root,manifest)
            receipt=json.loads((batch/'ingest_complete.json').read_text())
            receipt['example_data']=False
            (batch/'ingest_complete.json').write_text(json.dumps(receipt))
            with self.assertRaises(ArchiveError):
                verify_sources(root/'staging',model['units'][0],lambda:json.loads((root/'runtime-state.json').read_text()))

    def test_cache_revalidates_changed_manifest_and_fresh_runtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve()/'fixture'
            model,_=prepare(root)
            snapshot=json.loads((root/'runtime-state.json').read_text())
            runtime=lambda:snapshot
            cache={}
            unit=model['units'][0]
            verify_sources(root/'staging',unit,runtime,cache=cache)
            snapshot['batches'][0]['state']='COPYING'
            with self.assertRaises(ValueError):
                verify_sources(root/'staging',unit,runtime,cache=cache)
            snapshot['batches'][0]['state']='COMPLETED'
            manifest=json.loads((root/'staging/BATCH_20260928_000001/ingest_manifest.json').read_text())
            manifest['files'][0]['metadata']['capture_time']['normalized']='2026-07-08T10:00:00+08:00'
            save_manifest(root,manifest)
            with self.assertRaises(ArchiveError):
                verify_sources(root/'staging',unit,runtime,cache=cache)
