"""POSIX directory-handle I/O for isolated archive experiments."""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import stat
from uuid import uuid4
from blake3 import blake3
from .intake import relative


class ArchiveError(ValueError):
    pass


DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


@contextmanager
def directory(path):
    absolute = Path(path).absolute()
    parts = relative(str(absolute)[1:])
    fd = os.open('/', DIR)
    try:
        for part in parts:
            nxt = os.open(part, DIR, dir_fd=fd)
            os.close(fd)
            fd = nxt
        yield fd
    finally:
        os.close(fd)


@contextmanager
def subdirectory(root, path, create=False):
    fd = os.dup(root)
    try:
        for part in relative(path):
            if create:
                try:
                    os.mkdir(part, 0o700, dir_fd=fd)
                    os.fsync(fd)
                except FileExistsError:
                    pass
            nxt = os.open(part, DIR, dir_fd=fd)
            os.close(fd)
            fd = nxt
        yield fd
    finally:
        os.close(fd)


def open_file(directory_fd, name, flags=os.O_RDONLY):
    if len(relative(name)) != 1:
        raise ArchiveError('文件名必须为一个路径分量')
    fd = os.open(name, flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=directory_fd)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise ArchiveError('只接受普通文件')
    return fd


def persistent_identity(fd):
    """Filesystem identity plus inode; Linux device numbers change across boots.

    ext4 statfs derives f_fsid from the on-disk UUID. Fail closed when the
    filesystem cannot provide an identity; never reduce this to inode alone.
    """
    fsid = getattr(os.fstatvfs(fd), 'f_fsid', None)
    if type(fsid) is not int or fsid == 0:
        raise ArchiveError('文件系统未提供稳定标识，无法安全绑定归档目录')
    return [fsid & ((1 << 64) - 1), os.fstat(fd).st_ino]


def signature(fd):
    s = os.fstat(fd)
    return [*persistent_identity(fd), s.st_size, s.st_mtime_ns, s.st_ctime_ns]


def hash_fd(fd, *, progress=None):
    before = signature(fd)
    os.lseek(fd, 0, os.SEEK_SET)
    h = blake3()
    while data := os.read(fd, 1024 * 1024):
        h.update(data)
        if progress is not None:
            progress(len(data))
    if signature(fd) != before:
        raise ArchiveError('计算摘要时文件发生变化')
    return h.hexdigest()


def verified_target_hash(fd, expected_digest, *, expected_signature=None, progress=None, allowed_links=(1,)):
    """Verify a destination, allowing one fresh read after ctime-only drift.

    NAS metadata writers may change ctime without changing media. Never use
    this for source evidence: identity, size and mtime must still match, the
    second full read must be stable, and its digest must equal the recorded
    source digest. A second mutation or a content mismatch fails closed.
    """
    before = signature(fd)
    links = os.fstat(fd).st_nlink
    if expected_signature is not None and before[:4] != expected_signature[:4]:
        raise ArchiveError('目标文件身份、大小或修改时间发生变化')
    if links not in allowed_links:
        raise ArchiveError('目标文件链接关系发生变化')
    try:
        result = hash_fd(fd, progress=progress)
    except ArchiveError:
        after = signature(fd)
        if after[:4] != before[:4] or after == before or os.fstat(fd).st_nlink != links:
            raise
        before = after
        result = hash_fd(fd, progress=progress)
    if result != expected_digest:
        raise ArchiveError('目标文件完整内容与来源校验值不一致')
    if signature(fd) != before or os.fstat(fd).st_nlink != links:
        raise ArchiveError('目标校验期间文件再次发生变化')
    return before


def write_all(fd, data):
    pending = memoryview(data)
    while pending:
        written = os.write(fd, pending)
        if written <= 0:
            raise OSError('short write')
        pending = pending[written:]


def atomic_json(directory_fd, name, value):
    temp = '.record-' + uuid4().hex
    fd = open_file(directory_fd, temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    try:
        write_all(fd, (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n').encode())
        os.fsync(fd)
    finally:
        os.close(fd)
    try:
        os.replace(temp, name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        os.fsync(directory_fd)
    finally:
        try:
            os.unlink(temp, dir_fd=directory_fd)
        except FileNotFoundError:
            pass


@contextmanager
def exclusive_lock(directory_fd):
    fd = open_file(directory_fd, '.archive.lock', os.O_RDWR | os.O_CREAT)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ArchiveError('已有归档任务占用这个演示目录') from exc
        yield
    finally:
        os.close(fd)
