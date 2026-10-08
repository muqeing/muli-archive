"""Presentation-only pending-photo index. Never an archive authority.

Build once from a validated page generation. Lookups read at most six rows;
startup validates one immutable DB, not the canonical model or old receipts.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import tempfile
import threading
from datetime import datetime
from .order_feed_io import digest, canonical

FILE = re.compile(r'[a-f0-9]{64}\.sqlite\Z')
MAX_BYTES = 128 * 1024 * 1024

def selected_photos(units):
    photos = [u for u in units if u.get('kind') == 'photo']
    timed = True
    try:
        times = {u['unit_id']: datetime.fromisoformat(u['capture_time'].replace('Z','+00:00')).timestamp() for u in photos}
    except (KeyError, ValueError, AttributeError):
        timed = False
        times = {}
    if timed:
        photos.sort(key=lambda u:(times[u['unit_id']],u['unit_id']))
    count = min(6,len(photos))
    if count == len(photos): return photos
    start,span = (times[photos[0]['unit_id']],times[photos[-1]['unit_id']]-times[photos[0]['unit_id']]) if timed else (0,0)
    chosen=[];previous=-1
    for i in range(count):
        target=start+span*i/(count-1) if timed and span>0 else (len(photos)-1)*i/(count-1)
        best=min(range(previous+1,len(photos)-count+i+1),key=lambda j:abs((times[photos[j]['unit_id']] if timed and span>0 else j)-target))
        chosen.append(photos[best]);previous=best
    return chosen

class PhotoPreviewIndex:
    def __init__(self, root):
        self.root=Path(root);self.root.mkdir(mode=0o700,parents=True,exist_ok=True)
        if self.root.is_symlink() or not self.root.is_dir(): raise ValueError('预览索引目录无效')
        self.lock=threading.RLock();self.connection=None;self.descriptor=None;self.signature=None;self.closed=False

    def close(self, *, final=False):
        with self.lock:
            if self.connection is not None:self.connection.close()
            self.connection=None;self.descriptor=None
            if final:self.closed=True

    def _signature(self,path):
        st=path.lstat()
        if not stat.S_ISREG(st.st_mode) or st.st_nlink!=1 or st.st_size>MAX_BYTES:raise ValueError('预览索引文件身份异常')
        return (st.st_dev,st.st_ino,st.st_size,st.st_mtime_ns,st.st_ctime_ns)

    def activate(self, descriptor):
        with self.lock:
            if self.closed:raise ValueError('预览索引已关闭')
            if not isinstance(descriptor,dict) or not FILE.fullmatch(descriptor.get('file','')):raise ValueError('预览索引绑定无效')
            path=self.root/descriptor['file'];before=self._signature(path)
            if self.descriptor==descriptor and self.signature==before:return
            self.close()
            fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
            try:
                h=hashlib.sha256()
                with os.fdopen(os.dup(fd),'rb') as f:
                    for block in iter(lambda:f.read(1024*1024),b''):h.update(block)
                if h.hexdigest()!=descriptor.get('sha256') or self._signature(path)!=before:raise ValueError('预览索引损坏或发生变化')
            finally:os.close(fd)
            conn=sqlite3.connect(path.as_uri()+'?mode=ro&immutable=1',uri=True,check_same_thread=False)
            try:
                meta=dict(conn.execute('select name,value from meta'))
                if meta.get('schema')!='pending-photo-index/1' or meta.get('report_id')!=descriptor.get('report_id') or int(meta.get('count','-1'))!=descriptor.get('count'):raise ValueError('预览索引报告不匹配')
                if conn.execute('pragma quick_check').fetchone()!=('ok',):raise ValueError('预览索引数据库损坏')
                photos={}
                for uid,body,sha in conn.execute('select uid,body,sha from units'):
                    unit=json.loads(body)
                    if digest(unit)!=sha or unit.get('unit_id')!=uid or unit.get('kind')!='photo':raise ValueError('预览索引记录损坏')
                    photos[uid]=unit
                priority=[uid for ordinal,uid in conn.execute('select ordinal,uid from priority order by ordinal')]
                if len(photos)!=descriptor['count'] or len(priority)!=descriptor.get('priority_count') or len(set(priority))!=len(priority) or not set(priority)<=photos.keys():raise ValueError('预览索引范围不完整')
                token=digest({'report_id':descriptor['report_id'],'units':photos,'priority':priority})
                if descriptor['file']!=token+'.sqlite' or self._signature(path)!=before:raise ValueError('预览索引内容与待处理范围不一致')
            except BaseException:conn.close();raise
            self.connection=conn;self.descriptor=dict(descriptor);self.signature=before

    def build(self, model):
        photos={u['unit_id']: {'unit_id':u['unit_id'],'kind':'photo','files':u['files']} for u in model['units'] if u['kind']=='photo'}
        units={u['unit_id']:u for u in model['units']}
        priority=[];seen=set()
        for segment in model['initial_segments']:
            for unit in selected_photos([units[uid] for uid in segment['unit_ids'] if uid in units]):
                if unit['unit_id'] not in seen:priority.append(unit['unit_id']);seen.add(unit['unit_id'])
        token=digest({'report_id':model['report_id'],'units':photos,'priority':priority})
        final=self.root/(token+'.sqlite')
        # Only a new page generation builds. Always replace this derived file
        # from canonical input, so a corrupt pre-existing index repairs safely.
        with self.lock:
            if self.closed:raise ValueError('预览索引已关闭')
            fd,filename=tempfile.mkstemp(prefix='.preview-index-',dir=self.root);os.close(fd)
            temp=Path(filename)
            try:
                conn=sqlite3.connect(temp)
                with conn:
                    conn.execute('create table meta(name text primary key,value text not null)')
                    conn.execute('create table units(uid text primary key,body text not null,sha text not null)')
                    conn.execute('create table priority(ordinal integer primary key,uid text not null)')
                    conn.executemany('insert into meta values(?,?)',[('schema','pending-photo-index/1'),('report_id',model['report_id']),('count',str(len(photos)))])
                    conn.executemany('insert into units values(?,?,?)',((uid,canonical(u).decode(),digest(u)) for uid,u in photos.items()))
                    conn.executemany('insert into priority values(?,?)',enumerate(priority))
                conn.close()
                with temp.open('rb') as f:os.fsync(f.fileno())
                os.replace(temp,final)
                directory=os.open(self.root,os.O_RDONLY|os.O_DIRECTORY)
                try:os.fsync(directory)
                finally:os.close(directory)
            finally:
                if temp.exists():temp.unlink()
            descriptor={'file':final.name,'sha256':hashlib.sha256(final.read_bytes()).hexdigest(),'report_id':model['report_id'],'count':len(photos),'priority_count':len(priority)}
            self.activate(descriptor)
            return descriptor

    def lookup(self, descriptor, report_id, ids):
        if not isinstance(ids,list) or not 1<=len(ids)<=6 or any(not isinstance(x,str) or not 1<=len(x)<=200 for x in ids) or len(set(ids))!=len(ids):raise ValueError('每次只接受1至6张照片')
        with self.lock:
            self.activate(descriptor)
            if report_id!=self.descriptor['report_id']:raise ValueError('预览快照已更新，请刷新页面')
            rows={uid:(body,sha) for uid,body,sha in self.connection.execute('select uid,body,sha from units where uid in ('+','.join('?' for _ in ids)+')',ids)}
            if set(rows)!=set(ids):raise ValueError('照片不属于当前待处理范围')
            result=[]
            for uid in ids:
                body,sha=rows[uid];unit=json.loads(body)
                if digest(unit)!=sha or unit['unit_id']!=uid or unit['kind']!='photo':raise ValueError('预览素材索引损坏')
                result.append(unit)
            return result

    def priority_ids(self, descriptor, offset=0):
        with self.lock:
            self.activate(descriptor)
            return [row[0] for row in self.connection.execute('select uid from priority where ordinal>=? order by ordinal limit 6',(offset,))]
