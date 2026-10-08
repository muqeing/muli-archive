"""Target-only compatibility for NAS metadata changes, with bounded proofs.

A changed ctime is not proof of unchanged content. Re-read that target once
against its recorded digest; never relax source checks or inode/mtime binding.
"""
from collections import OrderedDict
import os
import stat
import threading

from .archive_io import ArchiveError, open_file, signature, verified_target_hash


class MetadataProofCache:
    def __init__(self, limit=8192):
        self.limit = limit
        self.values = OrderedDict()
        self.lock = threading.RLock()

    def contains(self, key):
        with self.lock:
            if key not in self.values:
                return False
            self.values.move_to_end(key)
            return True

    def remember(self, key):
        with self.lock:
            self.values[key] = True
            self.values.move_to_end(key)
            while len(self.values) > self.limit:
                self.values.popitem(last=False)


def check_target(fd, row, expected, *, cache=None, progress=None):
    """Return the stable current signature. Caller must also re-open the name."""
    if (not isinstance(expected, list) or len(expected) != 5 or
            any(type(v) is not int or v < 0 for v in expected)):
        raise ArchiveError('目标文件原始身份记录无效')
    info = os.fstat(fd)
    actual = signature(fd)
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or
            actual[2] != row['size_bytes'] or actual[:4] != expected[:4]):
        raise ArchiveError('目标文件身份、大小、修改时间或链接发生变化')
    if actual == expected:
        return actual
    key = (tuple(expected), tuple(actual), row['blake3'])
    if cache is not None and cache.contains(key):
        return actual
    actual = verified_target_hash(fd, row['blake3'], expected_signature=expected,
                                  progress=progress)
    if cache is not None:
        cache.remember((tuple(expected), tuple(actual), row['blake3']))
    return actual


def check_named_target(fd, row, expected, *, cache=None, progress=None):
    """Bind the verified inode to its no-follow directory entry as well."""
    handle = open_file(fd, row['target_name'])
    try:
        actual = check_target(handle, row, expected, cache=cache, progress=progress)
        again = open_file(fd, row['target_name'])
        try:
            if (signature(handle) != actual or signature(again) != actual or
                    os.fstat(handle).st_nlink != 1 or os.fstat(again).st_nlink != 1):
                raise ArchiveError('目标文件路径在核对时发生变化')
        finally:
            os.close(again)
        return actual
    finally:
        os.close(handle)
