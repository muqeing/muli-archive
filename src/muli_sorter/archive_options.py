"""Explicit per-request archive policy; old requests retain their original policy."""
from .archive_io import ArchiveError


def options_for(decisions):
    value = decisions.get('archive_options', {'mode':'copy', 'existing':'error'})
    if (not isinstance(value, dict) or set(value) not in (
            {'mode', 'existing'}, {'mode', 'existing', 'proxy_only_unit_ids'}) or
            value['mode'] not in ('copy', 'move') or value['existing'] not in ('skip_identical', 'error')):
        raise ArchiveError('归档方式或重名处理选项无效')
    if 'proxy_only_unit_ids' not in value:
        return dict(value)
    ids = value['proxy_only_unit_ids']
    if (not isinstance(ids, list) or any(type(uid) is not str or not uid for uid in ids) or
            len(ids) != len(set(ids))):
        raise ArchiveError('显式代理授权列表无效')
    return {**value, 'proxy_only_unit_ids': list(ids)}
