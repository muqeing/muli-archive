import os
from pathlib import Path
import tempfile
import unittest
from muli_sorter.archive_io import ArchiveError, atomic_json, directory, exclusive_lock, subdirectory


class ArchiveIOTests(unittest.TestCase):
    def test_safe_directory_and_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve()
            with directory(root) as fd, exclusive_lock(fd):
                with self.assertRaises(ArchiveError):
                    with exclusive_lock(fd): pass
                with subdirectory(fd,'one/two',create=True) as leaf:
                    atomic_json(leaf,'receipt.json',{'ok':True})
            self.assertIn('true',(root/'one/two/receipt.json').read_text())

    def test_symlink_directory_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve()
            (root/'outside').mkdir()
            (root/'link').symlink_to(root/'outside',target_is_directory=True)
            with directory(root) as fd, self.assertRaises(OSError):
                with subdirectory(fd,'link/child',create=True): pass
            self.assertFalse((root/'outside/child').exists())
