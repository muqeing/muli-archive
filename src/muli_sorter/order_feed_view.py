"""Consume a bounded order snapshot without networking in the classifier."""
from collections import Counter
from datetime import datetime
from pathlib import Path
import time

from .order_feed import epoch, explicit_dates, SCHEMA
from .order_feed_io import digest, read_json
from .order_projects import plan_folders, enrich_confirmation


def apply_feed(root, model, *, now=None, cache=None, share_immutable=False):
    now = time.time() if now is None else now
    result = {'state': 'not_connected', 'message': '订单持续刷新尚未接入',
              'last_verified_at': None, 'folder_intent_counts': {}, 'order_count': None}
    try:
        state = read_json(Path(root) / 'order-feed/状态.json')
        result['last_verified_at'] = state.get('last_verified_at')
        if state.get('schema_version') != SCHEMA or state.get('stopped') is not False:
            raise ValueError('订单刷新已停止或状态无效')
        if not -5 <= now - epoch(state['checked_at']) <= 180:
            raise ValueError('订单刷新任务心跳已过期')
        if state.get('state') != 'ready':
            result.update(state='unavailable', message='订单刷新暂不可用；当前仅显示目录候选')
            return model, result
        feed = read_json(Path(root) / 'order-feed/current.json')
        if (feed.get('schema_version') != SCHEMA or feed.get('base_report_id') != model['report_id'] or
                state.get('base_report_id') != model['report_id'] or
                feed.get('media_write_authorized') is not False or feed.get('folder_write_authorized') is not False):
            result.update(state='waiting', message='等待当前素材的订单候选更新')
            return model, result
        verified, expires = epoch(feed['verified_at']), epoch(feed['valid_until'])
        if not -5 <= now - verified <= 600 or not verified < expires <= verified + 600 or now > expires:
            raise ValueError('订单快照已过期')
        content = feed['content']
        if digest(content) != feed['content_id']:
            raise ValueError('订单快照内容摘要不符')
        if content['catalog']['complete_for_dates'] != explicit_dates(model):
            raise ValueError('订单查询日期与素材不一致')
        key = (model['report_id'], feed['content_id'], share_immutable)
        if cache is not None and cache.get('key') == key:
            enriched, counts = cache['model'], cache['counts']
        else:
            def namer(payload):
                entry = content['folder_names'][digest(payload)]
                if entry.get('error'):
                    raise ValueError(entry['error'])
                return entry['path']
            intents = plan_folders(content['catalog'], model['projects'], content['product_codes'], namer)
            if cache is not None:
                cache.clear()  # Release the previous order generation first.
            enriched = enrich_confirmation(model, content['catalog'], intents, share_immutable=share_immutable)
            counts = dict(Counter(row['action'] for row in intents['intentions']))
            if cache is not None:
                cache.update(key=key, model=enriched, counts=counts)
        result.update(state='ready', message='飞书订单候选已更新，归属仍待确认',
                      last_verified_at=feed['verified_at'],
                      order_count=len(content['catalog']['orders']), folder_intent_counts=counts)
        return enriched, result
    except FileNotFoundError:
        return model, result
    except Exception:
        result.update(state='unavailable', message='订单结果不可用或已过期；当前仅显示目录候选')
        return model, result
