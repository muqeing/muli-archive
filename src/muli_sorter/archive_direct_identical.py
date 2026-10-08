"""Identity and cleanup helpers for direct moves with duplicate targets.

An identical target is still verified as a real destination.  This module
keeps the duplicate-source path separate from the rename path so callers
cannot accidentally treat a skipped file as copied or remove it before every
destination in the request is ready.
"""
import os
from pathlib import PurePosixPath

from .archive import exists
from .archive_io import ArchiveError, hash_fd, open_file, signature


def target_digest(fd, row, *, cache=None, progress=None):
    """Verify and return a target's content digest and durable signature."""
    handle = open_file(fd, row['target_name'])
    try:
        if os.fstat(handle).st_size != row['size_bytes']:
            raise ArchiveError('直接移动现有目标大小不符：' + row['target_name'])
        if os.fstat(handle).st_nlink != 1:
            raise ArchiveError('直接移动现有目标不是唯一链接：' + row['target_name'])
        before = signature(handle)
        value = cache.digest(handle, progress) if cache is not None else hash_fd(handle, progress=progress)
        after = signature(handle)
        # Hashing an open inode is insufficient when the pathname was swapped
        # concurrently. Reopen the no-follow name and bind it to the same
        # complete signature before accepting the content evidence.
        again = open_file(fd, row['target_name'])
        try:
            if (after != before or signature(again) != after or
                    os.fstat(again).st_nlink != 1 or value != row['blake3']):
                raise ArchiveError('直接移动现有目标内容不符：' + row['target_name'])
        finally:
            os.close(again)
        return value, after
    finally:
        os.close(handle)


def require_identical_target(fd, row, *, expected_signature=None, cache=None, progress=None,
                             metadata_compatible=False):
    """Check a named regular target against content and optional identity."""
    if not exists(fd, row['target_name']):
        raise ArchiveError('直接移动相同目标缺失：' + row['target_name'])
    value, actual = target_digest(fd, row, cache=cache, progress=progress)
    # Content and the live name were already verified above. Only the direct
    # target readback opts in; all other callers keep their exact identity rule.
    if expected_signature is not None and (actual[:4] != expected_signature[:4]
            if metadata_compatible else actual != expected_signature):
        raise ArchiveError('直接移动现有目标身份发生变化：' + row['target_name'])
    return value, actual


def remove_duplicate_source(fd, row, expected_signature, *, hash_source):
    """Unlink one duplicate source only after its identity/content is read back."""
    name = row.get('source_name') or PurePosixPath(row['source_path']).name
    handle = open_file(fd, name)
    try:
        actual = signature(handle)
        if actual != expected_signature or hash_source(handle) != row['blake3']:
            raise ArchiveError('直接移动重复来源身份或内容发生变化：' + row['source_path'])
        os.unlink(name, dir_fd=fd)
        os.fsync(fd)
    finally:
        os.close(handle)
