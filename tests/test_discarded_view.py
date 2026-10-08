import json
import os
from pathlib import Path
import threading
import tempfile
import unittest
from unittest.mock import patch

from blake3 import blake3

from muli_sorter.archive_io import directory, persistent_identity, signature
from muli_sorter.discarded_view import project
from muli_sorter.archive_jobs import ArchiveJobs


class DiscardedViewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.staging = self.root / 'staging'
        self.archive = self.root / 'archive-state'
        self.index = self.archive / 'discarded-index'
        self.target = self.archive / 'discarded-media' / 'drop'
        self.source_path = 'BATCH_20261002_000001/SOURCE_DATA/DCIM/A.JPG'
        self.name = 'DCIM/A.JPG'
        self.data = b'discarded projection fixture'
        self.digest = blake3(self.data).hexdigest()
        self.unit_id = 'unit-discarded'
        self.staging.mkdir()
        self.index.mkdir(parents=True)
        self.target.joinpath(self.source_path).parent.mkdir(parents=True)
        target = self.target / self.source_path
        target.write_bytes(self.data)
        self.model = {'report_id': 'report', 'units': [{
            'unit_id': self.unit_id, 'kind': 'photo', 'warnings': [],
            'provenance': [{'manifest_id': 'manifest'}],
            'files': [{'source_path': self.source_path, 'name': self.name,
                       'size_bytes': len(self.data), 'blake3': self.digest}],
        }]}
        with directory(self.staging) as fd:
            self.source_identity = persistent_identity(fd)
        with directory(self.archive) as fd:
            self.target_identity = persistent_identity(fd)
        fd = os.open(target, os.O_RDONLY)
        try:
            self.target_signature = signature(fd)
        finally:
            os.close(fd)
        self._write_records()
        self.env = patch.dict(os.environ, {'MULI_DISCARDED_INDEX': str(self.index)})
        self.env.start()
        self.addCleanup(self.env.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def _write_records(self):
        target_path = f'discarded-media/drop/{self.source_path}'
        row = {'source_path': self.source_path, 'name': self.name,
               'size_bytes': len(self.data), 'blake3': self.digest,
               'target_path': target_path, 'target_signature': self.target_signature}
        receipt = {'schema': 'recoverable-discard/1', 'status': 'completed',
                   'destination': 'discarded-media/drop', 'files': [{**row, 'unit_id': self.unit_id}]}
        receipt_dir = self.archive / 'discarded-media' / 'drop'
        receipt_dir.joinpath('discard-receipt.json').write_text(json.dumps(receipt))
        projection = {'schema': 'discarded-projection/1', 'status': 'completed',
                      'checked_at': '2026-10-02T00:00:00+00:00',
                      'source_root_identity': self.source_identity,
                      'target_root_identity': self.target_identity,
                      'original_receipt_blake3': blake3(json.dumps(receipt).encode()).hexdigest(),
                      'original_receipt_path': 'discarded-media/drop/discard-receipt.json',
                      'units': [{'unit_id': self.unit_id, 'files': [row]}],
                      'scope_files': [row]}
        self.index.joinpath('drop.json').write_text(json.dumps(projection))

    def test_verified_missing_source_projects_discarded(self):
        state = project(self.model, self.staging)
        self.assertEqual(state['units'][self.unit_id]['category'], 'discarded')
        self.assertFalse(state['warnings'])

    def test_archive_jobs_material_state_uses_discarded_projection(self):
        service = ArchiveJobs.__new__(ArchiveJobs)
        service.mutex = threading.RLock()
        service.material_reports = {}
        service.staging = self.staging
        state = service.material_state(self.model)
        self.assertEqual(state['units'][self.unit_id]['category'], 'discarded')

    def test_existing_source_is_not_called_discarded(self):
        source = self.staging / self.source_path
        source.parent.mkdir(parents=True)
        source.write_bytes(self.data)
        state = project(self.model, self.staging)
        self.assertNotEqual(state['units'][self.unit_id]['category'], 'discarded')

    def test_target_change_preserves_exception_and_reports_warning(self):
        (self.target / self.source_path).write_bytes(b'changed')
        state = project(self.model, self.staging)
        self.assertNotEqual(state['units'][self.unit_id]['category'], 'discarded')
        self.assertTrue(state['warnings'])

    def test_root_identity_change_preserves_exception_and_reports_warning(self):
        projection = json.loads(self.index.joinpath('drop.json').read_text())
        projection['target_root_identity'] = [0, 0]
        self.index.joinpath('drop.json').write_text(json.dumps(projection))
        state = project(self.model, self.staging)
        self.assertNotEqual(state['units'][self.unit_id]['category'], 'discarded')
        self.assertTrue(state['warnings'])

    def test_unknown_schema_or_projection_symlink_reports_warning(self):
        projection_path = self.index.joinpath('drop.json')
        projection = json.loads(projection_path.read_text())
        projection['schema'] = 'discarded-projection/unknown'
        projection_path.write_text(json.dumps(projection))
        state = project(self.model, self.staging)
        self.assertTrue(state['warnings'])
        projection_path.write_text(json.dumps(projection | {'schema': 'discarded-projection/1'}))
        target = self.index.joinpath('projection-target.json')
        target.write_text(projection_path.read_text())
        projection_path.unlink()
        projection_path.symlink_to(target)
        state = project(self.model, self.staging)
        self.assertTrue(state['warnings'])

    def test_unconfigured_index_keeps_existing_projection_untouched(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop('MULI_DISCARDED_INDEX', None)
            state = project(self.model, self.staging)
        self.assertNotEqual(state['units'][self.unit_id]['category'], 'discarded')
        self.assertEqual(state['warnings'], [])


if __name__ == '__main__':
    unittest.main()
