"""Bounded generated-page retention. No recursive deletion or media access."""
from dataclasses import asdict,dataclass
from contextlib import contextmanager
import hashlib,json,math,os,re,stat,time
from pathlib import Path
from uuid import uuid4
from . import __version__
from .order_feed_io import directory
from .order_feed_io import atomic_json,read_json
from .snapshot_references import References,RetentionBlocked,REPORT,token,persistent_identity

NAMES={'确认模型.json','拍摄段确认.html'}

@dataclass(frozen=True)
class Policy:
    enabled:bool=False
    minimum_age_days:int=7
    keep_latest:int=20
    maximum_generations:int=20
    maximum_bytes:int=1_000_000_000
    pause_seconds:float=5.0
    successful_log_days:int=90

    @classmethod
    def parse(cls,value):
        if not isinstance(value,dict) or set(value)!=set(asdict(cls())):raise ValueError('unknown/incomplete retention policy')
        p=cls(**value)
        if type(p.enabled) is not bool:raise ValueError('enabled must be boolean')
        bounds={'minimum_age_days':(7,3650),'keep_latest':(20,10000),'maximum_generations':(1,20),'maximum_bytes':(1,1_000_000_000),'successful_log_days':(90,3650)}
        for k,(lo,hi) in bounds.items():
            x=getattr(p,k)
            if type(x) is not int or not lo<=x<=hi:raise ValueError('invalid policy '+k)
        if type(p.pause_seconds) not in (float,int) or not 0<p.pause_seconds<=5:raise ValueError('invalid pause budget')
        return p

    def digest(self):return hashlib.sha256(json.dumps(asdict(self),sort_keys=True,separators=(',',':')).encode()).hexdigest()


def file_token(s):return [s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns,s.st_ctime_ns]

def pointers(queue):
    value=read_json(Path(queue)/'队列状态.json');ids=set()
    for key in ('base_model_path','confirmation_page'):
        path=value.get(key)
        if not isinstance(path,str) or not re.fullmatch(r'combined/sha256:[a-f0-9]{64}/(确认模型\.json|拍摄段确认\.html)',path):raise RetentionBlocked('unknown current page pointer')
        ids.add(path.split('/')[1])
    return ids


def inventory(queue,*,clock=None):
    clock=time.time() if clock is None else clock;rows=[]
    with directory(Path(queue)/'combined') as root:
        names=os.listdir(root)
        if len(names)>10000:raise RetentionBlocked('snapshot metadata budget exceeded')
        for name in names:
            if not REPORT.fullmatch(name):raise RetentionBlocked('unknown combined directory entry')
            fd=os.open(name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=root)
            try:
                if set(os.listdir(fd))!=NAMES:raise RetentionBlocked('snapshot has unexpected/missing entries')
                files={}
                for filename in sorted(NAMES):
                    s=os.stat(filename,dir_fd=fd,follow_symlinks=False)
                    if not stat.S_ISREG(s.st_mode) or s.st_nlink!=1:raise RetentionBlocked('unsafe generated file')
                    files[filename]=file_token(s)
                rows.append({'report_id':name,'directory_identity':persistent_identity(fd),'files':files,
                             'bytes':sum(x[2] for x in files.values()),'newest_mtime_ns':max(x[3] for x in files.values())})
            finally:os.close(fd)
        return rows,persistent_identity(root)


def select_plan(policy,pins,rows,root,current,clock):
    if not current.issubset({r['report_id'] for r in rows}):raise RetentionBlocked('current generation missing')
    protected=current|set(pins)|{r['report_id'] for r in sorted(rows,key=lambda r:(r['newest_mtime_ns'],r['report_id']),reverse=True)[:policy.keep_latest]}
    selected=[];size=0;eligible=0
    for row in sorted(rows,key=lambda r:(r['newest_mtime_ns'],r['report_id'])):
        if row['report_id'] in protected or clock-row['newest_mtime_ns']/1e9<policy.minimum_age_days*86400:continue
        eligible+=1
        if len(selected)>=policy.maximum_generations or size+row['bytes']>policy.maximum_bytes:continue
        selected.append(row);size+=row['bytes']
    result={'schema':'snapshot-cleanup-plan/1','version':__version__,'policy_sha256':policy.digest(),'combined_identity':root,
            'current':sorted(current),'protected':sorted(protected),'generation_count':len(rows),'eligible_count':eligible,'selected':selected,'bytes':size}
    result['plan_sha256']=hashlib.sha256(json.dumps(result,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    return result


def plan(queue,policy,pins,*,clock=None):
    clock=time.time() if clock is None else clock;current=pointers(queue);rows,root=inventory(queue,clock=clock)
    result=select_plan(policy,pins,rows,root,current,clock)
    if pointers(queue)!=current:raise RetentionBlocked('page pointers changed during preview')
    return result


def delete_pair(queue,row,guard,event):
    """One approved generation. Guard and journal before unlink; no retry."""
    name=row['report_id']
    if not REPORT.fullmatch(name):raise RetentionBlocked('invalid generation name')
    if not isinstance(row.get('files'),dict) or set(row['files'])!=NAMES:raise RetentionBlocked('invalid planned filenames')
    with directory(Path(queue)/'combined') as root:
        fd=os.open(name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=root)
        try:
            if persistent_identity(fd)!=row['directory_identity'] or set(os.listdir(fd))!=NAMES:raise RetentionBlocked('generation changed')
            for f,expected in row['files'].items():
                s=os.stat(f,dir_fd=fd,follow_symlinks=False)
                if not stat.S_ISREG(s.st_mode) or s.st_nlink!=1 or file_token(s)!=expected:raise RetentionBlocked('generated file changed')
            guard();event({'type':'delete_intent','report_id':name,'files':row['files']})
            for f,expected in row['files'].items():
                guard();named=os.stat(name,dir_fd=root,follow_symlinks=False);held=os.fstat(fd)
                if (named.st_dev,named.st_ino)!=(held.st_dev,held.st_ino):raise RetentionBlocked('generation path changed')
                s=os.stat(f,dir_fd=fd,follow_symlinks=False)
                if file_token(s)!=expected or s.st_nlink!=1:raise RetentionBlocked('file changed after intent')
                os.unlink(f,dir_fd=fd)
            os.fsync(fd)
            actual=os.stat(name,dir_fd=root,follow_symlinks=False)
            if (actual.st_dev,actual.st_ino)!=(os.fstat(fd).st_dev,os.fstat(fd).st_ino) or os.listdir(fd):raise RetentionBlocked('generation entry replaced')
            os.rmdir(name,dir_fd=root);os.fsync(root)
            event({'type':'delete_result','report_id':name,'bytes':row['bytes']})
        finally:os.close(fd)


class Cleanup:
    """Runtime adapter must verify live owner/version/idle and bind the freezer."""
    def __init__(self,refs,policy,runtime):self.refs=refs;self.policy=policy;self.runtime=runtime

    def _pending(self):return self.refs.root/'inflight.json'

    def status(self):
        pending=self._pending()
        return {'enabled':self.policy.enabled,'policy_sha256':self.policy.digest(),'inflight':read_json(pending) if pending.exists() else None,
                'last_run':read_json(self.refs.root/'last-run.json') if (self.refs.root/'last-run.json').exists() else None}

    def preview(self):
        if self._pending().exists():raise RetentionBlocked('unresolved cleanup run; inspect first')
        with self.refs.gate(wait=False):
            # SH permits business readers/writers; freshness is rechecked.
            self.refs.assert_fresh()
            result=plan(self.refs.queue,self.policy,self.refs.pins())
            self.refs.assert_fresh()
            return result

    def shadow(self):
        """Only derived index changes; never freezes or unlinks page files."""
        if self._pending().exists():raise RetentionBlocked('unresolved cleanup run; inspect first')
        started=time.monotonic()
        baseline=self.runtime.check(self.refs,self.policy)
        if baseline.get('busy'):return {'status':'busy','reason':baseline['busy']}
        rows,root=inventory(self.refs.queue)
        with self.refs.gate(exclusive=True,wait=False):
            index=self.refs.refresh()
            if not index['complete']:return {'status':'indexing','index':index}
            activity=self.refs.activity()
            if activity:return {'status':'busy','reason':activity}
            current=pointers(self.refs.queue)
            if not current.issubset({r['report_id'] for r in rows}):return {'status':'busy','reason':'page_publication_changed'}
            result=select_plan(self.policy,self.refs.pins(),rows,root,current,time.time())
            return {'status':'shadow','plan':result,'index':index,'page_files_mutated':False,'classifier_paused':False,'elapsed_seconds':time.monotonic()-started}

    def run(self,*,execute=False,confirmed_policy=None,expected_plan=None):
        if not execute:return {'status':'dry_run','plan':self.preview()}
        if not self.policy.enabled:return {'status':'disabled'}
        if confirmed_policy!=self.policy.digest():raise ValueError('explicit current policy digest required')
        if self._pending().exists():raise RetentionBlocked('unresolved cleanup run; inspect first')
        baseline=self.runtime.check(self.refs,self.policy)
        if baseline.get('busy'):return {'status':'busy','reason':baseline['busy']}
        rows,combined_identity=inventory(self.refs.queue)
        try:
            with self.refs.gate(exclusive=True,wait=False):
                index=self.refs.refresh()
                if not index['complete']:return {'status':'indexing','index':index}
                activity=self.refs.activity()
                if activity:return {'status':'busy','reason':activity}
                pins=self.refs.pins();current=pointers(self.refs.queue)
                if not current.issubset({r['report_id'] for r in rows}):return {'status':'busy','reason':'page_publication_changed'}
                p=select_plan(self.policy,pins,rows,combined_identity,current,time.time())
                if expected_plan and expected_plan!=p['plan_sha256']:raise RetentionBlocked('preview plan changed')
                if not p['selected']:return {'status':'nothing_to_clean','eligible_count':p['eligible_count']}
                recheck=getattr(self.runtime,'recheck',lambda refs,policy,prior:self.runtime.check(refs,policy))
                baseline=recheck(self.refs,self.policy,baseline)
                if baseline.get('busy'):return {'status':'busy','reason':baseline['busy']}
                catalog=self.refs.catalog_token()
                run_id=uuid4().hex;run=self.refs.root/'runs'/run_id;run.mkdir(mode=0o700)
                state={'schema':'snapshot-cleanup-run/1','run_id':run_id,'version':__version__,'phase':'prepared','baseline':baseline,'plan':p,'policy_sha256':self.policy.digest()}
                atomic_json(self._pending(),state);atomic_json(run/'intent.json',state)
                def event(row):
                    with (run/'events.jsonl').open('a') as f:f.write(json.dumps({'at':time.time(),**row})+'\n');f.flush();os.fsync(f.fileno())
                deleted=[];error=None;started=time.monotonic()
                try:
                    state['phase']='pause_intent';atomic_json(self._pending(),state)
                    self.runtime.pause(baseline)
                    state['phase']='paused';atomic_json(self._pending(),state)
                    self.runtime.check_paused(baseline)
                    if pointers(self.refs.queue)!=set(p['current']):raise RetentionBlocked('published page changed')
                    with directory(self.refs.queue/'combined') as fd:
                        if persistent_identity(fd)!=p['combined_identity']:raise RetentionBlocked('combined root replaced')
                    def guard():
                        if self.refs.catalog_token()!=catalog:raise RetentionBlocked('control catalog changed')
                        if pointers(self.refs.queue)!=set(p['current']):raise RetentionBlocked('current page changed')
                    for row in p['selected']:
                        if time.monotonic()-started>=self.policy.pause_seconds:break
                        delete_pair(self.refs.queue,row,guard,event);deleted.append(row['report_id'])
                except BaseException as exc:
                    error={'type':type(exc).__name__,'message':str(exc)[:200]};event({'type':'run_error','error':error})
                finally:
                    # An ambiguous pause result is reconciled by adapter ID/state,
                    # never by repeating pause or addressing another container.
                    self.runtime.resume(baseline)
                    resumed=time.monotonic()
                self.runtime.verify(baseline)
                if error:raise RetentionBlocked('cleanup stopped; inspect run '+run_id+': '+error['message'])
                result={'status':'completed','run_id':run_id,'at':time.time(),'deleted_generations':len(deleted),'deleted_files':len(deleted)*2,
                        'bytes':sum(r['bytes'] for r in p['selected'] if r['report_id'] in deleted),'pause_seconds':resumed-started,'remaining_eligible':p['eligible_count']-len(deleted)}
                atomic_json(run/'result.json',result);atomic_json(self.refs.root/'last-run.json',result)
                self._pending().unlink()
                with directory(self.refs.root) as fd:os.fsync(fd)
                self._rotate_success_logs()
                return result
        except BlockingIOError:return {'status':'busy','reason':'reference_gate_in_use'}

    def recover_freezer(self,*,confirmed_policy):
        if confirmed_policy!=self.policy.digest():raise ValueError('explicit current policy digest required')
        with self.refs.gate(exclusive=True,wait=False):
            if not self._pending().exists():return {'status':'no_pending_run'}
            state=read_json(self._pending())
            if state.get('schema')!='snapshot-cleanup-run/1' or state.get('policy_sha256')!=self.policy.digest():raise RetentionBlocked('unknown pending run')
            if state['phase'] in ('pause_intent','paused'):self.runtime.resume(state['baseline'])
            return {'status':'freezer_reconciled_deletion_not_replayed','run_id':state['run_id'],'requires_review':True}

    def _rotate_success_logs(self):
        # Own successful runs only. Pending, failed and original audits survive.
        for run in (self.refs.root/'runs').iterdir():
            if run.is_symlink() or not re.fullmatch(r'[a-f0-9]{32}',run.name):continue
            result=run/'result.json'
            if not result.is_file() or result.is_symlink():continue
            value=read_json(result)
            stamp=value.get('at')
            if (value.get('status')!='completed' or value.get('run_id')!=run.name or type(stamp) not in (int,float) or
                    not math.isfinite(stamp) or stamp>time.time() or time.time()-stamp<self.policy.successful_log_days*86400):continue
            with directory(run) as fd:
                names=set(os.listdir(fd))
                if not names.issubset({'intent.json','events.jsonl','result.json'}):continue
                if any(not stat.S_ISREG(os.stat(n,dir_fd=fd,follow_symlinks=False).st_mode) or os.stat(n,dir_fd=fd,follow_symlinks=False).st_nlink!=1 for n in names):continue
                for name in names:os.unlink(name,dir_fd=fd)
                os.fsync(fd)
            run.rmdir()
