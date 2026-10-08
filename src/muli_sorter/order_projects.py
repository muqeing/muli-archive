"""Read-only folder intentions, using the existing folder service naming contract."""
from collections import defaultdict
from copy import deepcopy
from hashlib import sha256
import re
from .intake import relative
from .review import digest, identity_digest, validate_model


def plan_folders(catalog, projects, product_codes, folder_namer):
    """folder_namer is the existing service's pure project_paths adapter.

    No mkdir, HTTP write or media action. Existing sales-number matches win over
    spelling differences, preventing a second folder for the same order.
    """
    by_order, counts = defaultdict(list), defaultdict(int)
    for project in projects:
        if project.get('order_id'):
            by_order[project['order_id']].append(project)
    for order in catalog['orders']:
        counts[order['order_id']]+=1
    result=[]
    for order in catalog['orders']:
        row={'order_id':order['order_id'],'record_id':order['record_id'],'shoot_date':order['shoot_date'],
             'action':'needs_review','path':None,'reason':None,'folder_write_authorized':False}
        existing=by_order[order['order_id']]
        stages=order.get('stage') or []
        if not isinstance(stages,list) or any(not isinstance(x,str) for x in stages):
            row['reason']='订单阶段格式未知'
        elif any(any(word in stage for word in ('取消','退款')) for stage in stages):
            row['reason']='订单取消或退款状态需要核对'
        elif counts[order['order_id']]!=1:
            row['reason']='销售编号在查询结果中不唯一'
        elif len(existing)>1:
            row['reason']='同一销售编号对应多个项目目录'
        elif len(existing)==1:
            p=existing[0]
            row['path']=p['path']
            if order['shoot_date'] not in p.get('dates',[]):
                row['reason']='订单拍摄日与已有目录日期不同，不自动另建或改名'
            else:
                row.update(action='use_existing',project_id=p['project_id'],reason='唯一销售编号及拍摄日一致')
        else:
            codes={product_codes[r] for r in order['product_record_ids'] if product_codes.get(r)}
            if len(codes)!=1 or len(order['product_record_ids'])!=1:
                row['reason']='关联套餐缺失或不唯一'
            elif not order.get('customer_name'):
                row['reason']='订单缺少建目录所需姓名'
            elif not re.fullmatch(r'\d{5}',order['order_id']):
                row['reason']='销售编号格式无效'
            else:
                payload={'order_id':order['order_id'],'shoot_date':order['shoot_date'],
                         'customer_name':order['customer_name'],'package_code':next(iter(codes))}
                try:
                    path=folder_namer(payload)
                    parts=relative(path)
                    if len(parts)!=3 or parts[0]!=order['shoot_date'][:4] or not parts[2].startswith(order['shoot_date'].replace('-','')+'_'+order['order_id']+'_'):
                        raise ValueError('目录命名与订单身份不符')
                    row.update(action='create_required',path=path,reason='飞书订单唯一，项目目录缺失；待受限执行器新建',
                               folder_request={'service':'photo-project-folder-service','payload':payload})
                except (ValueError,TypeError,KeyError) as exc:
                    row['reason']='现有目录规则拒绝该订单：'+str(exc)
        result.append(row)
    # Include spelling/case collisions across new targets and current folders.
    targets=defaultdict(list)
    for row in result:
        if row['path']:
            targets[row['path'].casefold()].append(row)
    existing_paths={p['path'].casefold() for p in projects}
    for key, rows in targets.items():
        for row in rows:
            if row['action']=='create_required' and (len(rows)>1 or key in existing_paths):
                row.update(action='needs_review',reason='拟新建目录与另一个目标冲突')
                row.pop('folder_request',None)
    return {'schema_version':'folder-intents/0.5','source_fetched_at':catalog['fetched_at'],
            'folder_write_authorized':False,'intentions':result}


def enrich_confirmation(model,catalog,intents, *, share_immutable=False):
    """Add live-read order evidence and clearly marked missing-folder candidates.

    Associations remain pending; a date match is never an execution decision.
    """
    validate_model(model)
    if share_immutable:
        # Queue export owns immutable snapshots and only edits project evidence
        # and each unit's candidate IDs. Share untouched descriptors/segments.
        result={**model, 'projects':deepcopy(model['projects']),
                'units':[dict(unit) for unit in model['units']]}
    else:
        # Public callers retain the original fully detached output contract.
        result=deepcopy(model)
    projects={p['project_id']:p for p in result['projects']}
    orders={o['record_id']:o for o in catalog['orders']}
    for intent in intents['intentions']:
        if intent['action']=='use_existing':
            p=projects[intent['project_id']]
            p['order_evidence']={'source':'feishu_cli_readonly','record_id':intent['record_id'],
                                 'fetched_at':catalog['fetched_at'],'shooting_at':orders[intent['record_id']]['shooting_at']}
            p['folder_action']='use_existing'
        elif intent['action']=='create_required':
            path=intent['path']
            pid='dir-'+sha256(path.encode()).hexdigest()[:16]
            if pid in projects:
                continue
            projects[pid]={'project_id':pid,'order_id':intent['order_id'],'name':'【待建目录】'+path.rsplit('/',1)[-1],
                           'path':path,'dates':[intent['shoot_date']],'evidence_source':'feishu_order_folder_intent',
                           'folder_action':'create_after_confirmed_assignment','exists':False,
                           'order_evidence':{'source':'feishu_cli_readonly','record_id':intent['record_id'],
                                             'fetched_at':catalog['fetched_at'],'shooting_at':orders[intent['record_id']]['shooting_at']}}
    result['projects']=list(projects.values())
    added=set(projects)-{p['project_id'] for p in model['projects']}
    for unit in result['units']:
        unit['candidate_project_ids']=sorted(set(unit['candidate_project_ids'])|{pid for pid in added if unit['capture_date'] in projects[pid]['dates']})
    result['order_catalog_evidence']={'source':catalog['source'],'fetched_at':catalog['fetched_at'],
                                      'covered_dates':catalog['complete_for_dates'],'order_count':len(catalog['orders']),
                                      'date_matches_are_candidates_only':True,'folder_write_authorized':False}
    result['report_id']=identity_digest(result)
    validate_model(result)
    return result
