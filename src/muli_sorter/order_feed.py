"""Native, read-only Feishu refresh task; uses only Python 3.11 stdlib.

Credentials stay with the existing NAS CLI. This process has no media writer,
folder HTTP client, AI client, or code path that updates Ingest.
"""
import argparse
from collections import Counter
from datetime import date, datetime, timezone
import fcntl
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
import threading
import time

from .matching import project_index
from .order_catalog import OrderError, read_orders, read_product_codes
from .order_feed_io import atomic_json, canonical, digest, directory, read_json
from .review import identity_digest

SCHEMA = 'order-feed/0.6'
TTL = 600


def utc(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


def epoch(value):
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError('时间必须包含时区')
    return parsed.timestamp()


def queue_reference(root, now):
    state = read_json(Path(root) / '队列状态.json')
    source = state.get('source') or {}
    if (state.get('observer_stopped') is not False or source.get('ok') is not True or
            not -5 <= now - epoch(source['checked_at']) <= 120):
        raise OrderError('分类来源状态不可用或过期')
    path = state.get('base_model_path')
    if not path:
        page = state.get('confirmation_page')
        if not isinstance(page, str) or not page.endswith('/拍摄段确认.html'):
            raise OrderError('还没有完整分类模型')
        path = page.rsplit('/', 1)[0] + '/确认模型.json'
    if not isinstance(path, str) or not re.fullmatch(r'combined/sha256:[0-9a-f]{64}/确认模型.json', path):
        raise OrderError('分类模型路径无效')
    return path


def load_model(root, reference):
    model = read_json(Path(root) / reference)
    expected = identity_digest(model)
    if (model.get('schema_version') != '0.2' or model.get('report_id') != expected or
            reference.split('/')[1] != expected or model.get('order_catalog_evidence')):
        raise OrderError('分类模型摘要不符或不是基础分类模型')
    return model


def explicit_dates(model):
    result = set()
    for unit in model['units']:
        value = unit.get('capture_date')
        if value is not None:
            try:
                if date.fromisoformat(value).isoformat() != value:
                    raise ValueError()
            except (TypeError, ValueError):
                raise OrderError('素材拍摄日期格式无效') from None
            result.add(value)
    return sorted(result)


def candidate_products(orders, projects):
    existing = {p.get('order_id') for p in projects}
    counts = Counter(o['order_id'] for o in orders)
    result = set()
    for order in orders:
        stages, ids = order.get('stage') or [], order['product_record_ids']
        if (order['order_id'] not in existing and counts[order['order_id']] == 1 and
                isinstance(stages, list) and all(isinstance(s, str) for s in stages) and
                not any(any(w in s for w in ('取消', '退款')) for s in stages) and
                len(ids) == 1 and order.get('customer_name')):
            result.add(ids[0])
    return sorted(result)


def semantic(content):
    catalog = {k: v for k, v in content['catalog'].items() if k not in ('fetched_at', 'table_revision')}
    return {**content, 'catalog': catalog}


def same_projects(projects, model_projects):
    fields = ('project_id', 'order_id', 'path', 'dates')
    projection = lambda rows: sorted(({k: p.get(k) for k in fields} for p in rows), key=lambda p: p['project_id'])
    return projection(projects) == projection(model_projects)


def build_feed(model, projects, resource, reader, namer, *, now, previous=None):
    # Wait for the classification index to catch up before reading orders.
    if not same_projects(projects, model['projects']):
        raise OrderError('目录索引变化，等待分类模型更新')
    dates = explicit_dates(model)
    orders, revisions, seen = [], set(), set()
    for offset in range(0, len(dates), 60):
        part = read_orders(resource, dates[offset:offset + 60], reader=reader)
        revisions.add(part['table_revision'])
        for order in part['orders']:
            if order['record_id'] in seen:
                raise OrderError('跨日期查询出现重复订单记录')
            seen.add(order['record_id'])
            orders.append(order)
    if len(revisions) > 1:
        raise OrderError('读取期间订单表版本改变')
    orders.sort(key=lambda o: (o['order_id'], o['record_id']))
    ids, products = candidate_products(orders, projects), {}
    if len(ids) > 200:
        raise OrderError('待查套餐超过单次读取上限，未将多份产品快照拼成成功结果')
    if ids:
        products = read_product_codes(resource, ids, reader=reader)
    names = {}
    for order in orders:
        linked = order['product_record_ids']
        if len(linked) != 1 or not products.get(linked[0]):
            continue
        payload = {'order_id': order['order_id'], 'shoot_date': order['shoot_date'],
                   'customer_name': order['customer_name'], 'package_code': products[linked[0]]}
        try:
            path, name = namer(payload)
            path = Path(path)
            if path.is_absolute():
                path = path.relative_to('/projects')
            if len(path.parts) != 3 or any(p in ('.', '..', '') for p in path.parts) or path.name != name:
                raise ValueError('命名返回值不符')
            names[digest(payload)] = {'path': path.as_posix()}
        except (TypeError, ValueError, KeyError):
            names[digest(payload)] = {'error': '现有目录命名规则拒绝该订单'}
    content = {'catalog': {'schema_version': 'orders/0.5', 'source': 'feishu_cli_readonly',
                'fetched_at': utc(now), 'table_revision': next(iter(revisions), None),
                'complete_for_dates': dates, 'orders': orders},
               'product_codes': products, 'folder_names': names}
    previous_valid = (isinstance(previous, dict) and previous.get('schema_version') == SCHEMA and
                      previous.get('base_report_id') == model['report_id'] and
                      previous.get('content_id') == digest(previous.get('content')))
    if previous_valid and semantic(previous['content']) == semantic(content):
        content = previous['content']
    return {'schema_version': SCHEMA, 'base_report_id': model['report_id'],
            'content_id': digest(content), 'content': content,
            'verified_at': utc(now), 'valid_until': utc(now + TTL),
            'table_revision_at_verification': next(iter(revisions), None),
            'media_write_authorized': False, 'folder_write_authorized': False}


def nas_cli_reader(arguments):
    if arguments[:2] not in (['base', '+record-list'], ['base', '+record-get']):
        raise OrderError('只允许读取订单与产品投影')
    if '--as' not in arguments or arguments[arguments.index('--as') + 1] != 'user':
        raise OrderError('只使用现有默认用户身份')
    result = subprocess.run(['docker', 'exec', 'muli-agent-os-app', 'lark-cli', *arguments],
                            capture_output=True, text=True, timeout=60)
    if result.returncode:
        raise OrderError('NAS 飞书读取失败；未切换身份或重新登录')
    data = json.loads(result.stdout)
    if data.get('ok') is not True or not isinstance(data.get('data'), dict):
        raise OrderError('NAS 飞书未返回完整结果')
    return data['data']


def folder_namer(path, expected_sha256):
    path = Path(path)
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise ValueError('需要明确的可信命名模块')
    if not isinstance(expected_sha256, str) or not re.fullmatch('[0-9a-f]{64}', expected_sha256):
        raise ValueError('需要已核验命名模块的 SHA256')
    with directory(path.parent) as fd:
        source = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        try:
            if not stat.S_ISREG(os.fstat(source).st_mode):
                raise ValueError('命名模块必须是普通文件')
            with os.fdopen(source, 'rb', closefd=False) as file:
                code = file.read(1024 * 1024 + 1)
        finally:
            os.close(source)
    if len(code)>1024*1024 or hashlib.sha256(code).hexdigest()!=expected_sha256:
        raise ValueError('命名模块已改变，需重新核验')
    sys.dont_write_bytecode = True
    # Execute the exact verified bytes, closing the verify/import race.
    import types
    module = types.ModuleType('muli_existing_folder_namer')
    module.__file__ = str(path)
    exec(compile(code, str(path), 'exec'), module.__dict__)
    return lambda payload: module.project_paths(payload, Path('/projects'))


class OrderFeedWorker:
    def __init__(self, queue_root, projects, resource, reader, namer, *, refresh=300, clock=time.time):
        if not isinstance(refresh, (int, float)) or not math.isfinite(refresh) or not 30 <= refresh <= TTL / 2:
            raise ValueError('刷新间隔必须为 30 至 300 秒')
        self.root, self.projects = Path(queue_root), Path(projects)
        self.resource, self.reader, self.namer = resource, reader, namer
        self.refresh, self.clock = refresh, clock
        self.output = self.root / 'order-feed'
        self.failures, self.retry_at = 0, 0

    def step(self):
        now = self.clock()
        status = {'schema_version': SCHEMA, 'checked_at': utc(now), 'state': 'waiting',
                  'last_verified_at': None, 'base_report_id': None, 'stopped': False}
        if now < self.retry_at:
            status.update(state='retry_wait', retry_at=utc(self.retry_at))
            atomic_json(self.output / '状态.json', status)
            return status
        try:
            try:
                previous = read_json(self.output / 'current.json')
            except FileNotFoundError:
                previous = None
            if previous:
                status['last_verified_at'] = previous.get('verified_at')
            reference = queue_reference(self.root, now)
            base_id = reference.split('/')[1]
            status['base_report_id'] = base_id
            if (previous and previous.get('schema_version') == SCHEMA and
                  previous.get('content_id') == digest(previous.get('content')) and
                  previous.get('base_report_id') == base_id and
                  0 <= now - epoch(previous['verified_at']) < self.refresh):
                status['state'] = 'ready'
            else:
                model = load_model(self.root, reference)
                feed = build_feed(model, project_index(self.projects), self.resource, self.reader,
                                  self.namer, now=now, previous=previous)
                finished = self.clock()
                if queue_reference(self.root, finished) != reference or load_model(self.root, reference)['report_id'] != base_id:
                    raise OrderError('读取期间分类模型改变，丢弃本次结果')
                if not same_projects(project_index(self.projects), model['projects']):
                    raise OrderError('读取期间项目目录改变，丢弃本次结果')
                if finished - now > 120:
                    raise OrderError('读取耗时超过本次快照允许范围')
                feed.update(verified_at=utc(finished), valid_until=utc(finished + TTL))
                atomic_json(self.output / 'current.json', feed)
                self.failures, self.retry_at = 0, 0
                status.update(state='ready', last_verified_at=feed['verified_at'],
                              order_count=len(feed['content']['catalog']['orders']))
        except Exception as exc:
            self.failures += 1
            self.retry_at = now + min(30 * 2 ** min(self.failures - 1, 4), 300)
            status.update(state='error', error_type=type(exc).__name__, retry_at=utc(self.retry_at),
                          message='本次订单刷新不可用；保留历史结果并稍后重试')
        atomic_json(self.output / '状态.json', status)
        return status


def main(argv=None):
    parser = argparse.ArgumentParser(description='独立只读飞书订单刷新')
    parser.add_argument('--queue', required=True)
    parser.add_argument('--projects', required=True)
    parser.add_argument('--staging', required=True)
    parser.add_argument('--resource', required=True)
    parser.add_argument('--folder-service-module', required=True)
    parser.add_argument('--folder-service-sha256', required=True)
    parser.add_argument('--interval', type=float, default=5)
    parser.add_argument('--refresh', type=float, default=300)
    parser.add_argument('--once', action='store_true')
    args = parser.parse_args(argv)
    if not math.isfinite(args.interval) or args.interval < 5:
        parser.error('检查新模型的间隔不能小于 5 秒')
    root, projects = Path(args.queue).absolute(), Path(args.projects).absolute()
    for protected in (projects, Path(args.staging).absolute(), Path('/Volumes')):
        if root == protected or root.is_relative_to(protected) or protected.is_relative_to(root):
            parser.error('订单输出必须与中转、项目及 Mac 挂载媒体目录分离')
    marker = read_json(root / '队列状态.json')
    if not {'observer_stopped', 'source'} <= marker.keys():
        parser.error('目标不是已存在的分类服务状态目录')
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    worker = OrderFeedWorker(root, projects, read_json(args.resource), nas_cli_reader,
                             folder_namer(args.folder_service_module, args.folder_service_sha256), refresh=args.refresh)
    with directory(worker.output, create_leaf=True) as fd:
        lock = os.open('.lock', os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=fd)
        try:
            if not stat.S_ISREG(os.fstat(lock).st_mode):
                raise ValueError('锁文件类型不符')
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            while not stop.is_set():
                result = worker.step()
                if args.once:
                    return 0 if result['state'] == 'ready' else 1
                stop.wait(args.interval)
            atomic_json(worker.output / '状态.json', {'schema_version': SCHEMA,
                        'checked_at': utc(time.time()), 'state': 'stopped', 'stopped': True})
        finally:
            os.close(lock)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
