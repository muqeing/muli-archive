"""Memory and failure guarantees for the historical reservation snapshot writer."""
import json
from pathlib import Path
import tempfile
import tracemalloc
import unittest
from unittest.mock import patch
from muli_sorter.archive_io import atomic_json, directory


class AtomicJSONTests(unittest.TestCase):
    def test_buffered_write_keeps_exact_format_and_bounded_output_memory(self):
        value = {('合成来源/' + str(i)): {'target_path': '合成项目/' + str(i) + '/素材'*12,
                                      'blake3': 'a'*64} for i in range(18000)}
        with tempfile.TemporaryDirectory() as td, directory(Path(td).resolve()) as fd:
            tracemalloc.start()
            try:
                atomic_json(fd, 'reservations.json', value)
                _, peak = tracemalloc.get_traced_memory()
            finally:
                tracemalloc.stop()
            path = Path(td)/'reservations.json'
            self.assertGreater(path.stat().st_size, 4*1024*1024)
            self.assertLess(peak, 1024*1024, 'serialized output must stay buffered, not materialized')
            self.assertEqual(path.read_text(), json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)+'\n')

    def test_encoder_failure_keeps_previous_record_and_cleans_temporary_file(self):
        with tempfile.TemporaryDirectory() as td, directory(Path(td).resolve()) as fd:
            atomic_json(fd, 'state.json', {'original': True})
            before = (Path(td)/'state.json').read_bytes()
            for value in ({'bad': float('nan')}, {'bad': object()}):
                with self.assertRaises((ValueError, TypeError)):atomic_json(fd, 'state.json', value)
                self.assertEqual((Path(td)/'state.json').read_bytes(), before)
                self.assertEqual(sorted(p.name for p in Path(td).iterdir()), ['state.json'])

    def test_flush_failure_does_not_publish_or_leave_temporary_file(self):
        with tempfile.TemporaryDirectory() as td, directory(Path(td).resolve()) as fd:
            atomic_json(fd, 'state.json', {'original': True})
            with patch('muli_sorter.archive_io.os.fsync', side_effect=OSError('synthetic storage error')):
                with self.assertRaises(OSError):atomic_json(fd, 'state.json', {'new': True})
            self.assertEqual(json.loads((Path(td)/'state.json').read_text()), {'original': True})
            self.assertEqual(sorted(p.name for p in Path(td).iterdir()), ['state.json'])
