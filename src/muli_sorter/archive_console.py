"""Operator console; write requests require same-origin explicit submission.

LAN access must be explicitly enabled and pass through the separately restricted
LAN gateway. The worker stays on its isolated Docker network.
"""
import argparse
from hashlib import sha256
import gzip
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import ipaddress
import os
from pathlib import Path
import re
import sqlite3
import signal
import threading
from urllib.parse import urlsplit, parse_qs

from .archive_fixture import MARKER
from .archive_io import ArchiveError, atomic_json
from .archive_jobs import ArchiveJobs, JOB_ID
from .archive_submission import Submissions
from .archive_diagnostics import diagnostic_issues
from .queue_model_cache import QueueModelCache
from .intake import read_bytes
from .order_feed_io import read_json
from .order_feed_view import apply_feed
from .queue_source import sqlite_snapshot
from .review import validate_model
from .review_render import render_review
from .video_preview_view import preview_map
from .photo_previews import PhotoPreviews
from .photo_preview_index import PhotoPreviewIndex
from .video_pending_scope import publish_scope, bound_scope, VideoDisplayCache
from .photo_preview_warmup import PhotoWarmup
from .discarded_view import project as display_materials
from .page_loading import LOADING_PAGE, loading_page
from .review_page_cache import ReviewPageCache
from .review_display_store import DisplayStore
from .review_control_watch import ControlWatch
from .review_projection import compact_archive, pending_view, history_page, page_signature
from .discovery_status import snapshot as discovery_snapshot

from .workflow_status import service_origin, snapshot as workflow_snapshot

def renderer_fingerprint(directory):
    """Identity of the shipped console code.

    The persisted pending page is keyed by the material it shows; without the
    code identity a rebuilt image keeps serving the page an older build saved,
    so a UI fix can look like it never shipped.
    """
    code = sha256()
    for path in sorted(Path(directory).iterdir()):
        if path.is_file() and path.suffix in ('.py', '.js'):
            code.update(path.name.encode())
            code.update(path.read_bytes())
    return code.hexdigest()


MAX_BODY = 8 * 1024 * 1024


def accepts_gzip(value):
    """Honor explicit refusal; do not match substrings such as x-gzip."""
    weights = {}
    for item in value.lower().split(','):
        name, *params = item.strip().split(';')
        weight = 1.0
        for param in params:
            key, _, raw = param.strip().partition('=')
            if key == 'q':
                try:
                    weight = float(raw)
                except ValueError:
                    weight = 0.0
        weights[name.strip()] = weight
    return 0 < weights.get('gzip', weights.get('*', 0)) <= 1


def job_summary(job):
    """Browser task list projection; full job/receipts remain available unchanged."""
    fields = ('job_id', 'status', 'summary', 'archive_options', 'projects', 'phase',
              'current_file_bytes', 'issues', 'execution_strategy', 'direct_move_recovery_required', 'completed_at', 'finished_at', 'completedAt', 'verification')
    result = {key: job[key] for key in fields if key in job}
    result['errors'] = list(job.get('errors') or []) + [
        row['error'] for row in job.get('outcomes', []) if row.get('error')]
    result['issues'] = list(job.get('issues') or []) + [
        issue for row in job.get('outcomes', []) for issue in row.get('issues', [])]
    return result


def queue_model(queue):
    state=read_json(Path(queue)/'队列状态.json')
    path=state.get('base_model_path')
    if not isinstance(path,str) or not re.fullmatch(r'combined/sha256:[a-f0-9]{64}/确认模型\.json',path):
        raise ArchiveError('当前还没有可提交的确认快照')
    model=read_json(Path(queue)/path)
    validate_model(model)
    model,_=apply_feed(queue,model)
    return model


def make_server(jobs, queue, *, host='127.0.0.1', port=0, public_origin=None, allow_lan_origin=False, photo_previews=None, ingest_url=None):
    if host not in ('127.0.0.1','0.0.0.0'):
        raise ValueError('控制台监听地址无效')
    # 0.0.0.0 is for a container with a loopback-only published host port.
    if host=='0.0.0.0' and public_origin is None:
        raise ValueError('容器模式必须明确本机回环访问地址')
    queue=Path(queue)
    submissions = Submissions(jobs)
    ingest_url = service_origin(ingest_url)
    # Only presentation is persistent. All preflight/submit providers remain live.
    durable = (queue / '队列状态.json').exists()
    display_store = DisplayStore(jobs.state / 'review-display') if durable else None
    photo_index = PhotoPreviewIndex(jobs.state / 'photo-preview-index') if durable and photo_previews is not None else None
    photo_warmup = PhotoWarmup(photo_index, photo_previews) if photo_index is not None else None
    video_display = VideoDisplayCache(jobs.state/'preview-scopes/current.json', queue/'video-previews') if durable else None
    restore_once = True
    renderer = renderer_fingerprint(Path(__file__).parent)
    def display_key():
        return ('pending-display/6-source-preparation', jobs.identity, jobs.enabled, jobs.move_enabled,
                jobs.label, ingest_url, page_signature(queue, jobs.state), renderer)
    def build_page(phase):
        nonlocal restore_once
        key = display_key()
        if restore_once and display_store is not None:
            restore_once = False
            saved = display_store.load(key)
            if saved is not None:
                try:
                    if saved.get('video_scope') is None:raise ValueError('missing_video_scope')
                    bound_scope(jobs.state/'preview-scopes/current.json', saved['video_scope'])
                except (OSError,ValueError,KeyError,TypeError):
                    saved = None
            if saved is not None:
                if photo_index is not None:
                    try:
                        photo_index.activate(saved.get('photo_index'))
                        photo_warmup.start_scope(saved['photo_index'])
                    except (OSError, ValueError, sqlite3.DatabaseError):
                        saved = None
                if saved is not None:
                    phase('读取已保存的待处理页面')
                    return saved
        phase('读取当前素材清单')
        model = jobs.model()
        phase('核对已归档记录')
        archive = jobs.history.snapshot(model)
        phase('核对待处理来源')
        hidden = {row['unit_id'] for row in archive['archived_units'] if row.get('in_current_model')}
        hidden.update(archive.get('retained_unit_ids', []))
        materials = display_materials(model, jobs.staging, skip_source_ids=hidden)
        # Store canonical evidence for explicit live rechecks, never archive from
        # the skipped-source presentation overlay.
        with jobs.mutex:
            jobs.material_reports[model['report_id']] = model
        view, material_view = pending_view(model, archive, materials)
        # Source-only preparation can run before any project is chosen. It
        # consumes the current pending view, never historical archived media.
        pending_ids = {u['unit_id'] for u in view['units']}
        jobs.source_preparation.schedule([u for u in model['units'] if u['unit_id'] in pending_ids], scope_key=key)
        summary = compact_archive(archive, {u['unit_id'] for u in model['units'] if u.get('kind') == 'video'})
        discovery = discovery_snapshot(queue) if (queue/'队列状态.json').exists() else None
        workflow = workflow_snapshot(model, archive, materials, discovery, ingest_url) if ingest_url else None
        descriptor = photo_index.build(view) if photo_index is not None else None
        video_scope = publish_scope(jobs.state/'preview-scopes', view) if durable else None
        initial_photos = {}
        if descriptor is not None:
            photo_warmup.start_scope(descriptor)
            offset = 0
            while True:
                ids = photo_index.priority_ids(descriptor, offset)
                if not ids: break
                # Embed cached statuses only. One background feeder owns
                # prewarming; page generation never fills the decode queue.
                initial_photos.update(photo_previews.request_units(photo_index.lookup(descriptor, model['report_id'], ids), display_only=True, enqueue=False)['previews'])
                offset += len(ids)
        phase('准备待处理页面')
        html = render_review(view, preview_map(queue/'video-previews', view, prefix='video-previews/'),
                             project_root_label=jobs.label, submission_enabled=True,
                             photo_previews_enabled=photo_previews is not None, move_enabled=jobs.move_enabled,
                             archive_state=summary, material_state=material_view,
                             discovery_state=discovery, workflow_state=workflow, help_enabled=True, photo_preview_state=initial_photos)
        bundle = {'html_bytes': html.encode(), 'report_id': model['report_id'],
                  'archive_state': archive, 'archive_summary': summary,
                  'history_generation': summary['history_generation'], 'photo_index': descriptor, 'video_scope':video_scope}
        if display_store is not None:
            bundle = display_store.save(key, bundle)
        return bundle
    page_cache = ReviewPageCache(build_page, display_key, ttl=None if durable else 30,
                                 display_grace=300)
    def controls_changed():
        nonlocal restore_once
        import time
        restore_once = False
        atomic_json(jobs.state_fd, 'review-display-change.json', {'changed_at_ns': time.time_ns()})
        page_cache.invalidate()
    watch = ControlWatch([jobs.state / name for name in
        ('requests', 'units', 'external-history')], controls_changed) if durable else None

    def displayed_job(job, compact):
        result = job_summary(job) if compact else dict(job)
        if job.get('status') == 'completed':
            worker = getattr(jobs, 'handoff', None)
            if worker:
                result['archive_handoff'] = worker.status(job['job_id'], job.get('completed_at'))
        return result
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):
            pass  # Request payloads contain private project information.

        def common_headers(self,content_type,cache_control="no-store"):
            self.send_header('Content-Type',content_type)
            self.send_header('Cache-Control',cache_control)
            self.send_header('X-Content-Type-Options','nosniff')
            self.send_header('X-Frame-Options','DENY')
            self.send_header('Cross-Origin-Resource-Policy','same-origin')
            self.send_header('Referrer-Policy','no-referrer')
            self.send_header('Content-Security-Policy',"default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")

        def reply(self,status,value,kind='application/json; charset=utf-8', *, cache_control='no-store'):
            data=json.dumps(value,ensure_ascii=False,separators=(',', ':')).encode() if kind.startswith('application/json') and not isinstance(value, bytes) else value
            compressible = kind.startswith(('application/json', 'text/html'))
            encoded = compressible and len(data) >= 1024 and accepts_gzip(self.headers.get('Accept-Encoding', ''))
            if encoded:
                data = gzip.compress(data, compresslevel=1, mtime=0)
            self.send_response(status)
            self.common_headers(kind,cache_control)
            if compressible:
                self.send_header('Vary', 'Accept-Encoding')
            if encoded:
                self.send_header('Content-Encoding', 'gzip')
            self.send_header('Content-Length',str(len(data)))
            self.end_headers()
            try:
                self.wfile.write(data)
            except (BrokenPipeError,ConnectionResetError):
                pass

        def allowed(self,write=False):
            # Container health checks stay local even when the browser uses LAN.
            if (not write and urlsplit(self.path).path=='/health'
                    and ipaddress.ip_address(self.client_address[0]).is_loopback
                    and self.headers.get('Host')=='127.0.0.1:'+str(self.server.server_port)
                    and self.headers.get('Origin') is None
                    and self.headers.get('Sec-Fetch-Site') in (None,'none')):
                return True
            if self.headers.get('Host') != urlsplit(self.server.public_origin).netloc:
                self.reply(403,{'error':'访问地址不符，请使用已配置的本机入口'})
                return False
            fetch=self.headers.get('Sec-Fetch-Site')
            origin=self.headers.get('Origin')
            navigation = (not write and self.command == 'GET' and urlsplit(self.path).path == '/'
                and ingest_url and origin is None
                and self.headers.get('Sec-Fetch-Mode') == 'navigate'
                and self.headers.get('Sec-Fetch-Dest') == 'document'
                and self.headers.get('Referer') in (ingest_url, ingest_url + '/'))
            if navigation:
                return True
            if fetch not in (None,'none','same-origin') or (origin is not None and origin != self.server.public_origin):
                self.reply(403,{'error':'拒绝跨来源访问'})
                return False
            if write and (origin != self.server.public_origin or self.headers.get('X-Muli-Request')!='1'):
                self.reply(403,{'error':'请从归档控制台明确提交，离线页面不能写入'})
                return False
            return True

        def do_OPTIONS(self):
            self.reply(403,{'error':'不允许跨来源请求'})

        def do_GET(self):
            if not self.allowed():
                return
            route=urlsplit(self.path).path
            try:
                # Fixed local help assets never touch the review or media providers.
                if route in ('/help/', '/help/index.html'):
                    return self.reply(200, (Path(__file__).with_name('help')/'index.html').read_bytes(), 'text/html; charset=utf-8')
                if route in {f'/help/step-{i}.png' for i in range(1, 7)}:
                    return self.reply(200, (Path(__file__).with_name('help')/route.rsplit('/', 1)[1]).read_bytes(), 'image/png')
                if route=='/health':
                    return self.reply(200,{'service':'muli-sorter-console','version':'0.16.2',
                                           'submission_enabled':jobs.enabled,'move_enabled':jobs.move_enabled,'example_data':not jobs.production,
                                           'worker_running':bool(jobs.thread and jobs.thread.is_alive())})
                if route=='/':
                    return self.reply(200,loading_page(ingest_url).encode(),'text/html; charset=utf-8')
                if route=='/api/discovery-status':
                    return self.reply(200, discovery_snapshot(queue))
                if route=='/api/review-status':
                    return self.reply(200, page_cache.status())
                if route=='/api/review-page':
                    status, data, kind = page_cache.read()
                    if status==200 and photo_warmup is not None:
                        bundle = page_cache.get_bundle()
                        ready = photo_warmup.snapshot(bundle.get('photo_index')) if bundle else {}
                        if ready:
                            encoded = json.dumps(ready,ensure_ascii=False,separators=(',',':')).replace('<','\\u003c').encode()
                            data = re.sub(rb'(<script id="photo-preview-ready" type="application/json">).*?(</script>)',lambda m:m[1]+encoded+m[2],data,count=1,flags=re.S)
                    if status==200 and video_display is not None:
                        bundle=page_cache.get_bundle()
                        if bundle:
                            video_ready=video_display.read(bundle.get('video_scope'))
                            encoded=json.dumps(video_ready,ensure_ascii=False,separators=(',',':')).replace('<','\\u003c').encode()
                            data=re.sub(rb'(<script id="review-previews" type="application/json">).*?(</script>)',lambda m:m[1]+encoded+m[2],data,count=1,flags=re.S)
                    return self.reply(status,data,kind)
                if route=='/api/archive-history':
                    query = parse_qs(urlsplit(self.path).query, keep_blank_values=True)
                    if set(query) - {'report_id', 'generation', 'offset', 'limit'} or any(len(v) != 1 for v in query.values()):
                        raise ValueError('历史分页参数无效')
                    report_id = query.get('report_id', [''])[0]
                    if not report_id or len(report_id) > 200:
                        raise ValueError('素材报告编号无效')
                    generation = query.get('generation', [''])[0]
                    if len(generation) > 200:
                        raise ValueError('历史版本无效')
                    offset, limit = int(query.get('offset', ['0'])[0]), int(query.get('limit', ['50'])[0])
                    if offset < 0 or not 1 <= limit <= 50:
                        raise ValueError('历史分页范围无效')
                    bundle = page_cache.get_display_bundle()
                    if bundle is None:
                        status, payload, kind = page_cache.read()
                        if status != 200:
                            return self.reply(status, payload, kind)
                        bundle = page_cache.get_display_bundle()
                        if bundle is None:
                            return self.reply(202, {'status': 'building'})
                    return self.reply(200, history_page(bundle, report_id, generation, offset, limit))
                if route=='/api/material-state':
                    query = parse_qs(urlsplit(self.path).query)
                    values = query.get('report_id', [])
                    if len(values) != 1 or len(values[0]) > 200:
                        raise ValueError('素材报告编号无效')
                    try:
                        result = jobs.material_state(report_id=values[0])
                    except ValueError:
                        # A restored display contains no canonical source evidence.
                        current = jobs.model()
                        if current['report_id'] != values[0]:
                            raise ValueError('来源清单已更新，请保存草稿后刷新页面')
                        result = jobs.material_state(current)
                    return self.reply(200, result)
                if route=='/api/archive-state':
                    query = parse_qs(urlsplit(self.path).query)
                    if query.get('view') == ['compact'] and len(query.get('report_id', [])) == 1:
                        saved = page_cache.get_bundle(query['report_id'][0])
                        if saved is not None and 'archive_summary' in saved:
                            return self.reply(200, saved['archive_summary'])
                    if 'report_id' in query:
                        if len(query['report_id']) != 1 or len(query['report_id'][0]) > 200:
                            raise ValueError('素材报告编号无效')
                        try:
                            state = jobs.history.for_report(query['report_id'][0])
                        except ValueError:
                            current = jobs.model()
                            if current['report_id'] != query['report_id'][0]:
                                raise ValueError('归档清单已更新，请保存草稿后刷新页面')
                            state = jobs.history.snapshot(current)
                            jobs.material_state(current)
                        with jobs.mutex:
                            page_model = jobs.material_reports.get(query['report_id'][0])
                    else:
                        page_model = jobs.model()
                        state = jobs.history.snapshot(page_model)
                    page_cache.invalidate()
                    parents = {u['unit_id'] for u in (page_model or {}).get('units', []) if u.get('kind') == 'video'}
                    return self.reply(200, compact_archive(state, parents) if query.get('view') == ['compact'] else state)
                if route=='/api/jobs':
                    listed = jobs.list_jobs(summary=parse_qs(urlsplit(self.path).query).get('view') == ['summary'])
                    if parse_qs(urlsplit(self.path).query).get('view') == ['summary']:
                        listed = [displayed_job(job, True) for job in listed]
                    return self.reply(200,{'jobs':listed})
                if re.fullmatch(r'/api/submissions/[a-f0-9]{64}',route):
                    return self.reply(200,submissions.get(route.rsplit('/',1)[-1]))
                if re.fullmatch(r'/api/preflight-checks/[a-f0-9]{64}',route):
                    return self.reply(200,jobs.checks.get(route.rsplit('/',1)[-1]))
                if re.fullmatch(r'/api/confirmation-tickets/[a-f0-9]{64}',route):
                    return self.reply(200,jobs.confirmation(route.rsplit('/',1)[-1]))
                if route.startswith('/api/jobs/') and JOB_ID.fullmatch(route[len('/api/jobs/'):]):
                    job = jobs.get(route[len('/api/jobs/'):])
                    compact = parse_qs(urlsplit(self.path).query).get('view') == ['summary']
                    return self.reply(200,displayed_job(job, compact))
                if photo_previews is not None and re.fullmatch(r'/photo-previews/[a-f0-9]{64}\.jpg',route):
                    data=read_bytes(photo_previews.output,route.rsplit('/',1)[-1])
                    if len(data)>512*1024:
                        raise ValueError('照片预览过大')
                    return self.reply(200,data,'image/jpeg',cache_control='private, max-age=86400, immutable')
                if re.fullmatch(r'/video-previews/[a-fA-F0-9]{64}\.jpg',route):
                    data=read_bytes(queue,route.lstrip('/'))
                    if len(data)>2*1024*1024:
                        raise ValueError('截图文件过大')
                    return self.reply(200,data,'image/jpeg',cache_control='private, max-age=86400, immutable')
                self.reply(404,{'error':'入口不存在'})
            except FileNotFoundError:
                self.reply(404,{'error':'任务或截图不存在'})
            except (ValueError,KeyError,TypeError,OSError,sqlite3.DatabaseError) as exc:
                self.reply(409,{'error':str(exc)})

        def do_POST(self):
            if not self.allowed(write=True):
                return
            route=urlsplit(self.path).path
            body = None
            try:
                if self.headers.get('Content-Type','').split(';')[0] != 'application/json' or self.headers.get('Transfer-Encoding'):
                    raise ValueError('请求格式不支持')
                length=int(self.headers.get('Content-Length','0'))
                if not 0<length<=MAX_BODY:
                    raise ValueError('提交内容大小无效')
                self.connection.settimeout(15)
                raw=self.rfile.read(length)
                if len(raw)!=length:
                    raise ValueError('请求未完整接收')
                body=json.loads(raw)
                if not isinstance(body,dict):
                    raise ValueError('请求应为对象')
                if route=='/api/photo-previews' and photo_previews is not None and set(body)=={'report_id','unit_ids'}:
                    if photo_index is not None:
                        bundle = page_cache.get_bundle(body['report_id'])
                        if bundle is None:
                            raise ValueError('素材快照正在更新，请稍后重新加载预览')
                        subset = photo_index.lookup(bundle['photo_index'], body['report_id'], body['unit_ids'])
                        return self.reply(200,photo_previews.request_units(subset, display_only=True, scope_token=bundle['photo_index']['file']))
                    return self.reply(200,photo_previews.request(jobs.model(),body['report_id'],body['unit_ids']))
                if route=='/api/preflight' and set(body)=={'decisions'}:
                    return self.reply(200,jobs.preflight(body['decisions']))
                if route=='/api/preflight-checks' and set(body)=={'client_id','revision','decisions'}:
                    return self.reply(202,jobs.checks.start(body['client_id'],body['revision'],body['decisions']))
                if route=='/api/preflight-checks/cancel' and set(body)=={'client_id','revision'}:
                    return self.reply(200,jobs.checks.cancel(body['client_id'],body['revision']))
                if route=='/api/submissions' and set(body)=={'preview_id','decisions','confirmed'}:
                    return self.reply(202,submissions.start(body['preview_id'],body['decisions'],body['confirmed']))
                if route=='/api/submit' and set(body)=={'preview_id','decisions','confirmed'}:
                    result = jobs.submit(body['preview_id'],body['decisions'],body['confirmed'])
                    page_cache.invalidate()
                    return self.reply(202,result)
                match=re.fullmatch(r'/api/jobs/([a-f0-9]{64})/retry',route)
                if match and set(body)=={'confirmed'}:
                    result = jobs.retry(match[1],body['confirmed'])
                    page_cache.invalidate()
                    return self.reply(202,result)
                self.reply(404,{'error':'提交入口或字段不符'})
            except (ValueError,KeyError,TypeError,OSError,sqlite3.DatabaseError) as exc:
                result = {'error':str(exc)}
                if isinstance(body, dict) and route in ('/api/submit', '/api/submissions'):
                    token = body.get('preview_id')
                    ticket = jobs.previews.get(token) if isinstance(token, str) else None
                    ticket = ticket or {}
                    result['issues'] = diagnostic_issues(exc, ticket.get('model'),
                        ticket.get('scoped_decisions', body.get('decisions')))
                self.reply(409,result)
    class ReviewServer(ThreadingHTTPServer):
        def server_close(self):
            submissions.close()
            if photo_warmup is not None:
                photo_warmup.close()
            if photo_index is not None:
                photo_index.close(final=True)
            page_cache.shutdown()
            if watch is not None:
                watch.close()
            super().server_close()
    server=ReviewServer((host,port),Handler)
    server.page_cache = page_cache
    server.photo_index = photo_index
    server.photo_warmup = photo_warmup
    origin=public_origin or 'http://127.0.0.1:'+str(server.server_port)
    parsed=urlsplit(origin)
    valid_host=parsed.hostname in ('127.0.0.1','localhost')
    if allow_lan_origin:
        try:
            address=ipaddress.ip_address(parsed.hostname)
            valid_host=valid_host or any(address in ipaddress.ip_network(cidr) for cidr in ('10.0.0.0/8','172.16.0.0/12','192.168.0.0/16'))
        except ValueError:
            pass
    if parsed.scheme!='http' or not valid_host or parsed.path or parsed.query or parsed.fragment or parsed.username:
        server.server_close()
        raise ValueError('控制台只接受本机地址或明确启用的局域网 IPv4 地址')
    server.public_origin=origin
    return server


def main(argv=None):
    parser=argparse.ArgumentParser(description='独立素材归档控制台；默认不启用项目写入')
    parser.add_argument('--queue',required=True)
    parser.add_argument('--staging',required=True)
    parser.add_argument('--projects',required=True)
    parser.add_argument('--state',required=True)
    parser.add_argument('--state-db')
    parser.add_argument('--project-label')
    parser.add_argument('--port',type=int,default=18767)
    parser.add_argument('--host',choices=['127.0.0.1','0.0.0.0'],default='127.0.0.1')
    parser.add_argument('--public-origin')
    parser.add_argument('--ingest-url', help='独立拷贝备份服务的公开根地址，仅用于页面交接')
    parser.add_argument('--allow-lan-origin',action='store_true',help='仅用于已限制来源的独立局域网网关')
    parser.add_argument('--enable-project-writes',action='store_true')
    parser.add_argument('--enable-source-removal',action='store_true',help='允许明确提交的移动任务在目标校验后逐文件清理中转')
    parser.add_argument('--direct-move-root',help='已确认的单一挂载项目根；内含素材中转，仅用于新MOVE任务')
    parser.add_argument('--demo-root',help='仅使用带合成标记的独立实验目录')
    args=parser.parse_args(argv)
    if args.demo_root:
        demo=Path(args.demo_root).absolute()
        if str(demo).startswith('/Volumes/') or read_json(demo/'SYNTHETIC_ONLY.json') != MARKER:
            parser.error('演示入口只接受独立合成实验')
        if Path(args.staging).absolute()!=demo/'staging' or Path(args.projects).absolute()!=demo/'projects':
            parser.error('演示输入与目标必须来自同一合成实验目录')
        def runtime():
            value=read_json(demo/'runtime-state.json')
            value['generated_at']=datetime.now(timezone.utc).isoformat()
            return value
        provider=lambda:read_json(demo/'model.json')
    else:
        if not args.state_db:
            parser.error('生产模式必须提供现有 Ingest 只读状态库')
        runtime=lambda:sqlite_snapshot(args.state_db)
        provider=QueueModelCache(args.queue)
    jobs=ArchiveJobs(args.staging,args.projects,args.state,provider,runtime,
                     production=not bool(args.demo_root),enabled=args.enable_project_writes,project_label=args.project_label,move_enabled=args.enable_source_removal,
                     direct_move_view={'root':args.direct_move_root,'staging':'素材中转','projects':'.'} if args.direct_move_root else None)
    photos=PhotoPreviews(jobs.staging,jobs.state/'photo-previews')
    server=make_server(jobs,args.queue,photo_previews=photos,host=args.host,port=args.port,public_origin=args.public_origin,
                       allow_lan_origin=args.allow_lan_origin, ingest_url=args.ingest_url)
    def stop(*_):
        threading.Thread(target=server.shutdown,daemon=True).start()
    signal.signal(signal.SIGTERM,stop)
    signal.signal(signal.SIGINT,stop)
    jobs.handoff = None
    if os.environ.get('MULI_ARCHIVE_HANDOFF_CONFIG'):
        try:
            from .handoff_protocol import configuration
            from .archive_handoff import ArchiveHandoff
            cfg = configuration(os.environ['MULI_ARCHIVE_HANDOFF_CONFIG'])
            jobs.handoff = ArchiveHandoff(jobs,cfg['outbox'],cfg['acknowledgements'],cfg['since'],
                idle=lambda: not jobs.worker_busy.is_set() and server.page_cache.status().get('status')=='ready')
            jobs.handoff.start()
        except (OSError,ValueError,KeyError,TypeError):
            print(json.dumps({'service':'archive-handoff','status':'configuration_needs_review'}),flush=True)
    jobs.start()
    server.page_cache.warm(lambda: not any(job.get("status") in ("queued", "running") for job in jobs.list_jobs(summary=True)))
    print(json.dumps({'service':'muli-sorter-console','url':server.public_origin,'example_data':not jobs.production,
                      'submission_enabled':jobs.enabled}),flush=True)
    try:
        server.serve_forever(poll_interval=0.25)
    finally:
        if jobs.handoff:
            jobs.handoff.close()
        server.server_close()
        photos.close()
        jobs.close()


if __name__=='__main__':
    main()
