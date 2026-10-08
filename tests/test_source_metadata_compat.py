"""An attribute-only change must not look like a content change.

The NAS rewrites inode metadata (indexing, album services, permission fixes)
without touching the bytes. The independent verification receipt names the same
inode, size and mtime, so one content read proves the file is unchanged; asking
for a new independent verification would block an already verified batch.
"""
import os
import unittest
from unittest.mock import patch

from test_postcopy_receipt import PostcopyFixture
from muli_sorter.archive_io import ArchiveError
from muli_sorter.archive_source import verify_sources
from muli_sorter.staging_coordination import staging_guard


class SourceMetadataCompatTests(PostcopyFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.env = patch.dict(os.environ, {'MULI_POSTCOPY_RECEIPTS': str(self.postcopy)})
        self.env.start()
        self.addCleanup(self.env.stop)
        with staging_guard(self.root / 'staging', create=True):
            pass

    def source(self):
        return self.root / 'staging' / self.receipt['files'][0]['resolved_path']

    def verify(self):
        return verify_sources(self.root / 'staging', self.model['units'][0], self.runtime_snapshot)

    def test_attribute_only_change_is_accepted_after_a_content_read(self):
        path = self.source()
        before = path.stat()
        os.chmod(path, 0o600)
        after = path.stat()
        self.assertNotEqual(before.st_ctime_ns, after.st_ctime_ns)
        self.assertEqual(before.st_mtime_ns, after.st_mtime_ns)
        self.assertEqual(before.st_size, after.st_size)
        self.verify()

    def test_attribute_change_with_different_content_is_rejected(self):
        path = self.source()
        stat = path.stat()
        original = path.read_bytes()
        path.write_bytes(b'x' * len(original))
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        os.chmod(path, 0o600)
        with self.assertRaises(ArchiveError) as raised:
            self.verify()
        self.assertIn('内容摘要', str(raised.exception))
        self.assertIn('签名不一致', str(raised.exception))

    def test_a_real_size_or_time_change_is_still_rejected(self):
        path = self.source()
        stat = path.stat()
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000_000))
        with self.assertRaises(ArchiveError) as raised:
            self.verify()
        self.assertNotIn('内容摘要', str(raised.exception))


if __name__ == '__main__':
    unittest.main()
