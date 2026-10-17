"""Host-only Docker freezer adapter. No socket mounts or media execution."""
import json,os,re,sqlite3,subprocess
from pathlib import Path
from . import __version__
from .snapshot_references import RetentionBlocked,token
from .order_feed_io import read_json,directory

class DockerRuntime:
    def __init__(self,config):
        self.config=config;self.classifier=config['classifier_container'];self.archive=config['archive_container']
        self.names=[self.classifier,self.archive,*config['protected_containers']]
        if any(not isinstance(n,str) or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}',n) for n in self.names):raise ValueError('invalid bound container name')
        if not re.fullmatch(r'sha256:[a-f0-9]{64}',config['archive_image_id']):raise ValueError('exact archive image digest required')
        if len(self.names)!=6 or len(set(self.names))!=6:raise ValueError('bind all six distinct service containers')

    def command(self,args):
        return subprocess.check_output(['docker',*args],text=True,timeout=3)

    def snapshots(self,names):
        fields={'id':'.Id','image_id':'.Image','running':'.State.Running','paused':'.State.Paused','oom':'.State.OOMKilled','restarts':'.RestartCount','health':'(index .State "Health")','command':'.Config.Cmd','mounts':'.Mounts','memory':'.HostConfig.Memory','cpu':'.HostConfig.NanoCpus','pids':'.HostConfig.PidsLimit','user':'.Config.User','entrypoint':'.Config.Entrypoint','readonly':'.HostConfig.ReadonlyRootfs','cap_drop':'.HostConfig.CapDrop','security':'.HostConfig.SecurityOpt','restart_policy':'.HostConfig.RestartPolicy','ports':'.HostConfig.PortBindings','networks':'.NetworkSettings.Networks'}
        template='{'+','.join(json.dumps(k)+':{{json '+v+'}}' for k,v in fields.items())+'}'
        lines=self.command(['inspect','--format',template,*names]).splitlines()
        if len(lines)!=len(names):raise RetentionBlocked('incomplete container inventory')
        result={}
        for name,line in zip(names,lines):
            v=json.loads(line);v['health']=(v['health'] or {}).get('Status');v['networks']=sorted(v['networks']);result[name]=v
        return result

    def snap(self,name):return self.snapshots([name])[name]

    def check_storage(self,refs):
        cfg=self.config['storage']
        if set(cfg)!={'mounts','arrays','disks'} or not cfg['mounts'] or not cfg['arrays'] or not cfg['disks']:raise RetentionBlocked('storage checks not configured')
        found=[]
        for mount in cfg['mounts']:
            if not isinstance(mount,str) or not Path(mount).is_absolute():raise RetentionBlocked('invalid storage mount')
            data=json.loads(subprocess.check_output(['findmnt','-J','-T',mount,'-o','TARGET,OPTIONS'],text=True,timeout=5))['filesystems'][0]
            if data['target']!=mount or 'rw' not in data['options'].split(','):raise RetentionBlocked('storage not writable')
            found.append(Path(mount))
        if not all(any(path.is_relative_to(m) for m in found) for path in (refs.queue,refs.state)):raise RetentionBlocked('maintenance paths outside checked storage')
        lines=Path('/proc/mdstat').read_text().splitlines()
        for name in cfg['arrays']:
            if not re.fullmatch(r'md[0-9]+',name):raise RetentionBlocked('invalid array name')
            line=next((s for s in lines if s.startswith(name+' :')), '')
            if 'active' not in line or '(F)' in line:raise RetentionBlocked('array not healthy')
            degraded=Path('/sys/block')/name/'md/degraded'
            if degraded.exists() and degraded.read_text().strip()!='0':raise RetentionBlocked('array degraded')
        for name in cfg['disks']:
            if not re.fullmatch(r'[a-zA-Z0-9_-]+',name):raise RetentionBlocked('invalid disk name')
            if (Path('/sys/block')/name/'device/state').read_text().strip()!='running':raise RetentionBlocked('disk not running')

    def check(self,refs,policy):
        self.check_storage(refs)
        snapshots=self.snapshots(self.names)
        for row in snapshots.values():
            if not row['running'] or row['paused'] or row['oom'] or row['health'] not in (None,'healthy'):raise RetentionBlocked('service not stable')
        a=snapshots[self.archive];c=snapshots[self.classifier]
        if a['image_id']!=self.config['archive_image_id'] or '--enable-snapshot-retention' not in a['command']:raise RetentionBlocked('deployed owner/version not enabled')
        version=json.loads(self.command(['exec',self.archive,'python','-B','-c','import json;from muli_sorter import __version__;print(json.dumps(__version__))']))
        if version!=__version__:raise RetentionBlocked('maintenance/owner version mismatch')
        owner=read_json(refs.root/'owner.json')
        if owner!={'protocol':'snapshot-reference-gate/1','active':True,'version':__version__,'binding':refs.binding}:raise RetentionBlocked('reference writer ownership not active')
        sources=lambda row:{m['Source']:m for m in row['mounts']}
        if not sources(c).get(str(refs.queue),{}).get('RW'):raise RetentionBlocked('classifier queue mount mismatch')
        if sources(a).get(str(refs.queue),{}).get('RW') is not False:raise RetentionBlocked('archive queue mount mismatch')
        busy=self.business_state(refs)
        if busy:return busy
        return {'containers':snapshots,'classifier':self.classifier,'archive':self.archive}

    def business_state(self,refs):
        # Health is not idleness. All copy/scans and classification jobs are read.
        q=read_json(refs.queue/'队列状态.json')
        if q.get('source',{}).get('ok') is not True:raise RetentionBlocked('classification source unknown')
        if any(r.get('state') in ('queued','processing') for r in q.get('jobs',[])):return {'busy':'classification'}
        dbpath=Path(self.config['ingest_database']).absolute();token(dbpath)
        with directory(dbpath.parent):pass
        with sqlite3.connect(dbpath.as_uri()+'?mode=ro',uri=True,timeout=1) as db:
            db.execute('PRAGMA query_only=ON')
            batches=list(db.execute("SELECT json_extract(data,'$.batch_id'),json_extract(data,'$.state'),json_extract(data,'$.result') FROM objects WHERE kind='batch'"))
            if any(state not in ('COMPLETED','FAILED','CANCELLED','INTERRUPTED') for bid,state,result in batches):return {'busy':'copy_or_finalization'}
            if any(state in ('SCANNING','RUNNING','scanning','running') for (state,) in db.execute("SELECT json_extract(data,'$.state') FROM objects WHERE kind='scan'")):return {'busy':'scan'}
        for path in (refs.queue/'postcopy-verifications').glob('*.status.json'):
            if read_json(path).get('state')=='verifying':return {'busy':'independent_verification'}
        for bid,state,result in batches:
            if not isinstance(bid,str) or not re.fullmatch(r'BATCH_[0-9]{8}_[0-9]{6,}',bid):raise RetentionBlocked('unknown copy batch identity')
            if state=='COMPLETED' and result=='COPY_SIZE_VERIFIED' and not (refs.queue/'postcopy-verifications'/(bid+'.json')).exists():
                path=refs.queue/'postcopy-verifications'/(bid+'.status.json')
                status=read_json(path).get('state') if path.exists() else None
                if status not in ('blocked','failed','interrupted','manual_archive_verified'):return {'busy':'independent_verification_pending'}
        return None


    def recheck(self,refs,policy,baseline):
        self.verify(baseline)
        busy=self.business_state(refs)
        return busy or baseline

    def pause(self,baseline):
        row=self.snap(self.classifier)
        if row['id']!=baseline['containers'][self.classifier]['id'] or row['paused']:raise RetentionBlocked('classifier changed before pause')
        self.command(['pause',self.classifier])

    def check_paused(self,baseline):
        row=self.snap(self.classifier)
        if row['id']!=baseline['containers'][self.classifier]['id'] or not row['paused']:raise RetentionBlocked('classifier did not freeze')

    def resume(self,baseline):
        if baseline.get('classifier')!=self.classifier:raise RetentionBlocked('pending run has other classifier')
        row=self.snap(self.classifier)
        if row['id']!=baseline['containers'][self.classifier]['id']:raise RetentionBlocked('do not unfreeze replacement container')
        if row['paused']:self.command(['unpause',self.classifier])
        if self.snap(self.classifier)['paused']:raise RetentionBlocked('classifier remains frozen')

    def verify(self,baseline):
        actual=self.snapshots(list(baseline['containers']))
        for name,old in baseline['containers'].items():
            new=actual[name]
            for key in ('id','image_id','restarts','memory','command','mounts','cpu','pids','user','entrypoint','readonly','cap_drop','security','restart_policy','ports','networks'):
                if new[key]!=old[key]:raise RetentionBlocked('service drift: '+key)
            if not new['running'] or new['paused'] or new['oom'] or new['health'] not in (None,'healthy'):raise RetentionBlocked('service failed after cleanup')
