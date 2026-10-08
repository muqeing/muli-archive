import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from blake3 import blake3

from muli_sorter.archive_direct_recovery import check_recovery_entry
from muli_sorter.archive_io import ArchiveError, directory, hash_fd, signature


class DirectRecoveryEntryTests(unittest.TestCase):
    def setUp(self):
        # archive_io.directory walks from '/', so avoid macOS /var symlink
        # paths used by the default TemporaryDirectory location.
        self.tmp = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.root = Path(self.tmp.name)
        (self.root / 'src').mkdir()
        (self.root / 'dst').mkdir()
        self.src_context = directory(self.root / 'src')
        self.dst_context = directory(self.root / 'dst')
        self.src = self.src_context.__enter__()
        self.dst = self.dst_context.__enter__()

    def tearDown(self):
        self.dst_context.__exit__(None, None, None)
        self.src_context.__exit__(None, None, None)
        self.tmp.cleanup()

    def _row(self, source='clip.bin', target='clip.bin', content=b'original'):
        return {'source_path': source, 'target_name': target,
                'size_bytes': len(content), 'blake3': blake3(content).hexdigest()}

    def _signature(self, fd, name):
        handle = os.open(name, os.O_RDONLY, dir_fd=fd)
        try:
            return signature(handle)
        finally:
            os.close(handle)

    def test_present_source_reuses_saved_identity_without_hash(self):
        row = self._row()
        (self.root / 'src' / 'clip.bin').write_bytes(b'original')
        source_signature = self._signature(self.src, 'clip.bin')
        entry = {'state': 'pending', 'source_signature': source_signature}
        with patch('muli_sorter.archive_direct_move.hash_fd', side_effect=AssertionError('unexpected hash')):
            result = check_recovery_entry(self.src, self.dst, row, entry,
                                          recovering=True)
        self.assertEqual(result, (None, True, False))

    def test_removed_target_reuses_exact_saved_target_without_hash(self):
        row = self._row()
        (self.root / 'dst' / 'clip.bin').write_bytes(b'original')
        target_signature = self._signature(self.dst, 'clip.bin')
        entry = {'state': 'removed', 'source_signature': target_signature,
                 'target_signature': target_signature}
        with patch('muli_sorter.archive_direct_move.hash_fd', side_effect=AssertionError('unexpected hash')):
            result = check_recovery_entry(self.src, self.dst, row, entry,
                                          recovering=True)
        self.assertEqual(result, (target_signature, True, False))

    def test_source_content_change_with_restored_mtime_is_rejected(self):
        row = self._row()
        path = self.root / 'src' / 'clip.bin'
        path.write_bytes(b'original')
        source_signature = self._signature(self.src, 'clip.bin')
        before = path.stat()
        path.write_bytes(b'changed!')
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        entry = {'state': 'pending', 'source_signature': source_signature}
        with patch('muli_sorter.archive_direct_move.hash_fd', side_effect=AssertionError('unexpected hash')):
            with self.assertRaises(ArchiveError):
                check_recovery_entry(self.src, self.dst, row, entry,
                                     recovering=True)

    def test_removed_target_same_size_change_with_restored_mtime_is_rejected_without_hash(self):
        row = self._row()
        path = self.root / 'dst' / 'clip.bin'
        path.write_bytes(b'original')
        target_signature = self._signature(self.dst, 'clip.bin')
        before = path.stat()
        path.write_bytes(b'changed!')
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        entry = {'state': 'removed', 'source_signature': target_signature,
                 'target_signature': target_signature}
        with patch('muli_sorter.archive_direct_move.hash_fd', side_effect=AssertionError('unexpected hash')):
            with self.assertRaises(ArchiveError):
                check_recovery_entry(self.src, self.dst, row, entry,
                                     recovering=True)

    def test_replaced_target_inode_is_rejected_before_hash(self):
        row = self._row()
        path = self.root / 'dst' / 'clip.bin'
        path.write_bytes(b'original')
        target_signature = self._signature(self.dst, 'clip.bin')
        replacement = self.root / 'replacement'
        replacement.write_bytes(b'original')
        path.unlink()
        replacement.rename(path)
        entry = {'state': 'removed', 'source_signature': target_signature,
                 'target_signature': target_signature}
        with patch('muli_sorter.archive_direct_move.hash_fd', side_effect=AssertionError('unexpected hash')):
            with self.assertRaises(ArchiveError):
                check_recovery_entry(self.src, self.dst, row, entry,
                                     recovering=True)

    def test_uncertain_rename_requires_full_hash_and_returns_target_identity(self):
        row = self._row()
        source = self.root / 'src' / 'clip.bin'
        target = self.root / 'dst' / 'clip.bin'
        source.write_bytes(b'original')
        source_signature = self._signature(self.src, 'clip.bin')
        source.rename(target)
        target_signature = self._signature(self.dst, 'clip.bin')
        self.assertNotEqual(source_signature[4], target_signature[4])
        entry = {'state': 'renaming', 'source_signature': source_signature}
        with patch('muli_sorter.archive_direct_move.hash_fd', wraps=hash_fd) as reader:
            result = check_recovery_entry(self.src, self.dst, row, entry,
                                          recovering=True)
        self.assertEqual(result, (target_signature, False, True))
        self.assertEqual(reader.call_count, 1)

    def test_uncertain_rename_with_corrupt_target_fails_closed(self):
        row = self._row()
        source = self.root / 'src' / 'clip.bin'
        target = self.root / 'dst' / 'clip.bin'
        source.write_bytes(b'original')
        source_signature = self._signature(self.src, 'clip.bin')
        source.rename(target)
        before = target.stat()
        target.write_bytes(b'changed!')
        os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))
        entry = {'state': 'renaming', 'source_signature': source_signature}
        with patch('muli_sorter.archive_direct_move.hash_fd', wraps=hash_fd) as reader:
            with self.assertRaises(ArchiveError):
                check_recovery_entry(self.src, self.dst, row, entry,
                                     recovering=True)
        self.assertEqual(reader.call_count, 1)

    def test_source_target_conflict_and_pending_missing_fail(self):
        row = self._row()
        (self.root / 'src' / 'clip.bin').write_bytes(b'original')
        source_signature = self._signature(self.src, 'clip.bin')
        (self.root / 'dst' / 'clip.bin').write_bytes(b'original')
        with self.assertRaises(ArchiveError):
            check_recovery_entry(self.src, self.dst, row,
                                 {'state': 'pending', 'source_signature': source_signature},
                                 recovering=True)
        (self.root / 'src' / 'clip.bin').unlink()
        with self.assertRaises(ArchiveError):
            check_recovery_entry(self.src, self.dst, row,
                                 {'state': 'pending', 'source_signature': source_signature},
                                 recovering=True)


if __name__ == '__main__':
    unittest.main()
