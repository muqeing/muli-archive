"""Shared flock protocol. v2 is activated explicitly, never on worker startup.

Every v2 operation holds the global lock SH plus sorted batch locks. Global
maintenance takes EX. Legacy workers reject the v2 marker instead of silently
operating without batch protection. Lock files are permanent coordination state.
"""
from contextlib import contextmanager, ExitStack
import fcntl
import os
import re
import stat
import time

NAME = '.muli-staging-coordination.lock'
PROTOCOL = b'muli-staging-coordination/v1\n'
BATCH_PROTOCOL = b'muli-staging-coordination/v2\n'
BATCH_LOCK_DIR = '.muli-batch-locks'
BATCH_ID = re.compile(r'BATCH_\d{8}_\d{6,}\Z')


def _ids(batch_ids):
    values = tuple(batch_ids)
    if not values or any(not isinstance(b, str) or not BATCH_ID.fullmatch(b) for b in values):
        raise ValueError('批次协调范围无效')
    return sorted(set(values))


def _same(parent, name, fd):
    before = os.fstat(fd)
    after = os.stat(name, dir_fd=parent, follow_symlinks=False)
    if (before.st_dev, before.st_ino, before.st_nlink) != (after.st_dev, after.st_ino, after.st_nlink):
        raise ValueError('中转协调文件或目录被替换；停止操作')


def _marker(fd, allowed):
    info = os.fstat(fd)
    value = os.pread(fd, 128, 0)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or value not in allowed:
        raise ValueError('中转互斥文件无效；停止操作并核对协调配置')
    return value


def _open_lock(parent, name, *, create=False, readonly=False, marker=PROTOCOL):
    if create:
        if readonly:
            raise ValueError('只读协调不能创建文件')
        try:
            fd = os.open(name, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
        except FileExistsError:
            pass
        else:
            try:
                os.write(fd, marker)
                os.fsync(fd)
                os.fsync(parent)
                return fd
            except BaseException:
                os.close(fd)
                raise
    flags = os.O_RDONLY if readonly else os.O_RDWR
    return os.open(name, flags | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)


def _take(fd, exclusive, wait, cancel, readonly):
    mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
    while True:
        if cancel is not None and cancel.is_set():
            raise ValueError('等待中转操作结束时收到停止请求')
        try:
            fcntl.flock(fd, mode | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            if not wait:
                if readonly:
                    raise
                raise ValueError('所需中转范围正在使用，请在当前操作结束后继续') from None
            if cancel is None:
                time.sleep(0.1)
            else:
                cancel.wait(0.1)


@contextmanager
def _global(staging, *, exclusive=False, create=False, wait=False, cancel=None, readonly=False, scoped=False):
    root = os.open(staging, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    fd = None
    try:
        fd = _open_lock(root, NAME, create=create, readonly=readonly)
        version = _marker(fd, (PROTOCOL, BATCH_PROTOCOL))
        # Re-read after acquisition: an explicit idle migration may have won EX.
        while True:
            _take(fd, exclusive and not (scoped and version == BATCH_PROTOCOL), wait, cancel, readonly)
            _same(root, NAME, fd)
            current = _marker(fd, (PROTOCOL, BATCH_PROTOCOL))
            if current == version:
                break
            fcntl.flock(fd, fcntl.LOCK_UN)
            version = current
        yield root, fd, version
        _same(root, NAME, fd)
        if _marker(fd, (PROTOCOL, BATCH_PROTOCOL)) != version:
            raise ValueError('操作期间协调协议改变')
    finally:
        if fd is not None:
            os.close(fd)
        os.close(root)


@contextmanager
def staging_guard(staging, *, exclusive=False, create=False, wait=False, cancel=None, readonly=False):
    with _global(staging, exclusive=exclusive, create=create, wait=wait, cancel=cancel, readonly=readonly):
        yield


@contextmanager
def batch_guard(staging, batch_ids, *, exclusive=False, create=False, wait=False, cancel=None, readonly=False):
    ids = _ids(batch_ids)
    with _global(staging, exclusive=exclusive, wait=wait, cancel=cancel, readonly=readonly, scoped=True) as (root, _, version):
        if version == PROTOCOL:
            yield False
            return
        directory = os.open(BATCH_LOCK_DIR, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root)
        try:
            with ExitStack() as stack:
                handles = []
                for bid in ids:
                    # Only existing real batch directories may acquire/create locks.
                    batch = os.open(bid, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root)
                    stack.callback(os.close, batch)
                    name = bid + '.lock'
                    fd = _open_lock(directory, name, create=create, readonly=readonly, marker=BATCH_PROTOCOL)
                    stack.callback(os.close, fd)
                    _marker(fd, (BATCH_PROTOCOL,))
                    _take(fd, exclusive, wait, cancel, readonly)
                    _same(directory, name, fd)
                    _marker(fd, (BATCH_PROTOCOL,))
                    _same(root, bid, batch)
                    handles.append((bid, batch, name, fd))
                _same(root, BATCH_LOCK_DIR, directory)
                yield True
                for bid, batch, name, fd in handles:
                    _same(directory, name, fd)
                    _same(root, bid, batch)
                _same(root, BATCH_LOCK_DIR, directory)
        finally:
            os.close(directory)


def activate_batch_protocol(staging, batch_ids):
    """Explicit maintenance step. Caller must stop all three compatible services.

    Takes global EX without waiting; initializes all supplied batch locks before
    changing the marker in-place (never replace a live flock inode). An interrupted
    marker write fails closed. Never call this from a worker startup/request.
    """
    ids = _ids(batch_ids)
    # _global's exit assertion intentionally is not used around marker mutation.
    root = os.open(staging, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    fd = directory = None
    try:
        fd = _open_lock(root, NAME)
        _take(fd, True, False, None, False)
        _same(root, NAME, fd)
        _marker(fd, (PROTOCOL, BATCH_PROTOCOL))
        actual = {name for name in os.listdir(root) if BATCH_ID.fullmatch(name)}
        if actual != set(ids):
            raise ValueError('迁移批次清单与中转目录不一致')
        try:
            os.mkdir(BATCH_LOCK_DIR, 0o700, dir_fd=root)
            os.fsync(root)
        except FileExistsError:
            pass
        directory = os.open(BATCH_LOCK_DIR, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root)
        for bid in ids:
            bfd = os.open(bid, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root)
            os.close(bfd)
            lock = _open_lock(directory, bid + '.lock', create=True, marker=BATCH_PROTOCOL)
            try:
                _marker(lock, (BATCH_PROTOCOL,))
                _same(directory, bid + '.lock', lock)
            finally:
                os.close(lock)
        _same(root, BATCH_LOCK_DIR, directory)
        os.pwrite(fd, BATCH_PROTOCOL, 0)
        os.ftruncate(fd, len(BATCH_PROTOCOL))
        os.fsync(fd)
        _same(root, NAME, fd)
        if _marker(fd, (BATCH_PROTOCOL,)) != BATCH_PROTOCOL:
            raise ValueError('批次协调启用回读失败')
    finally:
        if directory is not None:
            os.close(directory)
        if fd is not None:
            os.close(fd)
        os.close(root)
