"""Offline queue dashboard and one deduplicated confirmation snapshot."""
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo
from html import escape
import json
from .queue_files import publish, publish_lazy
from .intake import read_bytes, decode, EvidenceError
from .review import build_review_model, canonical, digest, validate_model
from .review_render import render_review
from .order_feed_view import apply_feed
from .video_preview_view import render_with_previews, preview_source_key

LABELS = {'waiting_completion':'等待拷贝校验完成','queued':'已入队，等待整理','processing':'正在整理',
          'awaiting_confirmation':'已整理，待确认归属','retry_wait':'需要重试 / 核对','superseded':'旧版本已停用',
          'verification_required':'需要精准内容校验证据'}


def combine(queue, status):
    if not (status['source'] or {}).get('ok'):
        return None
    groups, metadata, snapshots = defaultdict(dict), {}, []
    excluded=[]
    for job in status['jobs']:
        if job['state']!='awaiting_confirmation':
            excluded.append({'batch_id':job['batch_id'],'status':job['state'],'reasons':[job['error'] or LABELS[job['state']]]})
            continue
        model=decode(read_bytes(queue.root,job['artifact']+'/确认模型.json'))
        validate_model(model)
        if digest(model)!=job['model_hash']:
            raise EvidenceError('已保存的确认模型发生变化')
        snapshots.append(model['snapshot_at'])
        for unit in model['units']:
            for p in unit['provenance']:
                key=(p['batch_id'],p['manifest_id'])
                g=groups[key].setdefault(p['group_id'],{'group_id':p['group_id'],'file_count':0,'units':[]})
                g['units'].append(unit)
                g['file_count']+=unit['file_count']
        metadata.update({p['project_id']:p for p in model['projects']})
    if not snapshots:
        return None
    report={'mode':'read_only_preview','example_data':queue.examples,'generated_at':max(snapshots),
            'projects':sorted(metadata.values(),key=lambda p:p['project_id']),
            'batches':[{'batch_id':b,'manifest_id':m,'status':'verified','groups':list(g.values())}
                       for (b,m),g in sorted(groups.items())]+excluded}
    # The report is private to this call. Let the builder consume its groups
    # instead of retaining all decoded input units beside the combined output.
    groups.clear()
    del model
    return build_review_model(report, _consume_input=True)


def _publish_model(queue, relative, model, *, checkpoint=None):
    # Model identity is immutable. Preview dependencies can change within that
    # identity, so invalidate publication before checking the on-disk bundle.
    sources=getattr(queue,'bundle_preview_sources',{})
    source_key=preview_source_key(queue,model)
    if sources.get(relative)!=source_key:
        queue.bundle_cache.pop(relative,None)
    publish_lazy(queue.root,relative,{
        '确认模型.json':lambda:canonical(model),
        '拍摄段确认.html':lambda:render_with_previews(queue,model),
    },cache=queue.bundle_cache,checkpoint=checkpoint)
    sources[relative]=source_key
    queue.bundle_preview_sources=sources


def export(queue, status, *, stopped=False):
    key=digest([(status['source'] or {}).get('ok'),[(j['id'],j['state'],j['artifact'],j['model_hash'],j['error']) for j in status['jobs']]])
    cache=getattr(queue,'view_cache',None)
    if cache and cache[0]==key:
        model=cache[1]
    else:
        # Drop the previous generation before building its replacement. Keeping
        # it in the local tuple and the order cache doubled the cold peak.
        cache=None
        queue.view_cache=None
        getattr(queue,'order_cache',{}).clear()
        getattr(queue,'preview_render_cache',{}).clear()
        queue.preview_source_cache=None
        model=combine(queue,status)
        queue.view_cache=(key,model)
    base_model_path=None
    link=None
    order_status={'state':'waiting','message':'等待素材完成分类后读取订单'}
    if model:
        if not hasattr(queue,'bundle_cache'):
            queue.bundle_cache={}
        base_relative='combined/'+model['report_id']
        _publish_model(queue,base_relative,model,
                checkpoint=lambda name:queue.checkpoint('combined_model_written') if name=='确认模型.json' else None)
        base_model_path=base_relative+'/确认模型.json'
        if not hasattr(queue,'order_cache'):
            queue.order_cache={}
        model,order_status=apply_feed(queue.root,model,cache=queue.order_cache,share_immutable=True)
        relative='combined/'+model['report_id']
        if relative!=base_relative:
            _publish_model(queue,relative,model)
        # Cached signatures are small, but old generations need not accumulate.
        for cache_name in ('bundle_cache','bundle_preview_sources'):
            small_cache=getattr(queue,cache_name,{})
            for old_path in list(small_cache):
                if old_path not in (base_relative,relative):small_cache.pop(old_path,None)
        link=relative+'/拍摄段确认.html'
    active=[j for j in status['jobs'] if j['state']!='superseded']
    counts={'batches':len(active),'prepared_batches':sum(j['state']=='awaiting_confirmation' for j in active),
            'units':len(model['units']) if model else 0,'candidate_segments':len(model['initial_segments']) if model else 0,
            'unique_files':sum(u['file_count'] for u in model['units']) if model else 0}
    display={**status,'observer_stopped':stopped,'summary':counts,'confirmation_page':link,
             'base_model_path':base_model_path,'orders':order_status}
    source=status['source'] or {}
    state='本次观察已结束 · 以下为快照' if stopped else '本地只读观察 · 刷新页面查看新结果'
    health='状态读取正常' if source.get('ok') else '状态读取失败，已暂停生成新确认页'
    cards=''.join(f'<article><strong>{escape(j["batch_id"])}</strong><span>{LABELS[j["state"]]}</span><p>{escape(j["error"] or "仅整理项目候选，尚未归档")}</p></article>' for j in active)
    action=f'<a class="button" href="{escape(link,quote=True)}">打开合并后的待确认素材</a>' if link else '<p>暂时没有可确认的素材。</p>'
    error=f'<p>{escape(source.get("error",""))}</p>' if not source.get('ok') else ''
    checked=source.get('checked_at')
    checked=datetime.fromisoformat(checked).astimezone(ZoneInfo('Asia/Shanghai')).strftime('%Y-%m-%d %H:%M:%S 北京时间') if checked else '未知'
    order_message=escape(order_status['message'])
    order_details=''
    if order_status.get('state')=='ready':
        c=order_status['folder_intent_counts']
        order_details=f"<p>{order_status['order_count']} 条订单 · {c.get('use_existing',0)} 条对应已有目录 · {c.get('create_required',0)} 条待建目录 · {c.get('needs_review',0)} 条需核对</p>"
    if order_status.get('last_verified_at'):
        at=datetime.fromisoformat(order_status['last_verified_at']).astimezone(ZoneInfo('Asia/Shanghai')).strftime('%Y-%m-%d %H:%M:%S 北京时间')
        order_details+=f'<small>最近订单核验：{escape(at)}</small>'
    html=f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>素材分类队列</title>
<style>body{{font:16px/1.6 system-ui;background:#f5f3ee;color:#23352d;margin:0}}main{{max-width:940px;margin:auto;padding:28px 18px}}h1{{font-size:28px}}.muted{{color:#647068}}.banner,article,.stats{{background:white;border:1px solid #d9ddd4;border-radius:14px;padding:18px;margin:14px 0}}.stats{{display:flex;gap:24px;flex-wrap:wrap}}.stats b{{font-size:28px;display:block}}article strong{{display:block;overflow-wrap:anywhere}}article span{{color:#3b6854}}p{{overflow-wrap:anywhere}}.button{{display:inline-block;background:#285c43;color:white;border-radius:9px;padding:12px 18px;text-decoration:none}}small{{display:block;overflow-wrap:anywhere}}</style>
<main><p class="muted">木梨 · 素材归属</p><h1>素材分类队列</h1><p>{state}</p><small>采集：{escape(checked)}</small>
<div class="banner"><strong>{health}</strong>{error}<p>只读取拷贝状态和清单。所有归属仍待确认，真实文件没有归档。</p></div>
<div class="stats"><div><b>{counts['batches']}</b>个当前批次</div><div><b>{counts['prepared_batches']}</b>批已整理</div><div><b>{counts['candidate_segments']}</b>段候选拍摄</div><div><b>{counts['unique_files']}</b>个去重文件</div></div>
<div class="banner"><strong>{order_message}</strong>{order_details}<p>订单读取独立运行；暂时不可用时继续整理素材，保留已生成的历史确认页。</p></div>
{action}<p class="muted">同一中转原件的跨批重复引用合并确认；按拍摄日期匹配历史项目。</p>{cards}
<p class="muted">{'合成实验。' if queue.examples else ''}当前页面是只读状态快照。确认页面保存归属计划，不触发项目文件写入。</p></main></html>'''
    publish(queue.root,None,{'队列状态.json':(json.dumps(display,ensure_ascii=False,indent=2)+'\n').encode(),
                             '分类队列.html':html.encode()})
    return display
