"""Scoped history routing. The catalog locates receipts, never authorizes media I/O.

Only an explicit offline build may initialize it. Every active-scope or paged
history result is reconstructed using the original receipt validation path.
"""
from collections import OrderedDict
from pathlib import Path
import json
import re
import threading
import zlib

from .archive_history import ArchiveHistory, file_identity, HEX
from .archive_io import ArchiveError
from .external_archive_history import project_external_history
from .history_catalog import HistoryCatalog
from .order_feed_io import digest, read_json


class ScopedArchiveHistory:
    def __init__(self, state, identity, label, production, *, initialize=False):
        self.legacy = ArchiveHistory(state, identity, label, production)
        self.state = Path(state)
        self.catalog = HistoryCatalog(self.state/'history-catalog.sqlite3', {
            'roots': identity, 'label': label, 'production': production})
        self.lock = threading.RLock()
        self.reports = OrderedDict()
        self.report_bytes = 0
        try:
            if not self.catalog.get_meta('ready'):
                if not initialize:
                    raise ArchiveError('历史目录尚未迁移；请先空闲停服并显式构建，不自动全读历史')
                self._sync(force=True)
                self.catalog.set_meta('ready', True)
        except BaseException:
            self.close()
            raise

    def migration_status(self):
        return {'ready':bool(self.catalog.get_meta('ready')),'indexed_units':self.catalog.total,
                'source_jobs':len(self.catalog.get_meta('sources',{})),
                'retained_jobs':len(self.catalog.get_meta('retained',{})),
                'needs_attention_jobs':sorted(self.catalog.get_meta('errors',{})),
                'external_warnings':self.catalog.get_meta('external_warnings',[])}

    def close(self):
        self.catalog.close()

    @staticmethod
    def _token(path):
        try:
            s=path.lstat()
            if path.is_symlink():
                raise ArchiveError('历史控制路径不能是符号链接')
            return [s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns,s.st_ctime_ns]
        except FileNotFoundError:
            return None

    def _job_paths(self, ids):
        paths=[]
        for jid in ids:
            if not isinstance(jid,str) or not re.fullmatch(HEX,jid):
                raise ArchiveError('历史目录中的任务编号无效，不能读取原回执')
            path=self.state/'requests'/('job-'+jid+'.json')
            if self._token(path) is not None:
                paths.append(path)
        return paths

    def _controls(self):
        return [self._token(self.state/name) for name in ('requests','units','external-history')]

    def _drop_temporaries(self):
        old=self.legacy
        old.cache.clear();old.cache_bytes=0
        old.projected.clear();old.projected_units=0
        while len(old.retained_projected)>2:
            old.retained_projected.popitem(last=False)

    def _sync(self, force=False):
        before=self._controls()
        if not force and before == self.catalog.get_meta('controls'):
            return
        prior=self.catalog.get_meta('sources',{})
        sources={}
        retained=self.catalog.get_meta('retained',{})
        errors=self.catalog.get_meta('errors',{})
        for path in sorted((self.state/'requests').glob('job-*.json')):
            jid=path.name[4:-5]
            if not re.fullmatch(HEX,jid):
                continue
            token=self._token(path);sources[jid]=token
            if not force and prior.get(jid)==token:
                continue
            rows=[]
            result=self.legacy._snapshot('',{},job_paths=[path],include_external=False,collector=rows)
            errors.pop(jid,None)
            if result['warnings']:
                errors[jid]=result['warnings']
            self.catalog.replace_job(jid,rows)
            cached=self.legacy.retained_projected.get(path)
            if cached:
                retained[jid]=cached['projection']
            else:
                retained.pop(jid,None)
            self._drop_temporaries()
        for jid in set(prior)-set(sources):
            self.catalog.remove_job(jid);retained.pop(jid,None);errors.pop(jid,None)
        # External receipts keep their own validation, including target identity
        # and ambiguity checks. Rebuild this small subset only when it changes.
        if force or before[2] != (self.catalog.get_meta('controls') or [None]*3)[2]:
            items,warnings=project_external_history(self.state,self.legacy.identity,
                self.legacy.label,self.legacy.production,{})
            previous=self.catalog.get_meta('external',[])
            current=[]
            for item in items:
                jid=item['job_id'];current.append(jid)
                receipt=read_json(self.state/'external-history'/('external-'+jid+'.json'))
                self.catalog.replace_job(jid,[(item,digest(file_identity(receipt['files'])))])
            for jid in set(previous)-set(current):
                self.catalog.remove_job(jid)
            self.catalog.set_meta('external',current)
            self.catalog.set_meta('external_warnings',warnings)
        for key,value in (('sources',sources),('retained',retained),('errors',errors),('controls',before)):
            self.catalog.set_meta(key,value)

    def _remember(self, model):
        uid=model['report_id']
        compact={'current':{u['unit_id']:digest(file_identity(u['files'])) for u in model['units']},
                 'units':[{'unit_id':u['unit_id'],'files':[{'blake3':f.get('blake3'),'size_bytes':f['size_bytes']}
                        for f in u['files']]} for u in model['units']]}
        raw=zlib.compress(json.dumps(compact,separators=(',',':')).encode(),1)
        previous=self.reports.pop(uid,b'');self.report_bytes-=len(previous)
        if len(raw)>32*1024*1024:
            raise ArchiveError('当前素材范围超过页面缓存预算，请缩小活动批次范围')
        self.reports[uid]=raw;self.report_bytes+=len(raw)
        while len(self.reports)>4 or self.report_bytes>32*1024*1024:
            _,old=self.reports.popitem(last=False);self.report_bytes-=len(old)
        return compact

    def _read(self, report_id, scope, *, display=True):
        self._sync()
        current,units=scope['current'],scope['units']
        fingerprints={(f['blake3'],f['size_bytes']) for u in units for f in u['files'] if f.get('blake3')}
        ids=self.catalog.matching_jobs(current,fingerprints)
        wanted={**self.catalog.matching_scopes(current,fingerprints),**current}
        paths=self._job_paths(ids)
        result=self.legacy._snapshot(report_id,wanted,selected_only=True,job_paths=paths,
                                     include_external=False)
        # External receipt validation is scoped by unit ID. It never grants
        # completion to an unrelated old destination.
        external_ids=set(self.catalog.get_meta('external',[])) & ids
        external,external_warnings=project_external_history(self.state,self.legacy.identity,self.legacy.label,
            self.legacy.production,current,selected_only=True,receipt_paths=[
                self.state/'external-history'/('external-'+jid+'.json') for jid in external_ids])
        result['warnings'].extend(external_warnings)
        seen={r['unit_id'] for r in result['archived_units']}
        result['archived_units'].extend(r for r in external if r['unit_id'] not in seen)
        for row in result['archived_units']:
            if row['unit_id'] not in current:
                row['in_current_model']=False
        retained_ids=[jid for jid,p in self.catalog.get_meta('retained',{}).items()
                      if display and set(p['units']) & current.keys()]
        retained_state=self.legacy._snapshot(report_id,current,job_paths=self._job_paths(retained_ids),include_external=False)
        result.update({key:retained_state[key] for key in
                       ('retained_unit_ids','retained_current_files','retained_jobs')})
        copies=ArchiveHistory._content_copies(units,result['archived_units'],current)
        result=ArchiveHistory._with_content_copies(result,copies)
        result['warnings'].extend(retained_state['warnings'])
        # Only current matches remain hot. Cold records are read through SQL
        # pagination; duplicate origins must not inflate the page's current list.
        result['archived_units']=[r for r in result['archived_units'] if r.get('in_current_model')]
        result['history_total']=self.catalog.total
        result['history_generation']=str(self.catalog.generation)
        self._drop_temporaries()
        return result

    def snapshot(self, model):
        with self.lock:
            return self._read(model['report_id'],self._remember(model))

    def for_report(self, report_id):
        with self.lock:
            if report_id not in self.reports:
                raise ValueError('当前页面的素材报告已过期，请先保存草稿，再刷新页面核对归档状态。')
            self.reports.move_to_end(report_id)
            return self._read(report_id,json.loads(zlib.decompress(self.reports[report_id])))

    def for_units(self, model, unit_ids):
        with self.lock:
            compact={'current':{u['unit_id']:digest(file_identity(u['files'])) for u in model['units'] if u['unit_id'] in unit_ids},'units':[]}
            return self._read(model['report_id'],compact,display=False)

    def page(self, report_id, generation, offset, limit):
        with self.lock:
            self._sync()
            if generation and generation != str(self.catalog.generation):
                raise ValueError('历史记录已更新，请重新展开历史')
            result=self.catalog.page(offset,limit)
            scope={'current':self.catalog.matching_scopes([r['unit_id'] for r in result['items']]),'units':[]}
            verified=self._read(report_id,scope)
            rows={r['unit_id']:r for r in verified['archived_units']}
            result['items']=[rows[r['unit_id']] for r in result['items'] if r['unit_id'] in rows]
            return {'version':'archive-history-page/1','report_id':report_id,
                    'generation':str(self.catalog.generation),'total':result['total'],
                    'offset':offset,'next_offset':result['next_offset'],'archived_units':result['items'],'warnings':verified['warnings']}
