"""Finite by default; continuous observation requires explicit --cycles 0."""
import argparse
import json
import time
from .cli import load_json
from .discovery_queue import DiscoveryQueue
from .queue_source import sqlite_snapshot, ssh_snapshot
from .queue_view import export


def main(argv=None):
    p=argparse.ArgumentParser(description='独立只读分类队列，不复制或移动真实素材')
    p.add_argument('--staging',required=True)
    p.add_argument('--projects',required=True)
    p.add_argument('--output',required=True)
    sources=p.add_mutually_exclusive_group(required=False)
    sources.add_argument('--snapshot')
    sources.add_argument('--state-db')
    sources.add_argument('--ssh-host',help='已有且主机指纹已确认的 SSH 别名')
    p.add_argument('--remote-db',help='仅配合 SSH：已获只读授权的状态数据库绝对路径')
    p.add_argument('--cycles',type=int,default=1,help='默认只观察一轮；0 持续运行')
    p.add_argument('--interval',type=float,default=2,help='每轮结束后的间隔秒数，最少 2 秒')
    p.add_argument('--synthetic',action='store_true')
    p.add_argument('--enable-batch-lifecycle',action='store_true')
    operations=p.add_mutually_exclusive_group()
    operations.add_argument('--history',action='store_true',help='分页查看已退出的批次；不读取源素材')
    operations.add_argument('--cancel-job',help='取消此分类任务，不删除素材；分类服务须先停止以释放单实例锁')
    p.add_argument('--confirm-cancel',action='store_true')
    p.add_argument('--offset',type=int,default=0)
    p.add_argument('--limit',type=int,default=50)
    a=p.parse_args(argv)
    if not (a.history or a.cancel_job) and not any((a.snapshot,a.state_db,a.ssh_host)):
        p.error('需要一个只读状态来源')
    if (a.history or a.cancel_job) and not a.enable_batch_lifecycle:
        p.error('历史和取消操作需显式启用批次生命周期')
    if a.confirm_cancel and not a.cancel_job:
        p.error('confirm-cancel只能用于明确的cancel-job')
    if a.cycles<0 or a.interval<2 or (bool(a.ssh_host)!=bool(a.remote_db)):
        p.error('检查轮数、至少2秒间隔，以及 SSH/数据库参数配对')
    if a.synthetic and not a.snapshot:
        p.error('合成实验只接受显式的合成快照文件')
    if a.history:
        from .batch_lifecycle import read_history
        print(json.dumps(read_history(a.staging,a.projects,a.output,a.offset,a.limit),ensure_ascii=False))
        return 0
    provider=(lambda:load_json(a.snapshot)) if a.snapshot else (lambda:ssh_snapshot(a.ssh_host,a.remote_db,include_handoff=a.enable_batch_lifecycle)) if a.ssh_host else (lambda:sqlite_snapshot(a.state_db,include_handoff=a.enable_batch_lifecycle))
    if a.state_db:
        from pathlib import Path
        source=Path(a.state_db).resolve(strict=True)
        output=Path(a.output).resolve()
        if output==source.parent or output.is_relative_to(source.parent) or source.is_relative_to(output):
            p.error('分类状态输出必须与 Ingest 状态目录分离')
    with DiscoveryQueue(a.staging,a.projects,a.output,allow_examples=a.synthetic,enable_lifecycle=a.enable_batch_lifecycle) as q:
        if a.history or a.cancel_job:
            result=q.cancel(a.cancel_job,a.confirm_cancel) if a.cancel_job else q.history_page(a.offset,a.limit)
            print(json.dumps(result,ensure_ascii=False))
            return 0
        i=0
        try:
            while True:
                status=q.tick(provider)
                display=export(q,status)
                i+=1
                print(json.dumps({'cycle':i,'source_ok':display['source']['ok'],**display['summary']},ensure_ascii=False),flush=True)
                if a.cycles and i>=a.cycles:
                    break
                time.sleep(a.interval)
        except KeyboardInterrupt:
            pass
        export(q,q.status(),stopped=True)
    return 0


if __name__=='__main__':
    raise SystemExit(main())
