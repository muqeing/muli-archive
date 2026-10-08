"""Studio folder contract and deterministic routing, without AI or media writes."""
from pathlib import PurePosixPath
import re
import unicodedata

from .archive_io import ArchiveError
from .material_triage import standard_dji_proxy_file
from .review import digest

LAYOUT_VERSION = 'studio/1'
STANDARD_FOLDERS = (
    '1相机原素材', '2视频素材/侧拍', '2视频素材/相机', '2视频素材/其他',
    '3调色调光100张', '4选片用JPG原片',
    '照片成片/客户选的原片', '照片成片/给客户的第一版', '视频成片',
)
RAW = {'.arw','.cr2','.cr3','.nef','.dng','.raf','.orf','.rw2','.pef','.srw','.raw'}
JPEG = {'.jpg','.jpeg'}
PHOTO_OTHER = {'.heic','.heif','.tif','.tiff','.png'}
VIDEO = {'.mp4','.mov','.m4v','.mts','.m2ts','.avi','.mxf'}
SIDECARS = {'.lrf','.thm','.scr','.xml','.xmp'}


def video_route(unit):
    device = str(unit.get('device') or '').strip()
    parts = [part.strip() for part in device.split(' / ')]
    routes = []
    for part in parts:
        text = part.casefold()
        if re.search(r'\bdji\b|大疆|\bosmo\b', text):
            routes.append(('侧拍', '大疆设备'))
        elif re.search(r'\biphone\b|手机|\bandroid\b|\bxperia\b|\b(?:google )?pixel\b|\b(?:huawei|honor|xiaomi|redmi|oppo|vivo|oneplus|realme|meizu)\b|华为|荣耀|小米|红米|\bgalaxy\b|\bsm-[a-z0-9]+\b|\bzenfone\b', text):
            routes.append(('侧拍', '手机设备'))
        elif re.search(r'\b(?:sony|canon|nikon|fujifilm|panasonic|olympus|pentax|leica|ricoh|hasselblad|blackmagic)\b|\bom system\b|索尼|佳能|尼康|富士|松下', text):
            routes.append(('相机', '相机设备'))
        elif re.search(r'\b(?:gopro|insta360|ipad)\b|影石', text):
            routes.append(('其他', '其他已识别设备'))
        elif text not in ('', '设备未识别', 'unknown'):
            routes.append((None, '设备类型尚未识别'))
    known = {route for route, _ in routes if route}
    if len(known) > 1 or (known and any(route is None for route, _ in routes)):
        raise ArchiveError('同一视频的设备来源存在冲突，请暂缓核对')
    # DJI metadata is often absent in verified Ingest manifests. Require both
    # a DJI card directory and the standard primary-video filename, not MP4 alone.
    names = [PurePosixPath(f['name']) for f in unit['files'] if PurePosixPath(f['name']).suffix.lower() in VIDEO]
    dji_names = bool(names) and all(
        any(re.fullmatch(r'DJI_\d{3}', part, re.I) for part in name.parts[:-1])
        and re.fullmatch(r'DJI_(?:\d{4}|\d{14}_\d{4}_[A-Z])', name.stem, re.I)
        for name in names)
    if dji_names:
        if known and known != {'侧拍'}:
            raise ArchiveError('DJI 文件来源与设备信息冲突，请暂缓核对')
        return '2视频素材/侧拍', 'DJI 标准文件名及卡内目录'
    sony_names = bool(names) and all(
        len(name.parts) >= 3 and tuple(p.upper() for p in name.parts[-3:-1]) == ('M4ROOT','CLIP')
        and re.fullmatch(r'C\d{4}',name.stem,re.I) for name in names)
    if sony_names:
        if known and known != {'相机'}:
            raise ArchiveError('相机卡内目录与设备信息冲突，请暂缓核对')
        return '2视频素材/相机', 'M4ROOT/CLIP 卡内目录及相机文件名'
    if known:
        category = next(iter(known))
        return '2视频素材/' + category, next(reason for route, reason in routes if route)
    raise ArchiveError('视频设备来源不明确，请先暂缓；不能仅凭 MP4/MOV 或 IMG 文件名判断侧拍')


def file_folder(unit, file):
    kind = unit['kind']
    extension = PurePosixPath(file['name']).suffix.lower()
    if kind == 'proxy_only':
        if (unit.get('_proxy_archive_authorized') is not True or
                standard_dji_proxy_file(unit) is None or extension != '.lrf'):
            raise ArchiveError('代理或辅助文件不能单独归档')
        return '2视频素材/侧拍', '已明确批准的 DJI LRF 代理'
    if kind == 'video':
        if extension not in VIDEO | SIDECARS:
            raise ArchiveError('视频单元混有其他主素材，请先核对配对关系')
        return video_route(unit)
    if kind == 'photo':
        if extension in JPEG:
            return '4选片用JPG原片', 'JPG/JPEG 原片'
        if extension in RAW:
            return '1相机原素材', 'RAW 原片'
        if extension in PHOTO_OTHER:
            return '1相机原素材', '其他格式照片原片'
        if extension in SIDECARS:
            has_raw = any(PurePosixPath(f['name']).suffix.lower() in RAW | PHOTO_OTHER for f in unit['files'])
            return ('1相机原素材' if has_raw else '4选片用JPG原片'), '随照片保留的伴随文件'
        raise ArchiveError('照片单元混有其他主素材，请先核对配对关系')
    if kind == 'audio':
        return '2视频素材/其他', '独立录音素材'
    raise ArchiveError('代理或辅助文件不能单独归档')


def studio_target_rows(unit, project):
    result, seen = [], set()
    for file in unit['files']:
        folder, _ = file_folder(unit, file)
        # Only copied destinations are flattened. Originals retain their paths.
        name = PurePosixPath(file['name']).name
        target = '/'.join((project['path'], folder, name))
        folded = unicodedata.normalize('NFC', target).casefold()
        if folded in seen:
            raise ArchiveError('同一项目存在同名素材，需核对后再归档：' + name)
        seen.add(folded)
        result.append({**file, 'target_path':target, 'target_name':name,
                       'temp':'.muli-'+digest([target,file['blake3']])[:24]+'.partial'})
    return result


def route_view(unit):
    if '中转文件已不在原路径，请查看归档记录或核对来源' in unit.get('warnings', []):
        return {'folders':[], 'reason':'中转文件已不在原路径，请查看归档记录或核对来源', 'blocked':True}
    try:
        rows = [file_folder(unit, file) for file in unit['files']]
        return {'folders':list(dict.fromkeys(folder for folder, _ in rows)),
                'reason':'；'.join(dict.fromkeys(reason for _, reason in rows)), 'blocked':False}
    except ArchiveError as exc:
        return {'folders':[], 'reason':str(exc), 'blocked':True}
