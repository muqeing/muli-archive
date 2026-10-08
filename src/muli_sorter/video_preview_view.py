"""Local stills decorate the page without changing the confirmation model/draft ID."""
import math
import os
import zlib
from pathlib import Path

from .order_feed_io import read_json, digest
from .video_previews import SCHEMA, RECIPE, IMAGE_NAME, identity, candidates


def preview_map(output, model, prefix='../../video-previews/'):
    if prefix not in ('../../video-previews/', 'video-previews/'):
        raise ValueError('只允许已知的本地截图目录')
    try:
        data = read_json(Path(output) / 'index.json')
        if data.get('schema_version') != SCHEMA or not isinstance(data.get('entries'), dict):
            return {}
        result = {}
        for unit in model['units']:
            entry = data['entries'].get(unit['unit_id'], {})
            if unit['kind'] not in ('video', 'proxy_only'):
                continue
            if entry.get('state') == 'error':
                result[unit['unit_id']] = {'state': 'error', 'message': '暂时无法提取截图，素材仍可确认或暂缓', 'frames': []}
                continue
            if (entry.get('state') != 'ready' or entry.get('recipe') != RECIPE or
                    entry.get('source') not in [identity(f) for f in candidates(unit)]):
                continue
            frames = []
            for frame in entry.get('frames', [])[:3]:
                at = frame['time_seconds']
                if (not IMAGE_NAME.fullmatch(frame['file']) or isinstance(at, bool) or not isinstance(at, (int, float)) or
                        not math.isfinite(at) or at < 0):
                    continue
                frames.append({'src': prefix + frame['file'], 'time_seconds': at})
            if frames:
                source = entry['source']['source_path']
                result[unit['unit_id']] = {'state': 'ready', 'source_name': Path(source).name,
                    'source_label': '代理视频' if Path(source).suffix.lower() == '.lrf' else '主视频', 'frames': frames}
        return result
    except (OSError, ValueError, KeyError, TypeError):
        return {}


def preview_source_key(queue, model):
    index = queue.root / 'video-previews' / 'index.json'
    try:
        st=index.lstat();stamp=(st.st_dev,st.st_ino,st.st_size,st.st_mtime_ns,st.st_ctime_ns)
    except FileNotFoundError:stamp=None
    label = os.environ.get('MULI_SORTER_PROJECTS_LABEL', str(getattr(queue, 'projects', '拍摄项目')))
    return (model['report_id'],stamp,label)


def render_with_previews(queue, model):
    from .review_render import render_review_bytes
    source_key=preview_source_key(queue,model)
    label=source_key[2]
    previous=getattr(queue,'preview_source_cache',None)
    if previous and previous[0]==source_key:
        compressed=getattr(queue,'preview_render_cache',{}).get(previous[1])
        if compressed is not None:return zlib.decompress(compressed)
    previews = preview_map(queue.root / 'video-previews', model)
    key = (model['report_id'], digest(previews), label)
    cache = getattr(queue, 'preview_render_cache', {})
    if key not in cache:
        cache[key] = zlib.compress(render_review_bytes(model, previews, project_root_label=label), 1)
        # publish's fast cache tracks on-disk attributes, so invalidate it when
        # the expected HTML changes without changing the model/report ID.
        getattr(queue, 'bundle_cache', {}).pop('combined/' + model['report_id'], None)
        # One base and one order-enriched page; old generations remain only on disk.
        while len(cache) > 2:
            del cache[next(iter(cache))]
        queue.preview_render_cache = cache
    queue.preview_source_cache=(source_key,key)
    return zlib.decompress(cache[key])
