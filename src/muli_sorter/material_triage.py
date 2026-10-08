"""Deterministic view over immutable review identities; never moves media."""
from collections import defaultdict
from datetime import datetime
import os
from pathlib import PurePosixPath
import re
import xml.etree.ElementTree as ET

from .intake import _open, media_stat

VERSION = 'material-triage/1'
MISSING = '中转文件已不在原路径，请查看归档记录或核对来源'


def _clip(file, companion=False):
    p = PurePosixPath(file['name'])
    parts = tuple(s.upper() for s in p.parts)
    stem, ext = p.stem.upper(), p.suffix.upper()
    if re.fullmatch(r'DJI_(?:\d{4}|\d{14}_\d{4}_[A-Z])', stem):
        if len(parts) == 3 and parts[0] == 'DCIM' and re.fullmatch(r'DJI_\d{3}', parts[1]):
            if ext in ({'.LRF'} if companion else {'.MP4', '.MOV'}):
                return ('dji', parts[1], stem)
        if companion and len(parts) == 4 and parts[:2] == ('MISC', 'THM') and re.fullmatch(r'DJI_\d{3}', parts[2]) and ext in {'.THM', '.SCR'}:
            return ('dji', parts[2], stem)
    if len(parts) in (3, 4) and parts[-3:-1] == ('M4ROOT', 'CLIP') and (len(parts) == 3 or parts[0] == 'PRIVATE'):
        if companion and ext == '.XML' and re.fullmatch(r'C\d{4}M\d{2}', stem):
            return ('sony', '/'.join(parts[:-1]), stem[:-3])
        if not companion and ext in {'.MP4', '.MOV'} and re.fullmatch(r'C\d{4}', stem):
            return ('sony', '/'.join(parts[:-1]), stem)
    return None


def standard_dji_proxy_file(unit):
    """Return the sole standard DJI LRF file for a proxy-only unit."""
    if unit.get('kind') != 'proxy_only' or len(unit.get('files', ())) != 1:
        return None
    file = unit['files'][0]
    p = PurePosixPath(file.get('name', ''))
    if (len(p.parts) != 3 or p.parts[0].upper() != 'DCIM' or
            not re.fullmatch(r'DJI_\d{3}', p.parts[1], re.I) or
            not re.fullmatch(r'DJI_(?:\d{4}|\d{14}_\d{4}_[A-Z])', p.stem, re.I) or
            p.suffix.upper() != '.LRF'):
        return None
    return file


def companion_links(model, approved_proxy_ids=()):
    """Unique clip within an evidenced manifest, including deduplicated imports.

    Physical stored-batch names alone cannot prove two imports used one card.
    Multiple candidates across any referenced manifest are deliberately ambiguous.
    """
    approved_proxy_ids = set(approved_proxy_ids)
    primaries = defaultdict(set)
    units = {u['unit_id']: u for u in model['units']}
    for unit in model['units']:
        if unit['kind'] != 'video':
            continue
        keys = {_clip(f) for f in unit['files']} - {None}
        if len(keys) != 1:
            continue
        for ref in unit.get('provenance', []):
            primaries[(ref['manifest_id'], next(iter(keys)))].add(unit['unit_id'])
    # An explicitly approved, standard LRF may stand in for a missing MP4.
    # This branch is deliberately opt-in so the historical default mapping is
    # byte-for-byte equivalent for old callers.
    for uid in approved_proxy_ids:
        unit = units.get(uid)
        if standard_dji_proxy_file(unit or {}) is None:
            continue
        key = _clip(unit['files'][0], True)
        if key is None:
            continue
        for ref in unit.get('provenance', []):
            primaries[(ref['manifest_id'], key)].add(uid)
    links, ambiguous = {}, set()
    for unit in model['units']:
        if unit['kind'] not in ('auxiliary', 'proxy_only'):
            continue
        keys = {_clip(f, True) for f in unit['files']}
        if len(keys) != 1 or None in keys:
            continue
        candidates = set()
        for ref in unit.get('provenance', []):
            candidates.update(primaries[(ref['manifest_id'], next(iter(keys)))])
        if len(candidates) == 1:
            parent = next(iter(candidates))
            if (parent in approved_proxy_ids and
                    (unit['kind'] != 'auxiliary' or any(
                        PurePosixPath(f['name']).suffix.upper() not in {'.THM', '.SCR'}
                        for f in unit['files']))):
                # A proxy parent can carry only the DJI THM/SCR children.
                continue
            links[unit['unit_id']] = parent
        elif candidates:
            ambiguous.add(unit['unit_id'])
    return links, ambiguous


def support_reason(file):
    name = file['name'].upper()
    if re.fullmatch(r'(?:PRIVATE/)?AVF_INFO/AVIN\d{4}\.(?:INP|BNP|INT)', name):
        return '相机管理文件'
    if re.fullmatch(r'(?:PRIVATE/)?M4ROOT/MEDIAPRO\.XML|PRIVATE/DATABASE/DATABASE\.BIN|(?:PRIVATE/)?SONY/SONYCARD\.IND', name):
        return '相机目录与索引文件'
    if re.fullmatch(r'SONY/SETTING/[^/]+/CAMSET/[^/]+\.DAT', name):
        return '相机设置文件，保留供恢复设置'
    if re.fullmatch(r'MISC/AC\d{3}\.DB', name):
        return '设备管理数据库'
    if name in {'SYSTEM VOLUME INFORMATION/INDEXERVOLUMEGUID', 'SYSTEM VOLUME INFORMATION/WPSETTINGS.DAT'} or PurePosixPath(name).name == '.DS_STORE':
        return '系统目录信息'
    return None


def check_companion_metadata(staging, unit, parent):
    """Check bounded Sony clip metadata where available; no timezone guessing."""
    for f in unit['files']:
        key = _clip(f, True)
        if not key or key[0] != 'sony':
            continue
        if not 0 < f['size_bytes'] <= 256 * 1024:
            raise ValueError('Sony 伴随文件格式或长度需要核对')
        fd = _open(staging, f['source_path'])
        try:
            raw = os.read(fd, 256 * 1024 + 1)
        finally:
            os.close(fd)
        if len(raw) != f['size_bytes'] or b'<!DOCTYPE' in raw.upper() or b'<!ENTITY' in raw.upper():
            raise ValueError('Sony 伴随文件格式或长度需要核对')
        try:
            root = ET.fromstring(raw)
        except ET.ParseError as exc:
            raise ValueError('Sony 伴随文件无法解析') from exc
        if root.tag.split('}')[-1] != 'NonRealTimeMeta':
            raise ValueError('Sony 伴随文件的内容与文件名不符')
        for node in root.iter():
            if node.tag.split('}')[-1] == 'CreationDate' and parent.get('timezone_trusted'):
                try:
                    created = datetime.fromisoformat(node.attrib['value'])
                    captured = datetime.fromisoformat(parent['capture_time'])
                except (KeyError, TypeError, ValueError) as exc:
                    raise ValueError('Sony 伴随文件的拍摄时间需要核对') from exc
                if created.tzinfo and captured.tzinfo and abs((created - captured).total_seconds()) > 2:
                    raise ValueError('Sony 伴随文件与主视频拍摄时间冲突')


def build_material_state(model, staging=None, *, skip_source_ids=()):
    links, ambiguous = companion_links(model)
    units = {u['unit_id']: u for u in model['units']}
    result = {}
    skip_source_ids = set(skip_source_ids)
    for unit in model['units']:
        uid, files = unit['unit_id'], unit['files']
        row = {'category': 'shoot', 'reason_code': 'primary', 'reason': ''}
        if uid in skip_source_ids:
            result[uid] = ({'category':'companion','parent_unit_id':links[uid]} if uid in links else row)
            continue
        try:
            if staging is not None:
                for f in files:
                    media_stat(staging, f['source_path'], f['size_bytes'])
            elif MISSING in unit.get('warnings', []):
                raise FileNotFoundError()
        except FileNotFoundError:
            row = {'category': 'exception', 'reason_code': 'source_missing', 'reason': '来源文件不在记录的位置', 'action': '核对归档记录或恢复来源后，点击重新检查；路径不存在不代表已经归档。'}
        except (OSError, ValueError):
            row = {'category': 'exception', 'reason_code': 'source_changed', 'reason': '来源文件大小、类型或访问状态与记录不符', 'action': '核对来源后重新检查，不能直接归档。'}
        else:
            apple = [f for f in files if PurePosixPath(f['name']).name.startswith('._')]
            reasons = [support_reason(f) for f in files]
            if apple:
                verified = staging is not None and len(apple) == len(files)
                for f in apple if verified else []:
                    try:
                        fd = _open(staging, f['source_path'])
                        try:
                            verified = verified and os.read(fd, 8) == bytes.fromhex('0005160700020000')
                        finally:
                            os.close(fd)
                    except (OSError, ValueError):
                        verified = False
                row = {'category': 'support' if verified else 'exception', 'reason_code': 'appledouble' if verified else 'metadata_unverified', 'reason': 'Mac 文件附属信息' if verified else '疑似 Mac 附属文件，尚未验证文件头', 'action': '保留原文件，无需选择项目。' if verified else '重新检查文件类型后再处理。'}
            elif all(reasons):
                row = {'category': 'support', 'reason_code': 'device_support', 'reason': '；'.join(dict.fromkeys(reasons)), 'action': '无需选择项目；文件继续保留在原批次。'}
            elif uid in links:
                row = {'category': 'companion', 'reason_code': 'linked_companion', 'reason': '已关联同来源的主视频', 'parent_unit_id': links[uid], 'action': '跟随主视频的项目归属。'}
                if staging is not None:
                    try:
                        check_companion_metadata(staging, unit, units[links[uid]])
                    except (OSError, ValueError) as exc:
                        row = {'category': 'exception', 'reason_code': 'companion_metadata_conflict', 'reason': str(exc), 'action': '保留待核对，不能自动跟随主视频。'}
            elif unit['kind'] in ('auxiliary', 'proxy_only'):
                code = 'ambiguous_companion' if uid in ambiguous else 'main_missing' if unit['kind'] == 'proxy_only' or any(_clip(f, True) for f in files) else 'unrecognized'
                reason = {'ambiguous_companion': '有多个可能对应的主视频', 'main_missing': '当前记录未找到对应主视频', 'unrecognized': '文件用途或配对关系尚未识别'}[code]
                row = {'category': 'exception', 'reason_code': code, 'reason': reason, 'action': '保留待处理；核对原卡或补充导入主素材后重新检查。'}
        if row['category'] == 'shoot':
            from .archive_layout import route_view
            routing = route_view({**unit, 'warnings': [w for w in unit.get('warnings', []) if w != MISSING]})
            if routing['blocked']:
                row = {'category': 'exception', 'reason_code': 'routing_unknown', 'reason': routing['reason'], 'action': '保留待处理；核对设备来源和素材类型后重新检查。'}
        result[uid] = row
    return {'version': VERSION, 'report_id': model['report_id'], 'units': result}
