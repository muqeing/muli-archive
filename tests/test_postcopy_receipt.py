from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from blake3 import blake3

from muli_sorter.archive_fixture import prepare, save_manifest
from muli_sorter.archive_io import ArchiveError
from muli_sorter.archive_source import verify_sources
from muli_sorter.intake import EvidenceError, validate_record
from muli_sorter.queue_evidence import signal_parts, size_verified_candidate


class PostcopyFixture:
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve() / 'fixture'
        self.model, self.decisions = prepare(self.root)
        self.batch_id = 'BATCH_20260928_000001'
        self.batch = self.root / 'staging' / self.batch_id
        self.postcopy = self.root / 'postcopy-verifications'
        self.postcopy.mkdir()
        self._make_size_only()

    def tearDown(self):
        self.tmp.cleanup()

    def _make_size_only(self):
        manifest = json.loads((self.batch / 'ingest_manifest.json').read_text())
        manifest['batch']['result'] = 'COPY_SIZE_VERIFIED'
        files = manifest['files']
        for item in files:
            item['copy_status'] = 'size_verified'
            item['hash']['destination'] = None
            item['hash']['existing_destination'] = None
            item['hash_match'] = None
            item['existing_copy'] = None
            item['error'] = None
        summary = manifest['summary']
        summary.update(copied_file_count=len(files), verified_file_count=0,
                       size_verified_file_count=len(files), awaiting_hash_file_count=0,
                       pending_file_count=0, failed_file_count=0,
                       copied_bytes=summary['selected_bytes'], previously_ingested_count=0)
        save_manifest(self.root, manifest)
        self.manifest = manifest
        self.receipt = self._receipt(manifest)
        self._write_receipt(self.receipt)

    def _receipt(self, manifest):
        manifest_raw = (self.batch / 'ingest_manifest.json').read_bytes()
        ingest_raw = (self.batch / 'ingest_complete.json').read_bytes()
        rows = []
        for item in manifest['files']:
            source = self.batch / 'SOURCE_DATA' / item['relative_path']
            st = source.stat()
            rows.append({
                'file_id': item['file_id'], 'relative_path': item['relative_path'],
                'resolved_path': f"{self.batch_id}/SOURCE_DATA/{item['relative_path']}",
                'size_bytes': item['size_bytes'], 'blake3': item['hash']['source'],
                'source_signature': {'dev': st.st_dev, 'ino': st.st_ino, 'size': st.st_size,
                                     'mtime_ns': st.st_mtime_ns, 'ctime_ns': st.st_ctime_ns},
            })
        return {
            'schema': 'postcopy-verification/1', 'status': 'completed',
            'verified_at': '2026-10-02T00:00:00+00:00', 'batch_id': self.batch_id,
            'batch_uid': manifest['batch']['batch_uid'], 'revision': manifest['revision'],
            'manifest_id': manifest['manifest_id'], 'source_id': manifest['batch']['source_id'],
            'original_manifest_blake3': blake3(manifest_raw).hexdigest(),
            'original_ingest_complete_blake3': blake3(ingest_raw).hexdigest(),
            'files': rows,
        }

    def _write_receipt(self, receipt):
        (self.postcopy / f'{self.batch_id}.json').write_text(json.dumps(receipt))

    def snapshot(self):
        return {'batches': [{**self.manifest['batch'], 'revision': self.manifest['revision']}]}

    def runtime_snapshot(self):
        return {'generated_at': datetime.now(timezone.utc).isoformat(),
                'batches': [{**self.manifest['batch'], 'revision': self.manifest['revision']}]}


class PostcopyReceiptTests(PostcopyFixture, unittest.TestCase):
    def test_status_marker_never_grants_admission(self):
        (self.postcopy / f'{self.batch_id}.json').unlink()
        (self.postcopy / f'{self.batch_id}.status.json').write_text(
            json.dumps({'state': 'manual_archive_verified', 'verified_files': 2}))
        with patch.dict(os.environ, {'MULI_POSTCOPY_RECEIPTS': str(self.postcopy)}):
            with self.assertRaisesRegex(EvidenceError, '缺少独立校验回执'):
                validate_record(self.root / 'staging', self.batch_id, allow_examples=True)

    def test_symlinked_receipt_ancestor_is_rejected(self):
        alias = self.root / 'receipt-alias'
        alias.symlink_to(self.postcopy, target_is_directory=True)
        child = self.postcopy / 'child'
        child.mkdir()
        (child / f'{self.batch_id}.json').write_text(json.dumps(self.receipt))
        with patch.dict(os.environ, {'MULI_POSTCOPY_RECEIPTS': str(alias / 'child')}):
            with self.assertRaisesRegex(EvidenceError, '根目录不可安全读取'):
                validate_record(self.root / 'staging', self.batch_id, allow_examples=True)

    def test_old_copy_verified_does_not_require_receipt(self):
        root = self.root / 'old'
        model, _ = prepare(root)
        manifest_path = root / 'staging' / self.batch_id / 'ingest_manifest.json'
        manifest = json.loads(manifest_path.read_text())
        manifest['files'][0]['metadata']['large_note'] = 'x' * (1024 * 1024 + 1)
        save_manifest(root, manifest)
        with patch.dict(os.environ, {'MULI_POSTCOPY_RECEIPTS': str(self.postcopy)}):
            self.assertNotIn('_postcopy_evidence', validate_record(root / 'staging', self.batch_id, allow_examples=True))
            runtime = lambda: json.loads((root / 'runtime-state.json').read_text())
            verify_sources(root / 'staging', model['units'][0], runtime,
                           reviewed_metadata=True, cache={})

    def test_size_verified_receipt_admits_queue_and_source(self):
        with patch.dict(os.environ, {'MULI_POSTCOPY_RECEIPTS': str(self.postcopy)}):
            evidence = size_verified_candidate(self.root / 'staging',
                                               {**self.manifest['batch'], 'revision': self.manifest['revision']},
                                               allow_examples=True)
            self.assertIsNotNone(evidence)
            self.assertEqual(signal_parts(self.root / 'staging', [self.batch_id], self.snapshot(),
                                          allow_examples=True) != {}, True)
            unit = self.model['units'][0]
            result = verify_sources(self.root / 'staging', unit, self.runtime_snapshot,
                                    reviewed_metadata=True)
            self.assertIn('postcopy', result[self.batch_id])

    def test_missing_or_tampered_receipt_is_rejected(self):
        with patch.dict(os.environ, {'MULI_POSTCOPY_RECEIPTS': str(self.postcopy)}):
            receipt_path = self.postcopy / f'{self.batch_id}.json'
            receipt_path.unlink()
            with self.assertRaises(EvidenceError):
                validate_record(self.root / 'staging', self.batch_id, allow_examples=True)

    def test_unconfigured_receipts_reject_size_only(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop('MULI_POSTCOPY_RECEIPTS', None)
            with self.assertRaises(EvidenceError):
                validate_record(self.root / 'staging', self.batch_id, allow_examples=True)
            self._write_receipt(self.receipt)
            changed = deepcopy(self.receipt)
            changed['original_manifest_blake3'] = '0' * 64
            self._write_receipt(changed)
            with self.assertRaises(EvidenceError):
                validate_record(self.root / 'staging', self.batch_id, allow_examples=True)

    def test_cache_is_invalidated_and_source_signature_is_checked(self):
        with patch.dict(os.environ, {'MULI_POSTCOPY_RECEIPTS': str(self.postcopy)}):
            unit = self.model['units'][0]
            cache = {}
            first = verify_sources(self.root / 'staging', unit, self.runtime_snapshot,
                                   reviewed_metadata=True, cache=cache, check_media=True)
            changed = deepcopy(self.receipt)
            changed['verified_at'] = '2026-10-02T00:00:01+00:00'
            self._write_receipt(changed)
            second = verify_sources(self.root / 'staging', unit, self.runtime_snapshot,
                                    reviewed_metadata=True, cache=cache, check_media=True)
            self.assertNotEqual(first, second)
            source = self.batch / 'SOURCE_DATA' / unit['files'][0]['source_path'].split('SOURCE_DATA/', 1)[1]
            source.write_bytes(source.read_bytes())
            os.utime(source, ns=(source.stat().st_atime_ns, source.stat().st_mtime_ns + 1))
            with self.assertRaises((EvidenceError, ArchiveError, ValueError)):
                verify_sources(self.root / 'staging', unit, self.runtime_snapshot,
                               reviewed_metadata=True, cache=cache, check_media=True)

    def test_receipt_root_replacement_invalidates_cache(self):
        with patch.dict(os.environ, {'MULI_POSTCOPY_RECEIPTS': str(self.postcopy)}):
            unit = self.model['units'][0]
            cache = {}
            first = verify_sources(self.root / 'staging', unit, self.runtime_snapshot,
                                   reviewed_metadata=True, cache=cache, check_media=True)
            replacement = self.root / 'postcopy-replacement'
            replacement.mkdir()
            (replacement / f'{self.batch_id}.json').write_text(json.dumps(self.receipt))
            with patch.dict(os.environ, {'MULI_POSTCOPY_RECEIPTS': str(replacement)}):
                second = verify_sources(self.root / 'staging', unit, self.runtime_snapshot,
                                        reviewed_metadata=True, cache=cache, check_media=True)
            self.assertNotEqual(first, second)

    def test_duplicate_json_keys_and_symlink_are_rejected(self):
        with patch.dict(os.environ, {'MULI_POSTCOPY_RECEIPTS': str(self.postcopy)}):
            receipt_path = self.postcopy / f'{self.batch_id}.json'
            receipt_path.write_text('{"schema":"postcopy-verification/1","schema":"postcopy-verification/1"}')
            with self.assertRaises(EvidenceError):
                validate_record(self.root / 'staging', self.batch_id, allow_examples=True)
            receipt_path.unlink()
            target = self.root / 'receipt-target.json'
            target.write_text(json.dumps(self.receipt))
            receipt_path.symlink_to(target)
            with self.assertRaises(EvidenceError):
                validate_record(self.root / 'staging', self.batch_id, allow_examples=True)


if __name__ == '__main__':
    unittest.main()
