"""Bounded checks for already-started direct-move entries.

This module deliberately does not change a journal entry.  It only decides
which already-recorded identity check is sufficient for the current paths.
The caller remains responsible for parent-directory and mount guards and for
persisting a returned target signature/state transition.
"""
from pathlib import PurePosixPath

from .archive_io import ArchiveError


def _saved_signature(value, label):
    """Return a validated five-field archive signature."""
    if (not isinstance(value, list) or len(value) != 5 or
            any(type(item) is not int or item < 0 for item in value)):
        raise ArchiveError('直接移动日志缺少有效的' + label)
    return value


def check_recovery_entry(src, dst, row, entry, *, recovering, progress=None):
    """Validate one direct-move journal entry without unnecessary hashing.

    The returned tuple is ``(target_signature_or_none, reused, hashed)``.
    ``reused`` means that a saved complete signature was sufficient; ``hashed``
    means the uncertain rename window required a full content read.

    A source that is still present must retain its exact saved identity and a
    target must not exist.  A removed source may be adopted only when its
    saved target identity is present and exact.  A ``renaming`` entry with no
    target identity is the only case that permits the conservative fallback:
    the target must match the saved source identity except for ctime and its
    content must match the recorded digest.
    """
    if not isinstance(row, dict) or not isinstance(entry, dict):
        raise ArchiveError('直接移动日志记录无效')
    state = entry.get('state')
    if state not in ('pending', 'renaming', 'removed'):
        raise ArchiveError('直接移动日志状态无效')

    # Lazy imports avoid a module cycle: archive_direct_move imports this
    # helper, while _checked itself lives in archive_direct_move.
    from .archive import exists
    from .archive_direct_move import _checked

    source_name = PurePosixPath(row['source_path']).name
    target_name = row['target_name']
    source_exists = exists(src, source_name)
    target_exists = exists(dst, target_name)

    if source_exists:
        if state == 'removed':
            raise ArchiveError('移动路径重新出现或目标已占用，保留现场')
        if target_exists:
            raise ArchiveError('移动路径重新出现或目标已占用，保留现场')
        source_signature = _saved_signature(entry.get('source_signature'), '来源身份')
        _checked(src, source_name, row, source_signature)
        return None, True, False

    if state == 'pending':
        raise ArchiveError('尚未开始直接移动的来源缺失')
    if not recovering:
        raise ArchiveError('直接移动来源在恢复前缺失')
    if not target_exists:
        raise ArchiveError('直接移动目标缺失，不能采用已移出来源')

    target_signature = entry.get('target_signature')
    if target_signature is not None:
        target_signature = _saved_signature(target_signature, '目标身份')
        actual = _checked(dst, target_name, row, target_signature)
        return actual, True, False

    # A crash can leave a renaming entry after rename but before the target
    # signature was durably recorded.  Only this uncertain window may hash.
    if state != 'renaming':
        raise ArchiveError('直接移动日志缺少目标身份')
    source_signature = _saved_signature(entry.get('source_signature'), '来源身份')
    actual = _checked(dst, target_name, row, source_signature, moved=True,
                      content=True, progress=progress)
    return actual, False, True
