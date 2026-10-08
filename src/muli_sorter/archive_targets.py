"""Plan names without overwriting, and bind numbered names before submission."""
import os
from itertools import chain
from pathlib import PurePosixPath
from .archive_io import ArchiveError, hash_fd, open_file, subdirectory
from .manual_folders import _inspect
from .manual_projects import fold
from .review import digest


def same_content(directory_fd, name, row, *, cache=None, progress=None):
    fd = open_file(directory_fd, name)
    try:
        if os.fstat(fd).st_size != row['size_bytes']:
            return False
        actual = cache.digest(fd, progress) if cache is not None else hash_fd(fd, progress=progress)
        return actual == row['blake3']
    finally:
        os.close(fd)


def plan_rows(projects_fd, selected, options, reservations, frozen=None, *, cache=None, progress=None):
    planned = {}
    by_source = {}
    listings = {}
    # Identical files from separate source paths may share one accepted target.
    # Keep every owner so replaying that task cannot mistake its own reservation
    # for a different task merely because another source was inserted last.
    reserved_targets = {}
    for path, reservation in reservations.items():
        reserved_targets.setdefault(fold(reservation['target_path']), {})[path] = reservation
    counts = {'skipped_files':0, 'renamed_files':0, 'copy_files':0, 'cleanup_files':0}
    checked = 0
    for item in selected:
        rows = []
        for original in item['rows']:
            if progress:
                progress(phase='第1/2阶段：核对所选目标文件', checked_files=checked, current_file=PurePosixPath(original['name']).name)
            row = dict(original)
            parent = str(PurePosixPath(row['target_path']).parent)
            name = row['target_name']
            if row.get('companion_of_source_path'):
                main = by_source.get(row['companion_of_source_path'])
                main_target = main['target_path'] if main else row.get('companion_parent_target')
                if not main_target or str(PurePosixPath(main_target).parent) != parent:
                    raise ArchiveError('缺少主视频已核定的目标路径，不能归档附属文件')
                row['companion_parent_target'] = main_target
                name = PurePosixPath(main_target).stem + row['companion_name_tail']
            locked = (frozen or {}).get(row['source_path']) or reservations.get(row['source_path'])
            if locked:
                if (str(PurePosixPath(locked['target_path']).parent) != parent or locked['blake3'] != row['blake3']):
                    raise ArchiveError('素材已经提交过其他归档归属，请先核对已有结果')
                candidates = [PurePosixPath(locked['target_path']).name]
            else:
                p = PurePosixPath(name)
                candidates = chain((name,), (p.stem+'_'+str(i)+p.suffix for i in range(1,10001)))
            chosen = None
            for candidate in candidates:
                target = parent+'/'+candidate
                prior = planned.get(fold(target))
                if prior:
                    identical = (prior['target_path']==target and prior['size_bytes']==row['size_bytes'] and prior['blake3']==row['blake3'])
                    if options['existing']=='skip_identical' and identical and (candidate==name or locked):
                        chosen = (candidate, True)
                        break
                    if locked or options['existing']=='error':
                        raise ArchiveError('同一项目存在同名素材，需核对后再归档：'+candidate)
                    continue
                present, identical = False, False
                if parent not in listings:
                    if _inspect(projects_fd,parent) is None:
                        listings[parent] = None
                    else:
                        with subdirectory(projects_fd,parent) as dest:
                            listing = {}
                            for entry in os.listdir(dest):
                                listing.setdefault(fold(entry),[]).append(entry)
                            listings[parent] = listing
                if listings[parent] is not None:
                    with subdirectory(projects_fd,parent) as dest:
                        aliases = listings[parent].get(fold(candidate),[])
                        present = bool(aliases)
                        if candidate in aliases:
                            identical = same_content(dest,candidate,row,cache=cache,
                                progress=(lambda count: progress(bytes_delta=count)) if progress else None)
                if not present:
                    owners = reserved_targets.get(fold(target), {})
                    owns_target = row['source_path'] in owners and all(
                        r['target_path'] == target and r['blake3'] == row['blake3']
                        for r in owners.values())
                    if owners and not owns_target:
                        if locked or options['existing']=='error':
                            raise ArchiveError('目标名称已被其他待执行任务占用：'+candidate)
                        continue
                    chosen = (candidate, False)
                    break
                if identical and (locked or (candidate==name and options['existing']=='skip_identical')):
                    chosen = (candidate, True)
                    break
                if locked or options['existing']=='error':
                    raise ArchiveError('目标文件已存在或内容不一致，禁止覆盖：'+candidate)
            if chosen is None:
                raise ArchiveError('同名文件编号已达上限，请核对目标目录')
            candidate, skipped = chosen
            row['target_name'] = candidate
            row['target_path'] = parent+'/'+candidate
            row['temp'] = '.muli-'+digest([row['target_path'],row['blake3']])[:24]+'.partial'
            planned[fold(row['target_path'])] = row
            by_source[row['source_path']] = row
            counts['skipped_files' if skipped else 'copy_files'] += 1
            counts['renamed_files'] += candidate != original['target_name']
            rows.append(row)
            checked += 1
            if progress:
                progress(checked_files=checked)
        item['rows'] = rows
    if options['mode']=='move':
        counts['cleanup_files'] = sum(len(i['rows']) for i in selected)
    return counts
