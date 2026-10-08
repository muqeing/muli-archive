"""Independent local durable discovery queue. No archive or Ingest write entry."""
from contextlib import ExitStack
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import time
from .archive_io import directory, exclusive_lock
from .cli import output_directory
from .intake import EvidenceError, _open, read_bytes, decode
import os
from .matching import project_index
from .queue_evidence import build_job, completed, signal, size_verified_candidate
from .queue_source import validate_snapshot
from .review import canonical, digest
from .review_render import render_review
from .queue_files import publish

# The queue directory "identity" record is bound to this exact value; raising
# it changes every cached job id, so force a targeted rebuild instead of a
# blanket version bump (see outputs/仓库合并与统一上线 for the procedure).
RULES = 'discovery-0.4'


class DiscoveryQueue:
    def __init__(self, staging, projects, state, *, allow_examples=False, checkpoint=None):
        self.staging, self.projects = Path(staging).resolve(strict=True), Path(projects).resolve(strict=True)
        self.root = output_directory(state, [self.staging, self.projects])
        if str(self.root).startswith('/Volumes/'):
            raise EvidenceError('队列状态须放本地磁盘，不能使用 SMB 挂载目录')
        for p in self.root.glob('queue.sqlite3*'):
            if p.is_symlink() or not p.is_file():
                raise EvidenceError('队列数据库路径不安全')
        self.stack = ExitStack()
        try:
            fd = self.stack.enter_context(directory(self.root))
            self.stack.enter_context(exclusive_lock(fd))
            self.db = sqlite3.connect(self.root/'queue.sqlite3', timeout=1)
            self.stack.callback(self.db.close)
            self.db.row_factory = sqlite3.Row
            self.db.execute('PRAGMA synchronous=FULL')
            self.db.execute('CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY,value TEXT NOT NULL)')
            self.db.execute('''CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,batch_id TEXT NOT NULL,uid TEXT NOT NULL,revision INTEGER NOT NULL,
                state TEXT NOT NULL,token TEXT,dependencies TEXT NOT NULL DEFAULT '[]',
                attempts INTEGER NOT NULL DEFAULT 0,next_try REAL NOT NULL DEFAULT 0,
                error TEXT,artifact TEXT,model_hash TEXT,first_seen REAL NOT NULL,ready_at REAL,queued_at REAL)''')
            columns={r[1] for r in self.db.execute('PRAGMA table_info(jobs)')}
            if 'queued_at' not in columns:
                self.db.execute('ALTER TABLE jobs ADD COLUMN queued_at REAL')
            identity = {'staging':str(self.staging),'projects':str(self.projects),'rules':RULES,'examples':allow_examples}
            prior = self.get_meta('identity')
            if prior is not None and prior != identity:
                raise EvidenceError('队列目录已经绑定另一来源、规则或演示模式')
            self.set_meta('identity', identity)
            self.db.execute("UPDATE jobs SET state='queued' WHERE state='processing'")
            self.db.commit()
        except BaseException:
            self.stack.close()
            raise
        self.examples = allow_examples
        self.checkpoint = checkpoint or (lambda _:None)
        self.index, self.index_at = None, 0
        self.artifact_cache = {}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.stack.close()

    def get_meta(self, key):
        row = self.db.execute('SELECT value FROM meta WHERE key=?',(key,)).fetchone()
        return json.loads(row[0]) if row else None

    def set_meta(self, key, value):
        self.db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)',(key,json.dumps(value,ensure_ascii=False)))

    def jobs(self):
        return [dict(r) for r in self.db.execute('SELECT * FROM jobs ORDER BY first_seen,id')]

    def artifact_valid(self, job):
        attrs=[]
        for name in ('确认模型.json','拍摄段确认.html'):
            fd=_open(self.root,job['artifact']+'/'+name)
            try:
                s=os.fstat(fd)
                attrs.append([s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns,s.st_ctime_ns])
            finally:
                os.close(fd)
        cache=[job['model_hash'],attrs]
        if self.artifact_cache.get(job['id'])!=cache:
            model=decode(read_bytes(self.root,job['artifact']+'/确认模型.json'))
            if digest(model)!=job['model_hash']:
                raise EvidenceError('保存的确认模型被修改，需重新生成')
            self.artifact_cache[job['id']]=cache
        return True

    def fail(self, job, error, now):
        n = job['attempts']+1
        self.db.execute("UPDATE jobs SET state='retry_wait',attempts=?,next_try=?,error=? WHERE id=?",
                        (n, now+min(60,2**min(n,6)),str(error)[:400],job['id']))

    def discover(self, provider):
        """Refresh the durable queue without running a classification job."""
        now = time.time()
        health = self.get_meta('source') or {}
        if now < health.get('retry_at',0):
            return None, self.status()
        try:
            snapshot = validate_snapshot(provider())
            if self.index is None or now-self.index_at>=60:
                self.index = project_index(self.projects)
                self.index_at = now
            self.set_meta('source',{'ok':True,'checked_at':snapshot['generated_at'],'retry_at':0,'failures':0})
        except Exception as exc:
            n = health.get('failures',0)+1
            self.set_meta('source',{'ok':False,'checked_at':datetime.now(timezone.utc).isoformat(),
                                  'failures':n,'retry_at':now+min(60,2**min(n,6)),'error':str(exc)[:400]})
            self.db.commit()
            return None, self.status()
        current = set()
        for row in snapshot['batches']:
            jid = digest([row['batch_uid'],row['revision'],RULES])
            current.add(jid)
            self.db.execute('''INSERT OR IGNORE INTO jobs (id,batch_id,uid,revision,state,first_seen)
                               VALUES (?,?,?,?,'waiting_completion',?)''',
                            (jid,row['batch_id'],row['batch_uid'],row['revision'],now))
            job = dict(self.db.execute('SELECT * FROM jobs WHERE id=?',(jid,)).fetchone())
            if job['batch_id'] != row['batch_id']:
                raise EvidenceError('同一批次身份更换目录，需核对来源')
            candidate = size_verified_candidate(self.staging, row, allow_examples=self.examples)
            if not completed(row) and candidate is None:
                state='verification_required' if row['state']=='COMPLETED' else 'waiting_completion'
                labels={'COPYING':'正在拷贝','VERIFYING':'正在校验','FINALIZING':'正在生成完成回执','INTERRUPTED':'拷贝已中断','FAILED':'拷贝失败','QUEUED':'拷贝排队中'}
                reason='缺少精准内容校验完成证据；不能按文件大小校验结果自动入队' if state=='verification_required' else labels.get(row['state'],'等待拷贝程序确认完成')
                self.db.execute("UPDATE jobs SET state=?,error=?,next_try=0 WHERE id=?",(state,reason,jid))
                continue
            if job['state'] == 'processing':
                # A worker owns this claim. Keep it processing while the
                # evidence token is unchanged; a changed token makes the
                # service discard the stale worker result before publishing.
                try:
                    deps = json.loads(job['dependencies']) or [row['batch_id']]
                    token = signal(self.staging,deps,snapshot,self.index,allow_examples=self.examples)
                    if token != job['token']:
                        self.db.execute(
                            "UPDATE jobs SET state='queued',token=?,error=NULL,queued_at=?,ready_at=NULL WHERE id=?",
                            (token,now,jid),
                        )
                except Exception as exc:
                    self.fail(job,exc,now)
                continue
            if job['state']=='retry_wait' and now<job['next_try']:
                continue
            try:
                deps = json.loads(job['dependencies']) or [row['batch_id']]
                token = signal(self.staging,deps,snapshot,self.index,allow_examples=self.examples)
                valid = job['state']=='awaiting_confirmation' and token==job['token']
                if valid:
                    try:
                        valid = self.artifact_valid(job)
                    except FileNotFoundError:
                        valid = False
                if not valid:
                    self.db.execute("UPDATE jobs SET state='queued',token=?,error=NULL,queued_at=?,ready_at=NULL WHERE id=?",(token,now,jid))
            except Exception as exc:
                self.fail(job,exc,now)
        for job in self.jobs():
            if job['id'] not in current:
                self.db.execute("UPDATE jobs SET state='superseded',error=? WHERE id=?",('当前完整列表已无此版本',job['id']))
        self.db.commit()
        self.checkpoint('discovered')
        return snapshot, self.status()

    def claim_jobs(self, max_jobs=1):
        """Claim queued work in the parent process; workers never open this DB."""
        claimed = []
        for job in [j for j in self.jobs() if j['state']=='queued'][:max_jobs]:
            self.db.execute("UPDATE jobs SET state='processing' WHERE id=?",(job['id'],))
            self.db.commit()
            self.checkpoint('claimed')
            claimed.append(dict(self.db.execute('SELECT * FROM jobs WHERE id=?',(job['id'],)).fetchone()))
        return claimed

    def build_claimed(self, job, snapshot):
        """Build a result from read-only inputs; intended for a worker process."""
        return build_job(self.staging,job['batch_id'],snapshot,self.index,allow_examples=self.examples)

    def accept_result(self, job, result, provider):
        """Revalidate a worker result before publishing and committing its state."""
        current = self.db.execute('SELECT state FROM jobs WHERE id=?',(job['id'],)).fetchone()
        if not current or current['state'] != 'processing':
            return False
        try:
            model,deps,before = result
            fresh = validate_snapshot(provider())
            after = signal(self.staging,deps,fresh,self.index,allow_examples=self.examples)
            if before != after:
                raise EvidenceError('分类期间完成证据变化，稍后重新生成')
            for dep in deps:
                if not any(s['batch_id']==dep for s in fresh['batches']):
                    raise EvidenceError('依赖批次已消失')
            relative = 'reports/'+job['id']+'/'+model['report_id']
            publish(self.root,relative,{'确认模型.json':canonical(model),'拍摄段确认.html':render_review(
                model, project_root_label=os.environ.get('MULI_SORTER_PROJECTS_LABEL', str(self.projects))).encode()})
            self.checkpoint('artifact_written')
            self.db.execute("""UPDATE jobs SET state='awaiting_confirmation',token=?,dependencies=?,
                artifact=?,model_hash=?,attempts=0,next_try=0,error=NULL,ready_at=? WHERE id=?""",
                (after,json.dumps(deps),relative,digest(model),time.time(),job['id']))
        except Exception as exc:
            self.fail(job,exc,time.time())
        self.db.commit()
        return True

    def fail_claimed(self, job, error):
        current = self.db.execute('SELECT state FROM jobs WHERE id=?',(job['id'],)).fetchone()
        if current and current['state'] == 'processing':
            self.fail(job,error,time.time())
            self.db.commit()
            return True
        return False

    def tick(self, provider, *, max_jobs=1):
        snapshot, _ = self.discover(provider)
        if snapshot is None:
            return self.status()
        for job in self.claim_jobs(max_jobs):
            try:
                result = self.build_claimed(job,snapshot)
            except Exception as exc:
                self.fail_claimed(job,exc)
                continue
            self.accept_result(job,result,provider)
        return self.status()

    def status(self):
        return {'mode':'readonly_discovery_queue','rules':RULES,'media_write_authorized':False,
                'example_data':self.examples,'generated_at':datetime.now(timezone.utc).isoformat(),
                'source':self.get_meta('source'),'jobs':self.jobs()}
