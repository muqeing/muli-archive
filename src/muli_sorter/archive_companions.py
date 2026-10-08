"""Bind a separately inventoried sidecar to an authorized main-video archive."""
from pathlib import PurePosixPath

from .archive_io import ArchiveError
from .archive_layout import VIDEO, studio_target_rows
from .material_triage import _clip, standard_dji_proxy_file
from .review import digest


def primary_file(parent):
    if parent.get('_proxy_archive_authorized') is True:
        files = [standard_dji_proxy_file(parent)]
        if files == [None]:
            raise ArchiveError('已批准代理不是标准 DJI LRF 文件')
    else:
        files = [f for f in parent['files'] if PurePosixPath(f['name']).suffix.lower() in VIDEO]
    if len(files) != 1:
        raise ArchiveError('伴随文件对应的主视频不唯一，请先核对')
    return files[0]


def companion_rows(unit, parent, project, *, archived=None, label=''):
    if parent.get('_proxy_archive_authorized') is True:
        if (unit.get('kind') != 'auxiliary' or any(
                PurePosixPath(f['name']).suffix.lower() not in {'.thm', '.scr'}
                for f in unit.get('files', ()) )):
            raise ArchiveError('已批准 LRF 代理只能携带同片段 THM/SCR 附属文件')
        proxy_file = standard_dji_proxy_file(parent)
        parent_key = _clip(proxy_file, True) if proxy_file is not None else None
        if (parent_key is None or
                {_clip(f, True) for f in unit.get('files', ())} != {parent_key}):
            raise ArchiveError('已批准 LRF 代理只能携带同清单同片段 THM/SCR 附属文件')
    primary = primary_file(parent)
    baseline = next(r for r in studio_target_rows(parent, project) if r['source_path'] == primary['source_path'])
    archived_target = None
    if archived is not None:
        if archived['project']['path'] != label.rstrip('/') + '/' + project['path']:
            raise ArchiveError('附属文件只能补齐到主视频已归档的项目')
        matches = [f for f in archived['files'] if f['name'] == PurePosixPath(primary['name']).name]
        if len(matches) != 1 or not matches[0]['target_path'].startswith(label.rstrip('/') + '/'):
            raise ArchiveError('主视频归档去向不唯一，请先核对')
        archived_target = matches[0]['target_path'][len(label.rstrip('/')) + 1:]
        if PurePosixPath(archived_target).parent != PurePosixPath(baseline['target_path']).parent:
            raise ArchiveError('主视频归档目录与当前分类规则不同，请先核对')
    result = []
    for file in unit['files']:
        p, main = PurePosixPath(file['name']), PurePosixPath(primary['name'])
        tail = p.name[len(main.stem):]
        if not p.name.upper().startswith(main.stem.upper()) or not tail:
            raise ArchiveError('伴随文件名与已核对主视频不一致')
        target = str(PurePosixPath(baseline['target_path']).parent / p.name)
        result.append({**file, 'target_path': target, 'target_name': p.name,
                       'temp': '.muli-' + digest([target, file['blake3']])[:24] + '.partial',
                       'companion_of_unit_id': parent['unit_id'],
                       'companion_of_source_path': primary['source_path'],
                       'companion_name_tail': tail,
                       'companion_parent_target': archived_target})
    return result
