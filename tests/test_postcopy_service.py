import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from test_postcopy_receipt import PostcopyFixture
from muli_sorter.intake import validate_record
from muli_sorter.postcopy_service import Verifier
from muli_sorter.staging_coordination import staging_guard
from muli_sorter.discovery_queue import DiscoveryQueue


class PostcopyServiceTests(PostcopyFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.receipt_path = self.postcopy / (self.batch_id + '.json')
        self.receipt_path.unlink()
        with staging_guard(self.root/'staging', create=True):
            pass
        self.env = patch.dict(os.environ, {'MULI_POSTCOPY_RECEIPTS': str(self.postcopy)})
        self.env.start(); self.addCleanup(self.env.stop)
        self.verifier = Verifier(self.root/'staging', self.postcopy,
                                lambda: {**self.runtime_snapshot(), 'complete_listing': True}, allow_examples=True)

    def test_full_content_read_publishes_receipt_without_media_or_ingest_changes(self):
        before = {str(p): p.read_bytes() for p in self.batch.rglob('*') if p.is_file()}
        self.verifier.verify(self.batch_id)
        self.assertEqual(before, {str(p): p.read_bytes() for p in self.batch.rglob('*') if p.is_file()})
        self.assertIn('_postcopy_evidence', validate_record(self.root/'staging', self.batch_id, allow_examples=True))
        status = json.loads((self.postcopy/(self.batch_id+'.status.json')).read_text())
        self.assertEqual(status['state'], 'completed')
        self.assertEqual(status['verified_files'], len(self.manifest['files']))

    def test_same_size_wrong_content_never_publishes_and_does_not_blindly_retry(self):
        source = self.batch/'SOURCE_DATA'/self.manifest['files'][0]['relative_path']
        original = source.read_bytes(); source.write_bytes(b'x'*len(original))
        self.verifier.cycle()
        self.assertFalse(self.receipt_path.exists())
        with patch.object(self.verifier, 'verify', side_effect=AssertionError('blind retry')):
            self.verifier.cycle()

    def test_missing_source_blocks_before_hashing(self):
        (self.batch/'SOURCE_DATA'/self.manifest['files'][0]['relative_path']).unlink()
        self.verifier.cycle()
        self.assertFalse(self.receipt_path.exists())
        status = json.loads((self.postcopy/(self.batch_id+'.status.json')).read_text())
        self.assertEqual(status['state'], 'blocked')

    def test_path_replacement_during_hashing_prevents_admission(self):
        changed = False
        def checkpoint(event, row):
            nonlocal changed
            if event == 'hash_chunk' and not changed:
                changed = True
                source = self.root/'staging'/row['resolved_path']
                old = source.with_suffix('.old'); source.rename(old)
                source.write_bytes(old.read_bytes())
        with self.assertRaisesRegex(ValueError, '路径被替换|来源内容或身份'):
            self.verifier.verify(self.batch_id, checkpoint=checkpoint)
        self.assertFalse(self.receipt_path.exists())

    def test_original_manifest_change_prevents_publish(self):
        def checkpoint(event, row):
            if event == 'before_publish':
                p = self.batch/'ingest_manifest.json'
                p.write_bytes(p.read_bytes()+b'\n')
        with self.assertRaisesRegex(ValueError, '原始备份记录改变'):
            self.verifier.verify(self.batch_id, checkpoint=checkpoint)
        self.assertFalse(self.receipt_path.exists())

    def test_existing_receipt_survives_later_move_cleanup_and_is_not_rewritten(self):
        self.verifier.verify(self.batch_id); before = self.receipt_path.read_bytes()
        (self.batch/'SOURCE_DATA'/self.manifest['files'][0]['relative_path']).unlink()
        self.verifier.cycle()
        self.assertEqual(before, self.receipt_path.read_bytes())

    def test_readonly_verifier_and_move_cleanup_share_the_existing_lock(self):
        with staging_guard(self.root/'staging', exclusive=True):
            with self.assertRaises(BlockingIOError):
                self.verifier.verify(self.batch_id)
        self.assertFalse(self.receipt_path.exists())

    def test_completed_copy_enters_queue_only_after_independent_full_verification(self):
        provider=lambda: {**self.runtime_snapshot(), 'complete_listing': True}
        with DiscoveryQueue(self.root/'staging', self.root/'projects',
                            self.root/'queue', allow_examples=True) as queue:
            queue.discover(provider)
            self.assertEqual(queue.jobs()[0]['state'], 'verification_required')
            self.verifier.verify(self.batch_id)
            queue.discover(provider)
            self.assertEqual(queue.jobs()[0]['state'], 'queued')
            self.assertEqual(queue.claim_jobs(1)[0]['batch_id'],self.batch_id)


if __name__ == '__main__':
    unittest.main()
