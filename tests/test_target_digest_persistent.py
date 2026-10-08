"""Persistent TargetDigestCache remains identity and content bound."""
import os
from pathlib import Path
import sqlite3
import time
import tempfile
import unittest
from unittest.mock import patch

from muli_sorter.archive_checks import TargetDigestCache
from muli_sorter.archive_io import hash_fd, signature


class PersistentTargetDigestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.media = self.root / 'media.bin'
        self.media.write_bytes(b'actual media bytes' * 4096)
        self.db = self.root / 'digest-cache.sqlite3'

    def tearDown(self):
        self.tmp.cleanup()

    def _digest(self, cache):
        with self.media.open('rb') as stream:
            return cache.digest(stream.fileno())

    def test_digest_survives_new_cache_instance_without_hashing_again(self):
        first = TargetDigestCache(disk_path=self.db)
        expected = self._digest(first)
        second = TargetDigestCache(disk_path=self.db)
        with patch('muli_sorter.archive_checks.hash_fd', wraps=hash_fd) as hashed:
            self.assertEqual(self._digest(second), expected)
        self.assertEqual(hashed.call_count, 0)

    def test_exact_identity_change_invalidates_persistent_row(self):
        first = TargetDigestCache(disk_path=self.db)
        original = self.media.stat()
        old = self._digest(first)
        self.media.write_bytes(b'changed media bytes' * 4096)
        os.utime(self.media, ns=(original.st_atime_ns, original.st_mtime_ns))
        second = TargetDigestCache(disk_path=self.db)
        with patch('muli_sorter.archive_checks.hash_fd', wraps=hash_fd) as hashed:
            new = self._digest(second)
        self.assertNotEqual(new, old)
        self.assertEqual(hashed.call_count, 1)

    def test_corrupt_database_falls_back_to_real_hash(self):
        self.db.write_bytes(b'not a sqlite database')
        cache = TargetDigestCache(disk_path=self.db)
        with patch('muli_sorter.archive_checks.hash_fd', wraps=hash_fd) as hashed:
            value = self._digest(cache)
        self.assertTrue(value)
        self.assertEqual(hashed.call_count, 1)

    def test_tampered_digest_row_falls_back_to_real_hash(self):
        cache = TargetDigestCache(disk_path=self.db)
        expected = self._digest(cache)
        with sqlite3.connect(self.db) as connection:
            connection.execute('UPDATE target_digests SET digest=?', ('0' * 64,))
        reopened = TargetDigestCache(disk_path=self.db)
        with patch('muli_sorter.archive_checks.hash_fd', wraps=hash_fd) as hashed:
            self.assertEqual(self._digest(reopened), expected)
        self.assertEqual(hashed.call_count, 1)

    def test_database_uses_wal_and_normal_synchronous_mode(self):
        cache = TargetDigestCache(disk_path=self.db)
        self.assertEqual(cache._db.execute('PRAGMA synchronous').fetchone()[0], 1)
        cache.close()
        with sqlite3.connect(self.db) as connection:
            self.assertEqual(connection.execute('PRAGMA journal_mode').fetchone()[0].lower(), 'wal')

    def test_future_or_nonfinite_timestamp_is_not_reused(self):
        cache = TargetDigestCache(disk_path=self.db)
        expected = self._digest(cache)
        with self.media.open('rb') as stream:
            key = cache._key(tuple(signature(stream.fileno())))
        # Keep the proof valid while making only the timestamp untrusted.
        with sqlite3.connect(self.db) as connection:
            connection.execute('UPDATE target_digests SET stored_at=? WHERE cache_key=?',
                               (time.time() + 3600, key))
        reopened = TargetDigestCache(disk_path=self.db)
        with patch('muli_sorter.archive_checks.hash_fd', wraps=hash_fd) as hashed:
            self.assertEqual(self._digest(reopened), expected)
        self.assertEqual(hashed.call_count, 1)

    def test_persistence_failure_does_not_block_hashing(self):
        cache = TargetDigestCache(disk_path=self.root / 'missing' / 'cache.sqlite3')
        with patch('muli_sorter.archive_checks.hash_fd', wraps=hash_fd) as hashed:
            self.assertTrue(self._digest(cache))
        self.assertEqual(hashed.call_count, 1)

    def test_disk_budget_is_bounded(self):
        other = self.root / 'other.bin'
        other.write_bytes(b'other media bytes' * 4096)
        cache = TargetDigestCache(limit=1, disk_path=self.db)
        self._digest(cache)
        with other.open('rb') as stream:
            cache.digest(stream.fileno())
        reopened = TargetDigestCache(limit=1, disk_path=self.db)
        with patch('muli_sorter.archive_checks.hash_fd', wraps=hash_fd) as hashed:
            self._digest(reopened)
        self.assertEqual(hashed.call_count, 1)


if __name__ == '__main__':
    unittest.main()
