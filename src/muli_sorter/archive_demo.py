"""Only create/run local synthetic archives; no production execution command."""
import argparse
from html import escape
import json
from pathlib import Path
from urllib.parse import quote
from .archive import run_synthetic
from .archive_fixture import prepare
from .cli import atomic_output, load_json
from .review_render import render_review


def render_result(root, model, report):
    labels={'completed':'已归档并校验','pending':'等待项目确认','deferred':'已暂缓','incomplete':'未完成，需要处理'}
    units={u['unit_id']:u for u in model['units']}
    cards=[]
    for outcome in report['outcomes']:
        unit=units[outcome['unit_id']]
        content=[]
        if outcome['status']=='completed':
            receipt=load_json(root/'state'/outcome['receipt'])
            content.append('<p>项目：'+escape(receipt['project']['name'])+'</p>')
            for row in receipt['files']:
                dest=quote('projects/'+row['target_path'])
                src=quote('staging/'+row['source_path'])
                content.append(f'<li>{escape(row["name"])} · <a href="{src}">中转原件</a> · <a href="{dest}">项目副本</a></li>')
            content.append('<p><a href="'+quote('state/'+outcome['receipt'])+'">查看逐文件校验回执</a></p>')
        else:
            content.extend('<li>'+escape(f['name'])+'</li>' for f in unit['files'])
        if outcome.get('error'):
            content.append('<p class="warn">'+escape(outcome['error'])+'</p>')
        cards.append('<section><h2>'+escape((unit.get('capture_date') or '日期未知')+' · '+labels[outcome['status']])+'</h2><ul>'+''.join(content)+'</ul></section>')
    summary=report['summary']
    repeat=load_json(root/'重复运行结果.json') if (root/'重复运行结果.json').exists() else None
    reused=f'<p>重复运行：重新核验并复用 {repeat["summary"]["reused_files"]} 个文件，新增复制 {repeat["summary"]["written_bytes"]} 字节。</p>' if repeat else ''
    return '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>合成素材归档验收</title><style>body{font:16px/1.6 -apple-system,sans-serif;background:#f3f5f4;color:#25312a;margin:0}main{max-width:950px;margin:auto;padding:24px}section{background:white;border:1px solid #dce4df;border-radius:12px;padding:20px;margin:18px 0}h1{font-size:27px}h2{font-size:19px}a{color:#17644b}p,li{overflow-wrap:anywhere}.warn{color:#954310}.badge{background:#fff0c8;padding:14px;border-radius:8px}</style><main><h1>确认后的素材已进入合成项目文件夹</h1><p class="badge">这是合成实验。图片是纯色测试图，视频是短测试片；没有操作真实客户素材，也没有部署 NAS。</p>'+f'<p>本次 {summary["completed_units"]} 组、{summary["completed_files"]} 个文件完成独立复制和完整内容校验；{summary["pending_units"]} 组待确认，{summary["incomplete_units"]} 组未完成。</p>'+reused+'<p>未确认的素材不会跟随其他素材归档。点击下方链接可分别打开中转原件、实际项目副本和回执。</p>'+''.join(cards)+'<section><h2>查看实验文件</h2><p><a href="拍摄段确认.html">合成确认页面</a> · <a href="归档结果.json">本次结果</a> · <a href="重复运行结果.json">重复运行结果</a></p><p>本页是结果快照；中断恢复和冲突处理另有独立测试，不代表实际 NAS 断电或长期运行已经验收。</p></section></main></html>'


def main(argv=None):
    parser=argparse.ArgumentParser(description='合成素材归档实验；拒绝真实确认模型和挂载卷')
    mode=parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--output',help='新建独立的本地合成实验目录')
    mode.add_argument('--workspace',help='继续已有合成实验目录')
    parser.add_argument('--prepare-only',action='store_true')
    parser.add_argument('--decisions',help='同份合成确认页下载的计划')
    args=parser.parse_args(argv)
    if args.prepare_only and not args.output:
        parser.error('prepare-only 只用于创建新实验')
    root=Path(args.output or args.workspace).absolute()
    if args.output:
        model,decisions=prepare(root)
        atomic_output(root/'拍摄段确认.html',render_review(model).encode())
    else:
        model,decisions=load_json(root/'model.json'),load_json(root/'decisions.json')
    if args.prepare_only:
        print(json.dumps({'mode':'synthetic_prepared','workspace':str(root)},ensure_ascii=False))
        return 0
    if args.decisions:
        decisions=load_json(args.decisions)
    report=run_synthetic(root,model,decisions)
    atomic_output(root/'归档结果.json',json.dumps(report,ensure_ascii=False,indent=2).encode())
    repeat=run_synthetic(root,model,decisions)
    atomic_output(root/'重复运行结果.json',json.dumps(repeat,ensure_ascii=False,indent=2).encode())
    atomic_output(root/'归档结果.html',render_result(root,model,report).encode())
    print(json.dumps({'workspace':str(root),'first_run':report['summary'],'repeat':repeat['summary'],'real_media_write_authorized':False},ensure_ascii=False))
    return 2 if report['summary']['incomplete_units'] or repeat['summary']['incomplete_units'] else 0


if __name__=='__main__':
    raise SystemExit(main())
