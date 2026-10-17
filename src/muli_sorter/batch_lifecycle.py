"""Workflow retirement, independent of media deletion and live cleanup leases."""
from datetime import datetime, timezone
import json
import time
from .intake import EvidenceError

INACTIVE = ('closed', 'cancelled', 'needs_attention', 'superseded')


def confirmed_handoff(row, clock=None):
    """Only a fresh, full, revision-bound archive acknowledgement may close."""
    from .completion_facts import confirmed
    if confirmed(row):
        return True
    clock = time.time() if clock is None else clock
    h = row.get('archive_handoff')
    if not isinstance(h, dict) or row['state'] != 'COMPLETED' or row['result'] not in ('COPY_VERIFIED','COPY_SIZE_VERIFIED'):
        return False
    try:
        checked = datetime.fromisoformat(h['checked_at'])
        expiry = datetime.fromisoformat(h['expires_at'])
        return (checked.tzinfo is not None and expiry.tzinfo is not None
                and -5 <= clock-checked.timestamp() <= 1800
                and clock < expiry.timestamp() <= checked.timestamp()+1800
                and h['status'] == 'confirmed' and type(h['revision']) is int and h['revision'] == row['revision']
                and type(h['total_file_count']) is int and h['total_file_count'] > 0
                and type(h['confirmed_file_count']) is int
                and h['confirmed_file_count'] == h['total_file_count']
                and type(h['missing_file_count']) is int and h['missing_file_count'] == 0
                and h.get('source_disposition') in ('moved','retained'))
    except (KeyError,ValueError,TypeError):
        return False


def handoff_evidence(row):
    from .completion_facts import confirmed
    return row['archive_completion'] if confirmed(row) else row['archive_handoff']


def initialize(db):
    db.execute('''CREATE TABLE IF NOT EXISTS batch_workflow (
        job_id TEXT PRIMARY KEY, batch_id TEXT NOT NULL, uid TEXT NOT NULL,
        revision INTEGER NOT NULL, state TEXT NOT NULL, changed_at TEXT NOT NULL,
        evidence TEXT NOT NULL)''')
    db.execute('CREATE INDEX IF NOT EXISTS batch_workflow_state ON batch_workflow(state,job_id)')


def retire(db, job, state, evidence):
    if state not in ('closed','cancelled','needs_attention'):
        raise EvidenceError('批次退出状态无效')
    db.execute('INSERT OR REPLACE INTO batch_workflow VALUES(?,?,?,?,?,?,?)',
               (job['id'],job['batch_id'],job['uid'],job['revision'],state,
                datetime.now(timezone.utc).isoformat(),json.dumps(evidence,ensure_ascii=False)))
    db.execute('UPDATE jobs SET state=?,error=NULL WHERE id=?',(state,job['id']))


def cancel(db, job_id, confirmed):
    if confirmed is not True:
        raise EvidenceError('必须明确确认取消本批次分类；不会删除素材')
    row = db.execute('SELECT * FROM jobs WHERE id=?',(job_id,)).fetchone()
    if row is None:
        raise EvidenceError('批次分类任务不存在')
    job = dict(row)
    if job['state'] == 'closed':
        raise EvidenceError('已结案批次无需取消')
    if job['state'] != 'cancelled':
        retire(db,job,'cancelled',{'operation':'cancel_classification','media_deleted':False})
    return {'job_id':job_id,'state':'cancelled','media_deleted':False}


def history_page(db, offset=0, limit=50):
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 50:
        raise EvidenceError('历史分页范围无效')
    where="state IN ('closed','cancelled','needs_attention','superseded')"
    total=db.execute('SELECT count(*) FROM jobs WHERE '+where).fetchone()[0]
    rows=[dict(r) for r in db.execute(
        'SELECT id,batch_id,uid,revision,state,first_seen,ready_at,error FROM jobs WHERE '+where+
        ' ORDER BY first_seen DESC,id LIMIT ? OFFSET ?',(limit,offset))]
    return {'total':total,'offset':offset,'next_offset':offset+len(rows) if offset+len(rows)<total else None,'jobs':rows}


def read_history(staging, projects, state, offset=0, limit=50):
    """Read a running classifier's history without a lock, migration, or write."""
    from pathlib import Path
    import sqlite3
    root=Path(state).absolute()
    path=root/'queue.sqlite3'
    if root.is_symlink() or path.is_symlink() or not path.is_file():
        raise EvidenceError('分类状态数据库路径不安全')
    db=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=1)
    try:
        db.execute('PRAGMA query_only=ON');db.row_factory=sqlite3.Row
        identity=db.execute("SELECT value FROM meta WHERE key='identity'").fetchone()
        if not identity:
            raise EvidenceError('分类状态缺少来源身份')
        identity=json.loads(identity[0])
        if identity.get('staging')!=str(Path(staging).resolve(strict=True)) or identity.get('projects')!=str(Path(projects).resolve(strict=True)):
            raise EvidenceError('分类状态绑定其他来源，不能读取此历史')
        return history_page(db,offset,limit)
    finally:
        db.close()
