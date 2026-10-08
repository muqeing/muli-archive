"""Publish owned queue reports through no-follow directory handles."""
from contextlib import nullcontext
import os
from uuid import uuid4
from .archive_io import directory, subdirectory, open_file, write_all
from .intake import MAX_RECORD_BYTES


def _attrs(fd, name):
    try:
        f=open_file(fd,name)
    except FileNotFoundError:
        return None
    try:
        s=os.fstat(f)
        return [s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns,s.st_ctime_ns]
    finally:
        os.close(f)


def _matches(fd, name, expected):
    try:
        f=open_file(fd,name)
    except FileNotFoundError:
        return False
    try:
        if os.fstat(f).st_size != len(expected) or len(expected)>MAX_RECORD_BYTES:
            return False
        at=0
        while chunk:=os.read(f,1024*1024):
            if chunk != expected[at:at+len(chunk)]:
                return False
            at+=len(chunk)
        return at==len(expected)
    finally:
        os.close(f)


def publish(root, path, files, *, cache=None, checkpoint=None):
    """Backward-compatible eager publication through the same safety checks."""
    return publish_lazy(root, path, {name: (lambda value=value: value) for name, value in files.items()},
                        cache=cache, checkpoint=checkpoint)


def publish_lazy(root, path, files, *, cache=None, checkpoint=None):
    """Check both disk signatures before materializing a potentially large bundle.

    Factories are evaluated one at a time only on a missing/changed bundle.
    Existing bytes are still compared before repair; half bundles are not valid.
    """
    with directory(root) as rootfd:
        with subdirectory(rootfd,path,create=True) if path else nullcontext(rootfd) as fd:
            attrs=[_attrs(fd,name) for name in files]
            if cache is not None and cache.get(path)==attrs and all(a is not None for a in attrs):
                return
            for name,produce in files.items():
                data=produce()
                if _matches(fd,name,data):
                    del data
                    continue
                temp='.queue-'+uuid4().hex
                f=open_file(fd,temp,os.O_WRONLY|os.O_CREAT|os.O_EXCL)
                try:
                    write_all(f,data)
                    os.fsync(f)
                finally:
                    os.close(f)
                try:
                    os.replace(temp,name,src_dir_fd=fd,dst_dir_fd=fd)
                    os.fsync(fd)
                finally:
                    try:
                        os.unlink(temp,dir_fd=fd)
                    except FileNotFoundError:
                        pass
                if checkpoint:
                    checkpoint(name)
                del data
            if cache is not None:
                cache[path]=[_attrs(fd,name) for name in files]
