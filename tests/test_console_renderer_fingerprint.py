"""A rebuilt console must not keep serving the page an older build saved.

The pending page is persisted and reused across restarts. Its key used to cover
only the material, so shipping a UI fix left the old page in place until some
unrelated change invalidated it. The key now carries a fingerprint of the
shipped code.
"""
import tempfile
import unittest
from pathlib import Path

from muli_sorter.archive_console import renderer_fingerprint


class RendererFingerprintTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        (self.root / 'console.py').write_text('print(1)\n')
        (self.root / 'ui.js').write_text('var a = 1;\n')

    def tearDown(self):
        self.tmp.cleanup()

    def fingerprint(self):
        return renderer_fingerprint(self.root)

    def test_identical_sources_share_a_fingerprint(self):
        first = self.fingerprint()
        self.assertEqual(first, renderer_fingerprint(self.root))

    def test_changed_script_changes_the_fingerprint(self):
        before = self.fingerprint()
        (self.root / 'ui.js').write_text('var a = 2;\n')
        self.assertNotEqual(before, self.fingerprint())

    def test_changed_python_changes_the_fingerprint(self):
        before = self.fingerprint()
        (self.root / 'console.py').write_text('print(2)\n')
        self.assertNotEqual(before, self.fingerprint())

    def test_added_script_changes_the_fingerprint(self):
        before = self.fingerprint()
        (self.root / 'extra.js').write_text('var b = 1;\n')
        self.assertNotEqual(before, self.fingerprint())

    def test_unrelated_files_are_ignored(self):
        before = self.fingerprint()
        (self.root / 'notes.txt').write_text('scratch\n')
        (self.root / 'assets').mkdir()
        (self.root / 'assets' / 'x.png').write_bytes(b'png')
        self.assertEqual(before, self.fingerprint())


if __name__ == '__main__':
    unittest.main()
