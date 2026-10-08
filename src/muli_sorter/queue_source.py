"""Minimal, read-only completion snapshots. Never import Ingest or read auth tables."""
from datetime import datetime, timezone
import sqlite3
import json
import subprocess
from pathlib import Path
from .intake import BATCH, EvidenceError


FIELDS = ('batch_id', 'batch_uid', 'revision', 'state', 'result', 'completed_at')
MAX_BATCHES = 10000
PROJECTION = ','.join("json_extract(data,'$."+f+"') AS "+f for f in FIELDS)
SQL = "SELECT "+PROJECTION+" FROM objects WHERE kind='batch' ORDER BY id LIMIT "+str(MAX_BATCHES+1)


def validate_snapshot(snapshot, now=None):
    if not isinstance(snapshot, dict) or snapshot.get('complete_listing') is not True:
        raise EvidenceError('需要完整批次状态列表，不能用截断的最近批次列表')
    now = now or datetime.now(timezone.utc)
    try:
        timestamp = datetime.fromisoformat(snapshot['generated_at'])
        if timestamp.tzinfo is None or not -5 <= (now-timestamp).total_seconds() <= 120:
            raise ValueError()
        rows = snapshot['batches']
        if not isinstance(rows, list) or len(rows) > MAX_BATCHES:
            raise ValueError()
        ids, uids = set(), set()
        for row in rows:
            if not isinstance(row, dict) or any(k not in row for k in FIELDS):
                raise ValueError()
            bid, uid = row['batch_id'], row['batch_uid']
            if not isinstance(bid, str) or not BATCH.fullmatch(bid) or not isinstance(uid, str) or not uid or len(uid)>200:
                raise ValueError()
            if bid in ids or uid in uids or type(row['revision']) is not int or row['revision'] < 0:
                raise ValueError()
            if not isinstance(row['state'], str) or (row['result'] is not None and not isinstance(row['result'], str)):
                raise ValueError()
            if row['completed_at'] is not None and not isinstance(row['completed_at'], str):
                raise ValueError()
            ids.add(bid)
            uids.add(uid)
    except (KeyError, ValueError, TypeError) as exc:
        raise EvidenceError('批次状态列表过期、重复或格式错误') from exc
    return snapshot


def sqlite_snapshot(path):
    """Version-coupled adapter for an already authorized local read-only state mount.

    mode=ro and query_only are intentional; immutable is unsafe with active WAL.
    No schema migration, writes, auth reads or application module imports.
    """
    path = Path(path).absolute()
    if path.is_symlink() or not path.is_file():
        raise EvidenceError('状态数据库必须是已存在的普通文件')
    started = datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(path.as_uri()+'?mode=ro', uri=True, timeout=1)
    try:
        conn.execute('PRAGMA query_only=ON')
        conn.row_factory = sqlite3.Row
        rows = conn.execute(SQL).fetchall()
        if len(rows)>MAX_BATCHES:
            raise EvidenceError('批次数超过读取上限；需要分页适配，不能静默遗漏')
        return validate_snapshot({'generated_at':started, 'complete_listing':True,
                                  'source':'ingest_sqlite_readonly', 'batches':[dict(r) for r in rows]})
    finally:
        conn.close()


def ssh_snapshot(host, database):
    """Read via an existing, trusted SSH identity; no credential file inspection."""
    if not host or host.startswith('-') or any(c.isspace() for c in host):
        raise EvidenceError('SSH 主机别名无效')
    if not database.startswith('/'):
        raise EvidenceError('远程状态库需要绝对路径')
    program = f'''import sqlite3,json
from pathlib import Path
from datetime import datetime,timezone
started=datetime.now(timezone.utc).isoformat()
c=sqlite3.connect(Path({database!r}).as_uri()+'?mode=ro',uri=True,timeout=1)
c.execute('PRAGMA query_only=ON')
c.row_factory=sqlite3.Row
rows=c.execute({SQL!r}).fetchall()
print(json.dumps({{'generated_at':started,'complete_listing':True,'source':'ingest_ssh_sqlite_readonly','batches':[dict(r) for r in rows]}}))
c.close()
'''
    result = subprocess.run(['ssh','-o','BatchMode=yes','-o','StrictHostKeyChecking=yes',
                             '-o','ConnectTimeout=5',host,'python3 -'],input=program,
                            capture_output=True,text=True,timeout=8)
    if result.returncode or len(result.stdout)>8*1024*1024:
        raise EvidenceError('SSH 只读状态采集失败；不更换身份或放宽主机核验')
    return validate_snapshot(json.loads(result.stdout))
