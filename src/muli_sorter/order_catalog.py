"""Read selected shooting dates with the existing Feishu CLI; never write Feishu."""
from datetime import date, datetime, timezone
import json
import re
import subprocess
from zoneinfo import ZoneInfo

ORDER_FIELDS=['销售编号','预约拍摄时间','客户姓名','订单环节','订单产品','素材文件夹路径','素材文件夹名称','素材文件夹状态']
ZONE=ZoneInfo('Asia/Shanghai')


class OrderError(ValueError):
    pass


def cli_read(arguments):
    if arguments[:2] not in (['base','+record-list'],['base','+record-get']):
        raise OrderError('订单入口只允许投影读取记录')
    result=subprocess.run(['lark-cli',*arguments],capture_output=True,text=True,timeout=40)
    if result.returncode:
        # Do not echo account details, OAuth links or CLI output into logs.
        raise OrderError('飞书 CLI 读取失败；检查既有登录和表权限，不自动切换身份')
    data=json.loads(result.stdout)
    if data.get('ok') is not True or not isinstance(data.get('data'),dict):
        raise OrderError('飞书 CLI 未返回成功记录结果')
    return data['data']


def matrix_rows(data, fields):
    columns=data.get('fields')
    values=data.get('data')
    ids=data.get('record_id_list')
    if not isinstance(columns,list) or len(columns)!=len(set(columns)) or set(columns)!=set(fields):
        raise OrderError('订单返回字段与最小读取范围不符')
    if not isinstance(values,list) or not isinstance(ids,list) or len(values)!=len(ids) or len(ids)!=len(set(ids)):
        raise OrderError('订单行与记录标识不一致')
    result=[]
    for rid,row in zip(ids,values):
        if not isinstance(rid,str) or not re.fullmatch(r'rec[A-Za-z0-9]+',rid) or not isinstance(row,list) or len(row)!=len(columns):
            raise OrderError('订单行格式异常')
        result.append({'record_id':rid,**dict(zip(columns,row))})
    return result


def text(value):
    if value is None:
        return ''
    if not isinstance(value,str):
        raise OrderError('预期订单文本字段，但返回了其他类型')
    return value.strip()


def shooting_time(value):
    try:
        dt=datetime.fromisoformat(value)
        if dt.tzinfo is None:
            raise ValueError()
        return dt.astimezone(ZONE)
    except (ValueError,TypeError):
        raise OrderError('订单拍摄时间缺少可靠时区') from None


def read_orders(resource, dates, *, reader=cli_read):
    dates=sorted(set(dates))
    if not dates or len(dates)>60 or any(date.fromisoformat(d).isoformat()!=d for d in dates):
        raise OrderError('订单查询只接受1至60个明确的拍摄日期')
    filter_json=json.dumps({'logic':'or','conditions':[['预约拍摄时间','==',f'ExactDate({d})'] for d in dates]},ensure_ascii=False)
    args=['base','+record-list','--base-token',resource['base_token'],'--table-id',resource['table_name'],
          '--as','user','--format','json','--filter-json',filter_json,'--limit','200',
          '--sort-json',json.dumps([{'field':'销售编号','desc':False}],ensure_ascii=False)]
    for field in ORDER_FIELDS:
        args+=['--field-id',field]
    seen, orders, offset, revision = set(), [], 0, None
    for page in range(20):
        data=reader(args+['--offset',str(offset)])
        if data.get('rev') is None:
            raise OrderError('订单返回缺少表版本，无法核对翻页一致性')
        if revision is None:
            revision=data['rev']
        elif revision!=data['rev']:
            raise OrderError('订单表在翻页期间改变，本次快照作废')
        rows=matrix_rows(data,ORDER_FIELDS)
        for row in rows:
            if row['record_id'] in seen:
                raise OrderError('翻页出现重复订单，不能跳过或猜测')
            seen.add(row['record_id'])
            dt=shooting_time(row['预约拍摄时间'])
            if dt.date().isoformat() not in dates:
                raise OrderError('飞书日期筛选返回了范围外记录，停止接受本次结果')
            number=text(row['销售编号'])
            if not re.fullmatch(r'\d{5}',number):
                raise OrderError('销售编号不是标准五位编号')
            links=row['订单产品'] or []
            if not isinstance(links,list) or any(not isinstance(x,dict) or not re.fullmatch(r'rec[A-Za-z0-9]+',x.get('id','')) for x in links):
                raise OrderError('关联产品格式异常')
            orders.append({'record_id':row['record_id'],'order_id':number,'shooting_at':dt.isoformat(),
                           'shoot_date':dt.date().isoformat(),'customer_name':text(row['客户姓名']),
                           'stage':row['订单环节'],'product_record_ids':sorted({x['id'] for x in links}),
                           'folder_path':text(row['素材文件夹路径']),'folder_name':text(row['素材文件夹名称']),
                           'folder_status':text(row['素材文件夹状态'])})
        if data.get('has_more') is False:
            return {'schema_version':'orders/0.5','source':'feishu_cli_readonly','base_title':resource.get('base_title'),
                    'table_name':resource['table_name'],'fetched_at':datetime.now(timezone.utc).isoformat(),
                    'table_revision':revision,'complete_for_dates':dates,'orders':orders}
        if data.get('has_more') is not True or not rows:
            raise OrderError('订单分页状态不完整')
        offset+=len(rows)
    raise OrderError('订单分页超过上限，没有把截断数据当作完整快照')


def read_product_codes(resource, record_ids, *, reader=cli_read):
    ids=sorted(set(record_ids))
    if len(ids)>200 or any(not re.fullmatch(r'rec[A-Za-z0-9]+',r) for r in ids):
        raise OrderError('关联产品标识范围无效')
    if not ids:
        return {}
    args=['base','+record-get','--base-token',resource['base_token'],'--table-id',resource['product_table_id'],
          '--as','user','--format','json','--field-id','产品ID']
    for rid in ids:
        args+=['--record-id',rid]
    rows=matrix_rows(reader(args),['产品ID'])
    if {r['record_id'] for r in rows}!=set(ids):
        raise OrderError('关联产品没有完整返回')
    return {r['record_id']:text(r['产品ID']) for r in rows}
