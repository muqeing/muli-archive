from copy import deepcopy
import json
import os
import unittest
from unittest.mock import patch

from blake3 import blake3

from muli_sorter import postcopy_receipt
from test_postcopy_receipt import PostcopyFixture


class ReceiptDecodeReuseTests(PostcopyFixture, unittest.TestCase):
    def _candidate(self, cache):
        return postcopy_receipt._candidate_signature(
            self.root / 'staging', self.batch_id, manifest=self.manifest, cache=cache)

    def test_candidate_reuses_decoded_receipt_within_callers_cache(self):
        cache = {}
        with patch.object(postcopy_receipt, '_decode', wraps=postcopy_receipt._decode) as decode:
            with patch.dict(os.environ, {'MULI_POSTCOPY_RECEIPTS': str(self.postcopy)}):
                results = [self._candidate(cache) for _ in range(6)]
        self.assertEqual(results[0], results[-1])
        self.assertEqual(decode.call_count, 1)

    def test_original_change_with_restored_mtime_does_not_reuse_decode(self):
        cache = {}
        receipt_path = self.postcopy / f'{self.batch_id}.json'
        with patch.dict(os.environ, {'MULI_POSTCOPY_RECEIPTS': str(self.postcopy)}):
            with patch.object(postcopy_receipt, '_decode', wraps=postcopy_receipt._decode) as decode:
                first = self._candidate(cache)
                before = receipt_path.stat()
                changed = deepcopy(self.receipt)
                changed['verified_at'] = '2026-10-02T00:00:01+00:00'
                receipt_path.write_text(json.dumps(changed))
                os.utime(receipt_path, ns=(before.st_atime_ns, before.st_mtime_ns))
                second = self._candidate(cache)
        self.assertNotEqual(first, second)
        self.assertEqual(decode.call_count, 2)

    def test_changed_renewal_chain_is_rejected_after_cache_hit(self):
        receipt_path = self.postcopy / f'{self.batch_id}.json'
        renewal_path = self.postcopy / f'{self.batch_id}.v2.json'
        renewal_path.write_text(json.dumps({
            'schema': 'postcopy-verification/2',
            'previous_receipt_blake3': blake3(receipt_path.read_bytes()).hexdigest(),
        }))
        cache = {}
        with patch.dict(os.environ, {'MULI_POSTCOPY_RECEIPTS': str(self.postcopy)}):
            self._candidate(cache)
            renewal = json.loads(renewal_path.read_text())
            renewal['previous_receipt_blake3'] = '0' * 64
            renewal_path.write_text(json.dumps(renewal))
            with self.assertRaises(postcopy_receipt.PostcopyError):
                self._candidate(cache)


if __name__ == '__main__':
    unittest.main()
