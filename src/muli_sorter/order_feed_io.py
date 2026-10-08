"""Small, standard-library-only I/O boundary for the native order reader."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import stat
from uuid import uuid4

# The queue publishes one confirmation model per combined snapshot. At 59,866
# units that file is 48.6 MiB, so the old 48 MiB cap refused the current
# snapshot and the archive workbench could not load. The console parses that
# model in about 316 MiB of RSS, so 128 MiB stays far below its 2 GiB limit
# while keeping a hard bound on any single state file.
MAX_BYTES = 128 * 1024 * 1024


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


@contextmanager
def directory(path, *, create_leaf=False):
    path = Path(path).absolute()
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for index, part in enumerate(path.parts[1:], 1):
            if part in ('.', '..'):
                raise ValueError('路径分量无效')
            if create_leaf and index == len(path.parts) - 1:
                try:
                    os.mkdir(part, 0o700, dir_fd=fd)
                    os.fsync(fd)
                except FileExistsError:
                    pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


def read_json(path):
    path = Path(path)
    with directory(path.parent) as parent:
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        try:
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_BYTES:
                raise ValueError('状态文件类型或大小不符')
            chunks, count = [], 0
            while chunk := os.read(fd, 1024 * 1024):
                count += len(chunk)
                if count > MAX_BYTES:
                    raise ValueError('状态文件超过大小限制')
                chunks.append(chunk)
            after = os.fstat(fd)
            if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise ValueError('读取期间状态文件改变')
            return json.loads(b''.join(chunks))
        finally:
            os.close(fd)


def atomic_json(path, value):
    path = Path(path)
    payload = canonical(value)
    if len(payload) > MAX_BYTES:
        raise ValueError('输出超过大小限制')
    with directory(path.parent, create_leaf=True) as parent:
        try:
            existing = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
            if not stat.S_ISREG(existing.st_mode):
                raise ValueError('拒绝覆盖非普通状态文件')
        except FileNotFoundError:
            pass
        name = '.feed-' + uuid4().hex
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
        try:
            view = memoryview(payload)
            while view:
                count = os.write(fd, view)
                if count <= 0:
                    raise OSError('short write')
                view = view[count:]
            os.fsync(fd)
        finally:
            os.close(fd)
        try:
            os.replace(name, path.name, src_dir_fd=parent, dst_dir_fd=parent)
            os.fsync(parent)
        finally:
            try:
                os.unlink(name, dir_fd=parent)
            except FileNotFoundError:
                pass
