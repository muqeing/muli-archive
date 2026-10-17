"""Derived page references. Original controls remain authoritative.

Enabled explicitly by the archive owner. Writers share the maintenance gate;
failed writes leave durable fences. Maintenance refreshes changed controls only.
No media or per-file receipt traversal belongs here.
"""
from contextlib import contextmanager
from collections import OrderedDict
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
import time
from uuid import uuid4

from .order_feed_io import directory
from .handoff_request_binding import _Reader, _canonical_digest
from .order_feed_io import atomic_json, read_json

def persistent_identity(fd):
    fsid=getattr(os.fstatvfs(fd),'f_fsid',None)
    if type(fsid) is not int or fsid==0:raise RetentionBlocked('filesystem has no stable identity')
    return [fsid & ((1<<64)-1),os.fstat(fd).st_ino]


REPORT = re.compile(r'sha256:[a-f0-9]{64}\Z')
TOKEN = re.compile(r'[a-f0-9]{64}\Z')
MAX_CONTROL_BYTES = 128 * 1024 * 1024


class RetentionBlocked(ValueError):
    pass


def token(path):
    path=Path(path)
    with directory(path.parent) as parent:
        fd=os.open(path.name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=parent)
        try:
            s=os.fstat(fd)
            if not stat.S_ISREG(s.st_mode) or s.st_nlink!=1:raise RetentionBlocked('unsafe control record')
            return [*persistent_identity(fd),s.st_size,s.st_mtime_ns,s.st_ctime_ns]
        finally:os.close(fd)


def _scalar(reader):
    raw=bytearray();quoted=False;escape=False
    while reader.available():
        char=reader.buffer[reader.pos]
        if not quoted and char in (44,125):break
        char=reader.take();raw.append(char)
        if len(raw)>8192:raise RetentionBlocked('control scalar too large')
        if escape:escape=False
        elif char==92 and quoted:escape=True
        elif char==34:quoted=not quoted
    return json.loads(raw)


def scan(path, scalars):
    """Stream controls; only exact canonical admitted request SHA can release pins."""
    before=token(path)
    if before[2]>MAX_CONTROL_BYTES:raise RetentionBlocked('control exceeds limit')
    with directory(Path(path).parent) as parent:
        fd=os.open(Path(path).name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=parent)
        try:
            s=os.fstat(fd)
            if [*persistent_identity(fd),s.st_size,s.st_mtime_ns,s.st_ctime_ns]!=before:raise RetentionBlocked('control replaced')
            r=_Reader(fd)
            def whitespace():
                while r.available() and r.buffer[r.pos] in b' \t\r\n':r.take()
            whitespace();r.expect(123);values={};hashes={};keys=set()
            while True:
                whitespace();key=r.key()
                if key in keys or len(keys)>=128:raise RetentionBlocked('duplicate/too many control fields')
                keys.add(key);whitespace();r.expect(58);whitespace()
                if key in scalars:values[key]=_scalar(r)
                else:hashes[key]=r.value_digest()
                separator=r.take()
                if separator==125:break
                if separator!=44:raise RetentionBlocked('noncanonical control')
            whitespace()
            if r.available() or token(path)!=before:raise RetentionBlocked('control changed')
            return {'values':values,'hashes':hashes,'keys':sorted(keys),'sha256':r.hash.hexdigest()}
        finally:os.close(fd)


class References:
    def __init__(self,state,queue,*,create=False):
        self.state=Path(state).absolute();self.queue=Path(queue).absolute();self.root=self.state/'snapshot-retention'
        if self.state==self.queue or self.state.is_relative_to(self.queue) or self.queue.is_relative_to(self.state):
            raise RetentionBlocked('queue and archive state must be separate')
        with directory(self.state) as a,directory(self.queue) as q:
            self.binding={'version':1,'archive':persistent_identity(a),'queue':persistent_identity(q)}
        if create:
            with directory(self.state) as parent:
                try:os.mkdir('snapshot-retention',0o700,dir_fd=parent);os.fsync(parent)
                except FileExistsError:pass
            with directory(self.root) as parent:
                for name in ('pending','runs'):
                    try:os.mkdir(name,0o700,dir_fd=parent)
                    except FileExistsError:pass
                fd=os.open('gate.lock',os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600,dir_fd=parent);os.close(fd)
            binding_file=self.root/'binding.json'
            if binding_file.exists():
                if read_json(binding_file)!=self.binding:raise RetentionBlocked('reference roots changed')
            else:atomic_json(binding_file,self.binding)
            try:
                with self.db(write=True) as db:
                    db.executescript('CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL); CREATE TABLE IF NOT EXISTS controls(path TEXT PRIMARY KEY, token TEXT NOT NULL, summary TEXT NOT NULL);')
                    prior=db.execute("SELECT value FROM meta WHERE key='binding'").fetchone()
                    if prior and json.loads(prior[0])!=self.binding:raise RetentionBlocked('reference index belongs to other roots')
                    db.execute('INSERT OR IGNORE INTO meta VALUES(?,?)',('binding',json.dumps(self.binding)))
            except (sqlite3.Error,RetentionBlocked):
                # A broken derived cache must disable cleanup, not archive work.
                atomic_json(self.root/'index-blocked.json',{'reason':'reference index requires inspection','at':time.time()})
        self.memo=OrderedDict()

    def activate(self):
        from . import __version__
        atomic_json(self.root/'owner.json',{'protocol':'snapshot-reference-gate/1','active':True,'version':__version__,'binding':self.binding})

    def close(self):
        from . import __version__
        atomic_json(self.root/'owner.json',{'protocol':'snapshot-reference-gate/1','active':False,'version':__version__,'binding':self.binding})
        self.memo.clear()

    @contextmanager
    def db(self,*,write=False):
        with directory(self.root):pass
        path=self.root/'references.sqlite3'
        for extra in ('','-journal','-wal','-shm'):
            candidate=Path(str(path)+extra)
            if candidate.is_symlink():raise RetentionBlocked('unsafe reference database')
            if candidate.exists():token(candidate)
        if path.exists():token(path)
        elif not write:raise RetentionBlocked('reference index not initialized')
        db=sqlite3.connect(str(path) if write else path.as_uri()+'?mode=ro',uri=not write,timeout=1)
        try:
            if write:db.execute('PRAGMA synchronous=FULL')
            else:db.execute('PRAGMA query_only=ON')
            yield db
            if write:db.commit()
        finally:db.close()

    @contextmanager
    def gate(self,*,exclusive=False,wait=True):
        with directory(self.root) as parent:
            fd=os.open('gate.lock',os.O_RDWR|os.O_NOFOLLOW,dir_fd=parent)
            try:
                info=os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1:raise RetentionBlocked('unsafe maintenance gate')
                fcntl.flock(fd,(fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)|(0 if wait else fcntl.LOCK_NB))
                if read_json(self.root/'binding.json')!=self.binding:raise RetentionBlocked('reference root binding changed')
                if exclusive:
                    if (self.root/'index-blocked.json').exists():raise RetentionBlocked('reference index requires inspection')
                    with self.db() as db:
                        row=db.execute("SELECT value FROM meta WHERE key='binding'").fetchone()
                        if not row or json.loads(row[0])!=self.binding:raise RetentionBlocked('reference index binding changed')
                yield
            finally:os.close(fd)

    def write(self,path,value,writer):
        path=Path(path);relative=path.relative_to(self.state).as_posix()
        # Progress-only job writes retain the same reference meaning. Still hold
        # the gate, but avoid another durable fence per media-file progress tick.
        semantic=(value.get('status'),value.get('report_id'),value.get('request_digest')) if relative.startswith('requests/job-') else None
        with self.gate():
            prior=self.memo.get(relative)
            if semantic is not None and prior==semantic:
                writer(path,value);return
            marker=self.root/'pending'/(uuid4().hex+'.json')
            atomic_json(marker,{'path':relative,'at':time.time()})
            writer(path,value)
            marker.unlink()
            with directory(marker.parent) as parent:os.fsync(parent)
            if semantic is not None:
                self.memo.pop(relative,None);self.memo[relative]=semantic
                while len(self.memo)>256:self.memo.popitem(last=False)

    def _paths(self):
        paths=[]
        for folder,pattern in [('requests','request-*.json'),('requests','job-*.json'),('submissions','*.json'),('confirmation-tickets','*.json')]:
            parent=self.state/folder
            if not parent.exists():continue
            with directory(parent):pass
            paths.extend(parent.glob(pattern))
        if len(paths)>100000:raise RetentionBlocked('control metadata budget exceeded')
        return sorted(paths)

    def _summary(self,path):
        relative=path.relative_to(self.state).as_posix();name=path.name
        if path.parent.name=='requests':
            kind='request' if name.startswith('request-') else 'job';key=name[len(kind)+1:-5]
            if not TOKEN.fullmatch(key):raise RetentionBlocked('unknown control name')
            data=scan(path,{'job_id','status','report_id','request_digest','admission_policy'})
            if data['values'].get('job_id')!=key:raise RetentionBlocked('control ID mismatch')
            return {'kind':kind,'id':key,**data}
        if path.parent.name=='submissions':
            row=read_json(path)
            if not TOKEN.fullmatch(name[:-5]) or row.get('submission_id')!=name[:-5]:raise RetentionBlocked('submission binding mismatch')
            return {'kind':'submission','status':row.get('status'),'report_id':row.get('report_id'),'expected_job_id':row.get('expected_job_id')}
        if path.stat().st_size>48*1024*1024:raise RetentionBlocked('ticket exceeds limit')
        row=read_json(path)
        if (set(row)!={'schema','token','identity','payload','digest'} or row['schema']!='archive-preview-ticket/1' or
                not TOKEN.fullmatch(name[:-5]) or row['token']!=name[:-5] or
                row['identity']!=read_json(self.state/'service-identity.json') or
                row['digest']!=_canonical_digest({k:row[k] for k in ('schema','token','identity','payload')}) or
                not isinstance(row['payload'],dict)):
            raise RetentionBlocked('ticket binding mismatch')
        payload=row['payload']
        report=payload.get('origin_report_id') or payload.get('model',{}).get('report_id')
        expires=payload.get('expires_at')
        if type(expires) not in (int,float) or not math.isfinite(expires):raise RetentionBlocked('unknown ticket expiry')
        return {'kind':'ticket','report_id':report,'expires_at':expires}

    def refresh(self,*,maximum_records=20,maximum_bytes=64*1024*1024,seconds=2):
        """Caller holds EX. First initialization is an explicit bootstrap."""
        if (self.root/'index-blocked.json').exists():raise RetentionBlocked('reference index requires inspection')
        with directory(self.root/'pending'):pass
        if any((self.root/'pending').iterdir()):raise RetentionBlocked('unresolved reference write; inspect before cleanup')
        before_catalog=self.catalog_token()
        paths=self._paths();seen=set();read_records=0;read_bytes=0;started=time.monotonic();complete=True
        with self.db(write=True) as db:
            for path in paths:
                relative=path.relative_to(self.state).as_posix();seen.add(relative);before=token(path)
                row=db.execute('SELECT token FROM controls WHERE path=?',(relative,)).fetchone()
                if row and json.loads(row[0])==before:continue
                if (read_records>=maximum_records or read_bytes+before[2]>maximum_bytes or
                        (seconds is not None and time.monotonic()-started>=seconds)):
                    complete=False;break
                summary=self._summary(path)
                if token(path)!=before:raise RetentionBlocked('control changed during index update')
                db.execute('INSERT OR REPLACE INTO controls VALUES(?,?,?)',(relative,json.dumps(before),json.dumps(summary)));read_records+=1;read_bytes+=before[2]
            if complete:
                for (relative,) in db.execute('SELECT path FROM controls').fetchall():
                    if relative not in seen:db.execute('DELETE FROM controls WHERE path=?',(relative,))
            if self.catalog_token()!=before_catalog:raise RetentionBlocked('control catalog changed during index update')
            db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',('ready',json.dumps({'at':time.time(),'records':len(paths),'complete':complete})))
        return {'records':len(paths),'read_records':read_records,'read_bytes':read_bytes,'complete':complete,'elapsed_seconds':time.monotonic()-started}

    def catalog_token(self):
        values={}
        for name in ('requests','submissions','confirmation-tickets'):
            p=self.state/name
            if p.exists():
                with directory(p) as fd:
                    st=os.fstat(fd);values[name]=[*persistent_identity(fd),st.st_mtime_ns,st.st_ctime_ns]
            else:values[name]=None
        return values

    def assert_fresh(self):
        before=self.catalog_token();paths=self._paths();seen=set()
        with self.db() as db:
            ready=db.execute("SELECT value FROM meta WHERE key='ready'").fetchone()
            if not ready or not json.loads(ready[0]).get('complete'):raise RetentionBlocked('reference index not ready')
            for path in paths:
                name=path.relative_to(self.state).as_posix();seen.add(name)
                row=db.execute('SELECT token FROM controls WHERE path=?',(name,)).fetchone()
                if not row or json.loads(row[0])!=token(path):raise RetentionBlocked('reference index stale')
            if db.execute('SELECT count(*) FROM controls').fetchone()[0]!=len(seen):raise RetentionBlocked('reference index stale')
        if before!=self.catalog_token():raise RetentionBlocked('control catalog changed')
        with directory(self.root/'pending'):pass
        if any((self.root/'pending').iterdir()):raise RetentionBlocked('unresolved reference write')

    def activity(self):
        with self.db() as db:
            for (raw,) in db.execute('SELECT summary FROM controls'):
                r=json.loads(raw)
                if r['kind']=='job' and r['values'].get('status') in ('queued','running'):return 'archive_job'
                if r['kind']=='submission' and r.get('status') not in ('accepted','rejected'):return 'submission'
        return None

    def pins(self,clock=None):
        clock=time.time() if clock is None else clock;pins=set()
        identity_hash=_canonical_digest(read_json(self.state/'service-identity.json'))
        with self.db() as db:
            ready=db.execute("SELECT value FROM meta WHERE key='ready'").fetchone()
            if not ready or not json.loads(ready[0]).get('complete'):raise RetentionBlocked('reference index not ready')
            for (raw,) in db.execute('SELECT summary FROM controls'):
                row=json.loads(raw);report=row.get('report_id') or row.get('values',{}).get('report_id');protect=False
                if row['kind']=='job':
                    v=row['values'];request_row=db.execute('SELECT summary FROM controls WHERE path=?',('requests/request-'+row['id']+'.json',)).fetchone()
                    request=json.loads(request_row[0]) if request_row else None;rv=(request or {}).get('values',{});rh=(request or {}).get('hashes',{})
                    independent=(v.get('status')=='completed' and row['hashes'].get('errors')==_canonical_digest([]) and
                        request is not None and request['sha256']==v.get('request_digest') and
                        rv.get('admission_policy')=='confirmed-plan/v1' and rh.get('roots')==identity_hash and
                        row['hashes'].get('file_plans')==rh.get('file_plans') and
                        {'model','decisions','file_plans','roots','job_id'}.issubset(request['keys']) and
                        set(request['keys']).issubset({'model','decisions','file_plans','roots','job_id','admission_policy','scope_binding','execution_strategy','source_evidence','target_parent_identities','verification_policy','validated_signatures'}))
                    protect=not independent
                elif row['kind']=='submission':protect=row['status'] not in ('accepted','rejected')
                elif row['kind']=='ticket':protect=clock<row['expires_at']+120
                elif row['kind']=='request':
                    if not db.execute('SELECT 1 FROM controls WHERE path=?',('requests/job-'+row['id']+'.json',)).fetchone():
                        parents=db.execute("SELECT summary FROM controls WHERE path LIKE 'submissions/%'")
                        parent=next((json.loads(r[0]) for r in parents if json.loads(r[0]).get('expected_job_id')==row['id']),None)
                        report=(parent or {}).get('report_id');protect=True
                if protect:
                    if not isinstance(report,str) or not REPORT.fullmatch(report):raise RetentionBlocked('unknown active reference')
                    pins.add(report)
        return pins
