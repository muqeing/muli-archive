"""Small task summaries; unchanged completed task bodies leave the hot path."""
from copy import deepcopy
import threading
from .order_feed_io import read_json

def summary(job):
    fields = ('job_id','status','summary','archive_options','projects','phase',
              'current_file_bytes','execution_strategy','direct_move_recovery_required',
              'completed_at','finished_at','completedAt','verification')
    result = {k:job[k] for k in fields if k in job}
    result['errors'] = list(job.get('errors') or []) + [r['error'] for r in job.get('outcomes',[]) if r.get('error')]
    return result

class JobCatalog:
    def __init__(self, root):
        self.root=root;self.lock=threading.RLock();self.rows={}

    def snapshot(self):
        with self.lock:
            live=set()
            for path in self.root.glob('job-*.json'):
                s=path.lstat();token=(s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns,s.st_ctime_ns)
                live.add(path)
                if path in self.rows and self.rows[path][0]==token:continue
                body=read_json(path)
                after=path.lstat()
                if token!=(after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns,after.st_ctime_ns):
                    # Atomic writer raced this read: don't publish stale summary.
                    self.rows.pop(path,None)
                    continue
                self.rows[path]=(token,summary(body))
            for path in set(self.rows)-live:del self.rows[path]
            return deepcopy([r[1] for r in sorted(self.rows.values(),key=lambda r:r[0][3],reverse=True)])
