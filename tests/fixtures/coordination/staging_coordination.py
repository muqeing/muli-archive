"""Cross-process coordination protocol shared verbatim with the Ingest worker."""
from contextlib import contextmanager
import fcntl
import os
import stat
import time

NAME = '.muli-staging-coordination.lock'
PROTOCOL = b'muli-staging-coordination/v1\n'


@contextmanager
def staging_guard(staging, *, exclusive=False, create=False, wait=False, cancel=None):
    root = os.open(staging,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    handle = None
    try:
        if create:
            try:
                handle = os.open(NAME,os.O_RDWR|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=root)
                os.write(handle,PROTOCOL)
                os.fsync(handle)
                os.fsync(root)
            except FileExistsError:
                pass
        if handle is None:
            handle = os.open(NAME,os.O_RDWR|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=root)
        info = os.fstat(handle)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or os.pread(handle,128,0)!=PROTOCOL:
            raise ValueError('中转互斥文件无效；停止操作并核对协调配置')
        mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        while True:
            if cancel is not None and cancel.is_set():
                raise ValueError('等待中转操作结束时收到停止请求')
            try:
                fcntl.flock(handle,mode|fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if not wait:
                    raise ValueError('中转区正在执行备份或移动清理，请在当前操作结束后继续')
                if cancel is None:
                    time.sleep(0.1)
                else:
                    cancel.wait(0.1)
        current = os.stat(NAME,dir_fd=root,follow_symlinks=False)
        if (current.st_dev,current.st_ino,current.st_nlink)!=(info.st_dev,info.st_ino,1):
            raise ValueError('中转互斥文件被替换；停止操作')
        yield
    finally:
        if handle is not None:
            os.close(handle)
        os.close(root)
