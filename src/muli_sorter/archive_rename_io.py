"""No-overwrite rename, and identity-bound access through ONE common mount."""
from contextlib import contextmanager
import ctypes
import errno
import os
from pathlib import Path, PurePosixPath
import sys

from .archive_io import ArchiveError, directory, persistent_identity, subdirectory
from .archive_targets import same_content

STRATEGY = 'same_volume_rename/v1'


def rename_noreplace(src, source_name, dst, target_name):
    # Never substitute os.rename/replace or an existence-check + overwrite.
    lib = ctypes.CDLL(None, use_errno=True)
    if sys.platform == 'linux':
        fn, flags = getattr(lib, 'renameat2', None), 1  # RENAME_NOREPLACE
    elif sys.platform == 'darwin':
        fn, flags = getattr(lib, 'renameatx_np', None), 4  # RENAME_EXCL
    else:
        fn = None
    if fn is None:
        raise OSError(errno.ENOSYS, '不支持禁止覆盖的原子移动')
    fn.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    fn.restype = ctypes.c_int
    if fn(src, os.fsencode(source_name), dst, os.fsencode(target_name), flags):
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err))


def mount_id(fd):
    if sys.platform == 'linux':
        # st_dev alone is insufficient for separate bind mounts (EXDEV).
        for line in Path(f'/proc/self/fdinfo/{fd}').read_text().splitlines():
            if line.startswith('mnt_id:'):
                return ('linux', int(line.split(':')[1]))
        raise ArchiveError('无法核对直接移动挂载身份')
    if sys.platform == 'darwin':
        return ('darwin', os.fstat(fd).st_dev)
    raise ArchiveError('当前系统不支持直接移动挂载核对')


@contextmanager
def view_roots(service):
    config = service.direct_move_view
    if not config:
        raise ArchiveError('未启用同卷直接移动入口')
    with directory(config['root']) as root:
        with subdirectory(root, config['staging']) as source:
            # '.' is the explicitly allowed projects root, not a relative path.
            with (directory(config['root']) if config['projects'] == '.' else
                  subdirectory(root, config['projects'])) as target:
                service._check_roots()
                if (persistent_identity(source) != service.identity['source_identity'] or
                        persistent_identity(target) != service.identity['target_identity']):
                    raise ArchiveError('直接移动入口与已登记中转或项目身份不一致')
                yield source, target


@contextmanager
def nearest_parent(root, path):
    current = PurePosixPath(path)
    while True:
        try:
            context = subdirectory(root, str(current)) if str(current) != '.' else None
            fd = context.__enter__() if context else os.dup(root)
            break
        except FileNotFoundError:
            if str(current) == '.':
                raise
            current = current.parent
    try:
        yield fd
    finally:
        if context:
            context.__exit__(None, None, None)
        else:
            os.close(fd)


def eligible(service, selected, options, *, cache=None):
    if options['mode'] != 'move' or not service.direct_move_view:
        return False
    rows = [r for i in selected for r in i['rows']]
    # Shared targets are direct-move eligible only when every claimant is the
    # same reviewed content.  The first claimant may create the target; later
    # claimants are handled as identical skips by the direct runner.
    planned = {}
    for row in rows:
        prior = planned.get(row['target_path'])
        if prior and (prior['size_bytes'] != row['size_bytes'] or
                      prior['blake3'] != row['blake3']):
            return False
        planned[row['target_path']] = row
    with view_roots(service) as (source, target):
        for item in selected:
            for row in item['rows']:
                with subdirectory(source, str(PurePosixPath(row['source_path']).parent)) as src:
                    with nearest_parent(target, str(PurePosixPath(row['target_path']).parent)) as dst:
                        if (mount_id(src) != mount_id(dst) or
                                persistent_identity(src)[0] != persistent_identity(dst)[0] or
                                os.fstatvfs(src).f_flag & os.ST_RDONLY or
                                os.fstatvfs(dst).f_flag & os.ST_RDONLY):
                            return False
                # Existing targets require a complete content check.  A
                # conflict is kept explicit so the caller can block a direct
                # request instead of silently choosing copy_then_cleanup.
                try:
                    with subdirectory(target, str(PurePosixPath(row['target_path']).parent)) as dst:
                        try:
                            os.stat(row['target_name'], dir_fd=dst, follow_symlinks=False)
                        except FileNotFoundError:
                            continue
                        if options['existing'] != 'skip_identical':
                            return False
                        try:
                            if not same_content(dst, row['target_name'], row,
                                                cache=cache if cache is not None else getattr(service, 'target_digests', None)):
                                return False
                        except (OSError, ArchiveError):
                            return False
                except FileNotFoundError:
                    # A not-yet-created target directory is a new target.
                    continue
    return True
