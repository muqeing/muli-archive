"""Offline rollback export, never a rollback or media operation."""
import argparse
import json
from pathlib import Path
from .archive_io import directory,exclusive_lock
from .archive import record
from .archive_reservations import ReservationIndex


def main(argv=None):
    p=argparse.ArgumentParser(description='停服后导出新预留索引，供核准回退使用；不替换旧记录')
    p.add_argument('--state',required=True)
    p.add_argument('--filename',required=True,help='新文件名 reservations-rollback-NAME.json')
    p.add_argument('--confirmed',action='store_true')
    args=p.parse_args(argv)
    if not args.confirmed:p.error('需要明确确认写入一个新导出文件；不会改写现有记录')
    state=Path(args.state).absolute()
    with directory(state) as fd,exclusive_lock(fd):
        identity=record(fd,'service-identity.json')
        if not identity or not (state/'reservations.sqlite3').is_file():
            p.error('需要已初始化的归档预留索引')
        index=ReservationIndex(state,identity)
        try:result=index.export_rollback(args.filename)
        finally:index.close()
    print(json.dumps(result,ensure_ascii=False))
    return 0


if __name__=='__main__':raise SystemExit(main())
