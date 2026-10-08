"""Reuse unchanged parsed queue snapshots; never cache live file authorization."""
from copy import deepcopy
from pathlib import Path
import re
import threading
from .order_feed_io import read_json
from .order_feed_view import apply_feed
from .review import validate_model

def token(path):
    try:
        s=path.lstat()
        if path.is_symlink():raise ValueError('快照不能是符号链接')
        return (s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns,s.st_ctime_ns)
    except FileNotFoundError:return None

class QueueModelCache:
    def __init__(self, root):
        self.root=Path(root);self.lock=threading.RLock();self.key=None;self.model=None;self.orders={}

    def _current(self):
        with self.lock:
            state=read_json(self.root/'队列状态.json')
            name=state.get('base_model_path')
            if not isinstance(name,str) or not re.fullmatch(r'combined/sha256:[a-f0-9]{64}/确认模型\.json',name):
                raise ValueError('当前还没有可提交的确认快照')
            path=self.root/name;key=(name,token(path))
            if self.key!=key:
                model=read_json(path);validate_model(model)
                if (name,token(path))!=key:raise ValueError('素材清单正在更新，请稍后读取')
                self.key=key;self.model=model;self.orders={}
            # Order freshness is checked on each use. Existing feed cache skips
            # unchanged enrichment; callers own their copy and cannot poison it.
            model,_=apply_feed(self.root,self.model,cache=self.orders)
            return model

    def __call__(self):
        with self.lock:
            return deepcopy(self._current())

    def current_report_id(self):
        """Recheck snapshot/order freshness without cloning unrelated units."""
        with self.lock:
            return self._current()['report_id']
