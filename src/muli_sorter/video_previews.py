"""Bounded, local video stills. Source files are opened read-only, never modified."""
from contextlib import contextmanager
from datetime import datetime, timezone
import argparse
import fcntl
import json
import math
import os
from pathlib import Path
import re
import signal
import subprocess
import threading
import time

from .archive_io import directory, subdirectory, open_file, signature
from .intake import relative
from .order_feed_io import atomic_json, read_json, digest, directory as output_directory
from .queue_files import publish
from .review import validate_model

SCHEMA = 'video-previews/0.7'
RECIPE = 'three-stills-10-50-90-480x320-v1'
FORMATS = {'.mp4': 'mov', '.mov': 'mov', '.m4v': 'mov', '.lrf': 'mov',
           '.mts': 'mpegts', '.m2ts': 'mpegts', '.avi': 'avi', '.mkv': 'matroska', '.webm': 'matroska'}
IMAGE_NAME = re.compile(r'[0-9a-f]{64}\.jpg\Z')


def utc():
    return datetime.now(timezone.utc).isoformat()


def video_units(model):
    by_id = {u['unit_id']: u for u in model['units'] if u['kind'] in ('video', 'proxy_only')}
    # Start with one clip per segment so long segments cannot delay every other scene.
    first = []
    for segment in model['initial_segments']:
        for uid in segment['unit_ids']:
            if uid in by_id and uid not in first:
                first.append(uid)
                break
    return [by_id[uid] for uid in first] + [u for uid, u in by_id.items() if uid not in first]


def candidates(unit):
    files = [f for f in unit['files'] if Path(f['source_path']).suffix.lower() in FORMATS]
    # Ingest-linked proxies are smaller and sufficient for project identification.
    return sorted(files, key=lambda f: (Path(f['source_path']).suffix.lower() != '.lrf', f['size_bytes'], f['source_path']))


def identity(file):
    return {k: file[k] for k in ('source_path', 'size_bytes', 'blake3')}


@contextmanager
def source_fd(staging, file):
    parts = relative(file['source_path'])
    with directory(staging) as root:
        with subdirectory(root, '/'.join(parts[:-1])) if len(parts) > 1 else _duplicate(root) as parent:
            fd = open_file(parent, parts[-1])
            try:
                if os.fstat(fd).st_size != file['size_bytes']:
                    raise ValueError('source_size_changed')
                yield fd
            finally:
                os.close(fd)


@contextmanager
def _duplicate(fd):
    yield fd


def extract(fd, file, *, ffmpeg='ffmpeg', ffprobe='ffprobe'):
    path = ('/proc/self/fd/' if Path('/proc/self/fd').exists() else '/dev/fd/') + str(fd)
    demuxer = FORMATS[Path(file['source_path']).suffix.lower()]
    before = signature(fd)
    base = ['-v', 'error', '-protocol_whitelist', 'file,pipe', '-f', demuxer]
    os.lseek(fd, 0, os.SEEK_SET)
    probe = subprocess.run([ffprobe, *base, '-select_streams', 'v:0', '-show_entries',
                            'stream=duration,avg_frame_rate:format=duration', '-of', 'json', path],
                           pass_fds=(fd,), capture_output=True, timeout=20, check=True)
    metadata = json.loads(probe.stdout)
    streams = metadata.get('streams')
    if not isinstance(streams, list) or not streams or not isinstance(streams[0], dict):
        raise ValueError('no_video_stream')
    stream = streams[0]
    duration = float(stream.get('duration', metadata.get('format', {}).get('duration', 0)))
    if not math.isfinite(duration) or not 0 < duration <= 7 * 86400:
        raise ValueError('invalid_duration')
    rate = str(stream.get('avg_frame_rate', '25/1')).split('/')
    fps = float(rate[0]) / float(rate[1]) if len(rate) == 2 and float(rate[1]) else 25
    fps = fps if math.isfinite(fps) and 0 < fps <= 240 else 25
    # Very short clips may have no frame at 90% of the container duration.
    last_frame = max(0, duration - 1 / fps)
    positions = sorted(set(round(min(duration * part, last_frame), 3) for part in (0.1, 0.5, 0.9)))
    frames = []
    for position in positions:
        os.lseek(fd, 0, os.SEEK_SET)
        command = [ffmpeg, '-nostdin', *base, '-threads', '1', '-ss', str(position), '-i', path,
                   '-map', '0:v:0', '-an', '-sn', '-dn', '-frames:v', '1',
                   '-vf', 'scale=480:320:force_original_aspect_ratio=decrease:force_divisible_by=2:out_range=full,format=yuvj420p,setsar=1',
                   '-threads', '1', '-q:v', '4', '-f', 'image2pipe', '-vcodec', 'mjpeg', 'pipe:1']
        result = subprocess.run(command, pass_fds=(fd,), capture_output=True, timeout=30, check=True)
        data = result.stdout
        if not 4 <= len(data) <= 512 * 1024 or not data.startswith(b'\xff\xd8') or not data.endswith(b'\xff\xd9'):
            raise ValueError('invalid_image')
        frames.append((position, data))
    if signature(fd) != before:
        raise ValueError('source_changed_during_preview')
    return frames, before


def cached(output, entry, unit, staging):
    if entry.get('state') != 'ready' or entry.get('recipe') != RECIPE:
        return False
    file = next((f for f in candidates(unit) if identity(f) == entry.get('source')), None)
    if file is None or not 1 <= len(entry.get('frames', [])) <= 3:
        return False
    try:
        with source_fd(staging, file) as fd:
            if signature(fd) != entry.get('source_signature'):
                return False
        with directory(output) as out:
            for frame in entry['frames']:
                if not IMAGE_NAME.fullmatch(frame['file']):
                    return False
                fd = open_file(out, frame['file'])
                try:
                    size = os.fstat(fd).st_size
                    if not 4 <= size <= 512 * 1024 or os.read(fd, 2) != b'\xff\xd8':
                        return False
                    os.lseek(fd, -2, os.SEEK_END)
                    if os.read(fd, 2) != b'\xff\xd9':
                        return False
                finally:
                    os.close(fd)
        return True
    except (OSError, ValueError, KeyError, TypeError):
        return False


def generate_unit(staging, output, unit, extractor=extract):
    for file in candidates(unit):
        try:
            with source_fd(staging, file) as fd:
                before = signature(fd)
                images, source_signature = extractor(fd, file)
                if signature(fd) != before or source_signature != before:
                    raise ValueError('source_changed_during_preview')
            frames, files = [], {}
            for position, data in images:
                key = digest({'source': identity(file), 'signature': before, 'recipe': RECIPE, 'time': position}) + '.jpg'
                frames.append({'file': key, 'time_seconds': position})
                files[key] = data
            publish(output, None, files)
            return {'state': 'ready', 'recipe': RECIPE, 'source': identity(file), 'source_signature': before,
                    'source_label': '代理视频' if Path(file['source_path']).suffix.lower() == '.lrf' else '主视频',
                    'source_name': Path(file['source_path']).name, 'frames': frames}
        except (OSError, ValueError, KeyError, subprocess.SubprocessError):
            continue
    return {'state': 'error', 'message': '暂时无法提取截图，素材仍可确认或暂缓',
            'retry_after': time.time() + 300, 'frames': []}


def load_index(output):
    try:
        value = read_json(Path(output) / 'index.json')
        if value.get('schema_version') == SCHEMA and isinstance(value.get('entries'), dict):
            return value
    except (OSError, ValueError):
        pass
    return {'schema_version': SCHEMA, 'entries': {}}


def model_from_queue(root):
    state = read_json(Path(root) / '队列状态.json')
    if state.get('observer_stopped') is not False or state.get('source', {}).get('ok') is not True:
        raise ValueError('queue_not_ready')
    at = datetime.fromisoformat(state['source']['checked_at']).timestamp()
    if not -5 <= time.time() - at <= 120:
        raise ValueError('queue_stale')
    relative_path = state.get('base_model_path')
    if not re.fullmatch(r'combined/sha256:[0-9a-f]{64}/确认模型\.json', relative_path or ''):
        raise ValueError('model_path_invalid')
    model = read_json(Path(root) / relative_path)
    validate_model(model)
    return model


def build(model, staging, output, *, stop=None, once=True, extractor=extract):
    validate_model(model)
    index = load_index(output)
    units = video_units(model)
    index['entries'] = {u['unit_id']: index['entries'].get(u['unit_id'], {'state': 'pending', 'frames': []}) for u in units}
    index.update(base_report_id=model['report_id'], checked_at=utc())
    atomic_json(Path(output) / 'index.json', index)
    for unit in units:
        if stop is not None and stop.is_set():
            break
        entry = index['entries'][unit['unit_id']]
        if entry.get('state') == 'error' and entry.get('retry_after', 0) > time.time():
            continue
        if cached(output, entry, unit, staging):
            continue
        index['entries'][unit['unit_id']] = generate_unit(staging, output, unit, extractor)
        index['checked_at'] = utc()
        atomic_json(Path(output) / 'index.json', index)
        if not once:
            break  # Reload the current queue before processing the next clip.
    return index


def main(argv=None):
    parser = argparse.ArgumentParser(description='本地视频截图；原素材只读')
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--queue')
    source.add_argument('--model')
    source.add_argument('--pending-scope')
    parser.add_argument('--staging', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--once', action='store_true')
    args = parser.parse_args(argv)
    staging, output = Path(args.staging).absolute(), Path(args.output).absolute()
    if output == staging or output.is_relative_to(staging) or staging.is_relative_to(output):
        parser.error('截图输出必须与素材目录分离')
    if args.model and not args.once:
        parser.error('本地模型预览请使用 --once')
    with output_directory(output, create_leaf=True) as parent:
        lock = open_file(parent, '.video-previews.lock', os.O_WRONLY | os.O_CREAT)
    # The process owns this descriptor until exit; a second worker must not race it.
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    stop = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stop.set())
    if args.pending_scope:
        from .video_pending_scope import PendingVideoWorker
        return PendingVideoWorker(args.pending_scope, staging, output).run(stop, once=args.once)
    while not stop.is_set():
        try:
            model = read_json(args.model) if args.model else model_from_queue(args.queue)
            result = build(model, staging, output, stop=stop, once=args.once)
            counts = {state: sum(e['state'] == state for e in result['entries'].values()) for state in ('ready', 'error', 'pending')}
            atomic_json(output / 'worker-state.json', {'state': 'running', 'checked_at': utc(),
                        'video_units': len(result['entries']), **counts})
            if args.once:
                print(json.dumps({'video_units': len(result['entries']), **counts}))
                return 0 if counts['error'] == 0 else 1
        except (OSError, ValueError, KeyError) as exc:
            atomic_json(output / 'worker-state.json', {'state': 'unavailable', 'error_type': type(exc).__name__, 'checked_at': utc()})
            if args.once:
                return 1
        stop.wait(2)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
