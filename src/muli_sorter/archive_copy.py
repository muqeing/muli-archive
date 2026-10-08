"""Stage an independent file copy, resume only a verified matching prefix."""
import os
from blake3 import blake3
from .archive_io import ArchiveError, hash_fd, open_file, signature, write_all, verified_target_hash


def stage_file(source_dir, source_name, target_dir, row, persist, checkpoint, chunk_size=1024*1024, progress=None):
    source = open_file(source_dir, source_name)
    target = None
    written = 0
    progress = progress or (lambda kind, size:None)
    try:
        before = signature(source)
        if before[2] != row['size_bytes']:
            raise ArchiveError('源大小改变')
        if row.get('source_signature') and row['source_signature'] != before:
            raise ArchiveError('中断后源文件身份或属性改变')
        if row.get('temp_identity'):
            target = open_file(target_dir, row['temp'], os.O_RDWR)
            if signature(target)[:2] != row['temp_identity'] or os.fstat(target).st_nlink != 1:
                raise ArchiveError('临时文件身份或链接关系改变')
        else:
            target = open_file(target_dir, row['temp'], os.O_RDWR | os.O_CREAT | os.O_EXCL)
            row['temp_identity'] = signature(target)[:2]
            row['source_signature'] = before
            os.fsync(target_dir)
            persist()
        offset = os.fstat(target).st_size
        if offset > row['size_bytes']:
            raise ArchiveError('临时副本长度异常')
        h = blake3()
        left = offset
        while left:
            data = os.read(source, min(chunk_size, left))
            old = os.read(target, len(data))
            if not data or old != data:
                raise ArchiveError('临时副本与源前缀不一致，保留现场')
            h.update(data)
            left -= len(data)
        progress('resumed_bytes',offset)
        while data := os.read(source, chunk_size):
            write_all(target, data)
            h.update(data)
            written += len(data)
            progress('written_bytes',len(data))
            os.fsync(target)
            row['copied_bytes'] = os.fstat(target).st_size
            persist()
            checkpoint('copy_chunk', row)
        if signature(source) != before or h.hexdigest() != row['blake3']:
            raise ArchiveError('源内容与成功清单不一致')
        if os.fstat(target).st_size != row['size_bytes']:
            raise ArchiveError('临时副本完整读回校验失败')
        verified_target_hash(target, row['blake3'])
        os.utime(target, ns=(os.fstat(source).st_atime_ns, before[3]))
        os.fsync(target)
        row['staged'] = True
        persist()
        checkpoint('staged', row)
        return written, offset
    finally:
        if target is not None:
            os.close(target)
        os.close(source)


def verify_final(source_dir, source_name, target_dir, row):
    source = open_file(source_dir, source_name)
    target = None
    try:
        target = open_file(target_dir, row['target_name'])
        if signature(source) != row['source_signature']:
            raise ArchiveError('发布前后源文件状态改变')
        if signature(source)[:2] == signature(target)[:2]:
            raise ArchiveError('目标与源共享文件实体，拒绝视为独立副本')
        if os.fstat(target).st_size != row['size_bytes'] or hash_fd(source) != row['blake3']:
            raise ArchiveError('正式目标或源内容不符，禁止覆盖')
        # Publication temporarily gives our independent copy two names. The
        # caller verifies and removes exactly the recorded temporary name,
        # then requires one remaining link before accepting its receipt.
        links = (1, 2) if signature(target)[:2] == row.get('temp_identity') else (1,)
        target_signature = verified_target_hash(target, row['blake3'], allowed_links=links)
        if signature(source) != row['source_signature']:
            raise ArchiveError('发布前后源文件状态改变')
        return target_signature
    finally:
        if target is not None:
            os.close(target)
        os.close(source)
